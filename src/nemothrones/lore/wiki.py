"""A polite MediaWiki client for A Wiki of Ice and Fire.

* batched ``action=query`` (titles in groups), ``list=search``, redirects followed
* at most one request per ``rate_limit_s`` on a single connection, ``maxlag=5``
* a descriptive User-Agent (your contact only if you configure one)
* retries with backoff on 429/503/maxlag
* every page is cached in lore.db; cached pages are served without a request
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from .. import __version__
from ..config import Settings
from .cache import LoreCache

INFO_BATCH = 50


class WikiError(RuntimeError):
    pass


@dataclass
class WikiPage:
    title: str
    revid: int | None
    length: int | None
    wikitext: str
    categories: list[str] = field(default_factory=list)


def user_agent(contact: str) -> str:
    ua = f"NemoThrones/{__version__} (personal non-commercial fan game; https://github.com/JackWetherell/NemoThrones"
    return ua + (f"; {contact})" if contact else ")")


def norm(title: str) -> str:
    t = title.strip().replace("_", " ")
    t = t.split("#", 1)[0].strip()
    return t[:1].upper() + t[1:] if t else t


class WikiClient:
    def __init__(self, settings: Settings, cache: LoreCache, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = settings.lore
        self.cache = cache
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.requests = 0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.cfg.timeout_s, transport=self._transport, trust_env=self._transport is None,
                headers={"User-Agent": user_agent(self.cfg.contact), "Accept": "application/json"},
                limits=httpx.Limits(max_connections=1))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, params: dict) -> dict:
        params = {**params, "format": "json", "formatversion": "2", "maxlag": "5"}
        async with self._lock:
            for attempt in range(4):
                wait = self._last + self.cfg.rate_limit_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                self.requests += 1
                try:
                    r = await self._http().get(self.cfg.api_url, params=params)
                except httpx.HTTPError as e:
                    if attempt == 3:
                        raise WikiError(f"{type(e).__name__}: {e}") from e
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code in (429, 503):
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise WikiError(f"HTTP {r.status_code}: {r.text[:200]}")
                try:
                    data = r.json()
                except ValueError as e:
                    raise WikiError(f"non-JSON reply from {self.cfg.api_url}: {r.text[:120]!r}") from e
                err = data.get("error")
                if err and err.get("code") == "maxlag":
                    await asyncio.sleep(5)
                    continue
                if err:
                    raise WikiError(f"{err.get('code')}: {err.get('info')}")
                return data
        raise WikiError("wiki busy: retries exhausted")

    # ------------------------------------------------------------------ api
    async def siteinfo(self) -> dict:
        data = await self._get({"action": "query", "meta": "siteinfo", "siprop": "general"})
        return data.get("query", {}).get("general", {})

    async def search(self, query: str, limit: int = 10) -> list[str]:
        cached = self.cache.get_search(query, self.cfg.max_age_days)
        if cached is not None:
            return cached
        data = await self._get({"action": "query", "list": "search", "srsearch": query,
                                "srlimit": str(limit), "srnamespace": "0"})
        titles = [h["title"] for h in data.get("query", {}).get("search", [])]
        self.cache.put_search(query, titles)
        return titles

    def _mapping(self, q: dict, requested: list[str]) -> dict[str, str]:
        hop: dict[str, str] = {}
        for n in q.get("normalized", []):
            hop[n["from"]] = n["to"]
        for rd in q.get("redirects", []):
            hop[rd["from"]] = rd["to"]
        out = {}
        for t in requested:
            cur, seen = t, set()
            while cur in hop and cur not in seen:
                seen.add(cur)
                cur = hop[cur]
            out[t] = cur
            self.cache.put_redirect(t, cur)
        return out

    async def info(self, titles: list[str]) -> dict[str, int]:
        """Page lengths for many titles (no content). Missing pages are omitted."""
        out: dict[str, int] = {}
        todo = []
        for t in dict.fromkeys(norm(x) for x in titles if x):
            c = self.cache.get_info(t)
            if c and LoreCache.fresh(c["fetched_at"], self.cfg.max_age_days):
                if not c["missing"]:
                    out[t] = c["length"] or 0
            else:
                todo.append(t)
        for i in range(0, len(todo), INFO_BATCH):
            batch = todo[i:i + INFO_BATCH]
            data = await self._get({"action": "query", "prop": "info", "titles": "|".join(batch), "redirects": "1"})
            q = data.get("query", {})
            mapping = self._mapping(q, batch)
            pages = {p["title"]: p for p in q.get("pages", [])}
            for t in batch:
                p = pages.get(mapping[t])
                missing = p is None or p.get("missing") or p.get("invalid")
                self.cache.put_info(mapping[t], None if missing else p.get("length"), bool(missing))
                if not missing:
                    out[t] = int(p.get("length") or 0)
            self.cache.commit()
        return out

    async def pages(self, titles: list[str]) -> dict[str, WikiPage]:
        """Content for many titles, keyed by the requested (normalised) title."""
        out: dict[str, WikiPage] = {}
        todo = []
        for t in dict.fromkeys(norm(x) for x in titles if x):
            c = self.cache.get_page(t)
            if c and LoreCache.fresh(c["fetched_at"], self.cfg.max_age_days):
                out[t] = WikiPage(c["title"], c["revid"], c["length"], c["wikitext"] or "", c["categories"])
            else:
                todo.append(t)
        step = max(1, self.cfg.batch_titles)
        for i in range(0, len(todo), step):
            batch = todo[i:i + step]
            params = {"action": "query", "prop": "revisions|info|categories", "rvprop": "content|ids",
                      "rvslots": "main", "titles": "|".join(batch), "redirects": "1", "cllimit": "max"}
            merged: dict[str, dict] = {}
            mapping: dict[str, str] = {}
            while True:
                data = await self._get(params)
                q = data.get("query", {})
                mapping.update(self._mapping(q, batch))
                for p in q.get("pages", []):
                    cur = merged.setdefault(p["title"], {"title": p["title"]})
                    for k, v in p.items():
                        if k == "categories":
                            cur.setdefault("categories", []).extend(v)
                        elif k == "revisions" and not cur.get("revisions"):
                            cur["revisions"] = v
                        elif k not in cur:
                            cur[k] = v
                cont = data.get("continue")
                if not cont:
                    break
                params = {**params, **cont}
            for t in batch:
                p = merged.get(mapping.get(t, t))
                if not p or p.get("missing") or p.get("invalid") or not p.get("revisions"):
                    continue
                rev = p["revisions"][0]
                text = (rev.get("slots", {}).get("main", {}) or {}).get("content") or rev.get("content") or ""
                cats = [c["title"].split(":", 1)[-1] for c in p.get("categories", [])]
                page = WikiPage(p["title"], rev.get("revid"), p.get("length"), text, cats)
                self.cache.put_page(page.title, page.revid, page.length, page.wikitext, page.categories)
                out[t] = page
        return out

    async def page(self, title: str) -> WikiPage | None:
        return (await self.pages([title])).get(norm(title))

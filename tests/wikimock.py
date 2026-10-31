"""An in-process MediaWiki API serving the synthetic fixtures in tests/fixtures/wiki."""

from __future__ import annotations

from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "wiki"
REDIRECTS = {"Catelyn Tully": "Catelyn Stark", "Ned Stark": "Eddard Stark", "Robert Baratheon": "Robert I Baratheon"}


def pages() -> dict[str, str]:
    return {p.stem.replace("_", " "): p.read_text() for p in FIXTURES.glob("*.wikitext")}


class WikiMock:
    def __init__(self):
        self.pages = pages()
        self.calls: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.calls.append(params)
        q: dict = {}
        if params.get("meta") == "siteinfo":
            return httpx.Response(200, json={"query": {"general": {
                "sitename": "A Wiki of Ice and Fire (synthetic fixture)", "generator": "MediaWiki 1.39"}}})
        if params.get("list") == "search":
            words = params["srsearch"].lower().split()
            hits = [t for t in self.pages if all(w in t.lower() for w in words)]
            return httpx.Response(200, json={"query": {"search": [{"title": t} for t in hits[:10]]}})
        titles = params.get("titles", "").split("|")
        redirects, out = [], []
        for t in titles:
            target = REDIRECTS.get(t, t)
            if target != t:
                redirects.append({"from": t, "to": target})
            text = self.pages.get(target)
            if text is None:
                out.append({"title": target, "missing": True})
                continue
            page = {"title": target, "length": len(text)}
            if "revisions" in params.get("prop", ""):
                page["revisions"] = [{"revid": 1000 + len(text), "slots": {"main": {"content": text}}}]
                page["categories"] = []
            out.append(page)
        q["pages"] = out
        if redirects:
            q["redirects"] = redirects
        return httpx.Response(200, json={"query": q})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

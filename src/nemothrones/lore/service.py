"""LoreService: the Maester's front door, used by the REPL, the Game and the CLI.

* search_characters / probe / cast / dossier  (CLI and /new)
* lore_text / command                         (/lore, spoiler-filtered to the in-game date)
* promote                                     (a canon character outside the cast enters the story)
* build_pack                                  (any character, any year -> era pack)
"""

from __future__ import annotations

import re

import httpx

from ..agents.prompts import lore_deep_system, user_message
from ..agents.schemas import LoreAnswer
from ..config import Settings
from ..engine import clock
from ..world.fate import record_fate
from ..world.pack import CharacterSpec
from ..world.seed import character_row
from .cache import LoreCache
from .dates import alive_at
from .dossier import DossierOut, FateOut, build_dossier, extract_fate
from .erapack import build_era_pack, match_place, slug
from .parse import ParsedPage, parse_wikitext
from .slicer import resolve_ambiguous, slice_page
from .wiki import WikiClient, WikiError, norm

ATTRIBUTION = "A Wiki of Ice and Fire (awoiaf.westeros.org), text CC BY-SA"
DEFAULT_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject an in-process wiki here


class LoreService:
    def __init__(self, settings: Settings, llm, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.llm = llm
        self.cache = LoreCache(settings.lore_db)
        self.wiki = WikiClient(settings, self.cache, transport or DEFAULT_TRANSPORT)

    async def aclose(self) -> None:
        await self.wiki.aclose()
        self.cache.close()

    # ------------------------------------------------------------ pages
    async def parsed(self, title: str) -> ParsedPage | None:
        wp = await self.wiki.page(title)
        return parse_wikitext(wp.title, wp.wikitext, wp.categories) if wp else None

    async def find(self, query: str) -> ParsedPage | None:
        page = await self.parsed(query)
        if page:
            return page
        hits = await self.wiki.search(query, 5)
        return await self.parsed(hits[0]) if hits else None

    async def search_characters(self, query: str) -> list[str]:
        exact = await self.parsed(query)
        hits = await self.wiki.search(query, 10)
        titles = list(dict.fromkeys(([exact.title] if exact else []) + hits))[:8]
        pages = await self.wiki.pages(titles)
        out = []
        for t in titles:
            wp = pages.get(norm(t))
            if wp and parse_wikitext(wp.title, wp.wikitext).is_character:
                out.append(wp.title)
        return list(dict.fromkeys(out))

    async def probe(self, title: str = "Eddard Stark") -> dict:
        site = await self.wiki.siteinfo()
        wp = await self.wiki.page(title)
        if wp is None:
            raise WikiError(f"page {title!r} not found")
        p = parse_wikitext(wp.title, wp.wikitext, wp.categories)
        return {"site": site.get("sitename", "?"), "generator": site.get("generator"), "title": p.title,
                "revid": wp.revid, "length": wp.length, "infobox_template": p.infobox_name,
                "infobox_keys": sorted(p.infobox), "born": p.infobox.get("born"), "died": p.infobox.get("died"),
                "is_character": p.is_character, "sections": [" > ".join(s.path) for s in p.sections][:40],
                "links": len(p.links), "requests": self.wiki.requests}

    # ------------------------------------------------------------ dossiers
    async def dossier_for(self, page: ParsedPage, year: int) -> tuple[DossierOut, FateOut]:
        d = self.cache.get_dossier(page.title, year)
        f = self.cache.get_fate(page.title, year)
        if d is not None and f is not None:
            return DossierOut.model_validate(d), FateOut.model_validate(f)
        sliced = await resolve_ambiguous(self.llm, page.display_name, slice_page(page, year))
        dossier = DossierOut.model_validate(d) if d is not None else await build_dossier(self.llm, page, sliced)
        fate = FateOut.model_validate(f) if f is not None else await extract_fate(self.llm, page, sliced)
        self.cache.put_dossier(page.title, year, dossier.model_dump())
        self.cache.put_fate(page.title, year, fate.model_dump())
        return dossier, fate

    async def dossier(self, title: str, year: int) -> dict:
        page = await self.find(title)
        if page is None:
            raise WikiError(f"no page for {title!r}")
        d, f = await self.dossier_for(page, year)
        return {"title": page.title, "as_of": f"{year} AC", "dossier": d.model_dump(),
                "FATE (spoilers, Director-only)": f.model_dump()}

    async def cast(self, title: str, year: int):
        from .cast import select_cast

        page = await self.find(title)
        if page is None:
            raise WikiError(f"no page for {title!r}")
        return await select_cast(self.wiki, page, year, self.settings.game.cast_size)

    async def build_pack(self, character: str, year: int, moon: int | None = None, progress=None):
        page = await self.find(character)
        if page is None:
            raise WikiError(f"no WOIAF page for {character!r}")
        return await build_era_pack(self, page.title, year, moon, self.settings.game.cast_size, progress)

    # ------------------------------------------------------------ /lore
    async def lore_text(self, topic: str, year: int) -> str:
        page = await self.find(topic)
        if page is None:
            return f"The maesters have no record of '{topic}'."
        sliced = slice_page(page, year)  # no LLM: anything ambiguous is simply withheld
        rows = [page.display_name]
        from .dossier import SAFE_INFOBOX

        for k in SAFE_INFOBOX:  # never titles, allegiances or spouses: lifetime lists leak the future
            v = page.infobox.get(k)
            if v:
                flat = re.sub(r"\s+", " ", v)[:160]
                rows.append(f"{k.title()}: {flat}")
        died = page.died
        if died and died[1] is not None and died[1] < year:
            rows.append(f"Died: {page.infobox.get('died', '')[:120]}")
        past = sliced.past
        past = re.sub(r"\[[^\]]+\] ", "", past)
        rows.append("")
        rows.append(past[:1800] + ("…" if len(past) > 1800 else "") if past.strip()
                    else "(Nothing recorded before this date could be shown without spoilers.)")
        url = self.settings.lore.page_url + page.title.replace(" ", "_")
        rows += ["", f"Source: {ATTRIBUTION} — \"{page.title}\" <{url}>",
                 f"Filtered to what was recorded before {year} AC; the filter may be imperfect."]
        return "\n".join(rows)

    async def command(self, game, arg: str) -> list:
        from ..engine.game import Block
        from ..llm import LLMUnavailable
        from ..llm.budget import BudgetExceeded

        year = clock.year_of(game.store.now)
        try:
            if arg.lower().startswith("deep "):
                q = arg[5:].strip()
                excerpt = ""
                page = await self.find(q)
                if page:
                    excerpt = slice_page(page, year).past[:4000]
                ctx = {"question": q, "as_of": f"{year} AC", "wiki_excerpt_before_date": excerpt}
                ans = await game.llm.call("lore_deep", lore_deep_system(f"{year} AC"), user_message(ctx, q),
                                          LoreAnswer)
                src = f"\n\nSources: {', '.join(ans.sources)}" if ans.sources else ""
                return [Block("sheet", ans.answer + src + f"\n({ATTRIBUTION})")]
            return [Block("sheet", await self.lore_text(arg, year))]
        except (WikiError, httpx.HTTPError) as e:
            return [Block("error", f"A Wiki of Ice and Fire is unreachable: {e}")]
        except (LLMUnavailable, BudgetExceeded) as e:
            return [Block("error", f"The maesters cannot answer now: {e}")]

    # ------------------------------------------------------------ promotion
    async def promote(self, game, name: str) -> str | None:
        """Bring a canon character who is not in the cast into the story (alive, sliced, as an agent)."""
        s = game.store
        existing = s.find_character(name)
        if existing:
            return existing["id"]
        try:
            page = await self.find(name)
        except (WikiError, httpx.HTTPError):
            return None
        if page is None or not page.is_character:
            return None
        year = clock.year_of(s.now)
        if alive_at(page.born, page.died, year) is False:
            return None
        try:
            d, f = await self.dossier_for(page, year)
        except Exception:  # noqa: BLE001
            return None
        cid = slug(page.display_name)
        while s.character(cid):
            cid += "_x"
        with s.transaction():
            loc_id = None
            g = match_place(d.location)
            if g:
                loc_id = g["id"]
                if not s.location(loc_id):
                    s.emit("location_added", {"id": g["id"], "name": g["name"], "region": g.get("region"),
                                              "kind": g.get("kind"), "x": g.get("x"), "y": g.get("y"),
                                              "data": {"aliases": g.get("aliases", []),
                                                       "sublocations": g.get("sublocations", []),
                                                       "has_maester": g.get("has_maester", False)}},
                           visibility="system")
            elif d.location:
                found = s.find_location(d.location)
                loc_id = found["id"] if found else None
            house = s.find_house(d.house) if d.house else None
            born = page.born
            spec = CharacterSpec(
                id=cid, name=page.display_name, wiki_title=page.title, house=house["id"] if house else None,
                born_year=(born[1] if born and born[1] is not None else (born[0] if born else None)),
                location=loc_id, sublocation=d.sublocation, importance=0.4, titles=d.titles[:4],
                gender=d.gender, culture=d.culture, faith=d.faith, appearance=d.appearance, traits=d.traits,
                values=d.values, fears=d.fears, voice=d.voice, summary=d.summary, skills=d.skills,
                possessions=d.possessions, secrets=d.secrets, activity=d.activity)
            row = character_row(spec, is_player=False, start_t=s.now)
            row["tier"] = 1
            row["next_act_t"] = s.now + clock.MIN_PER_DAY
            s.emit("character_added", row, actors=[cid], summary=f"{page.display_name} enters the story",
                   visibility="system")
            fate_text = "\n".join([f.summary] + [f"{e.year} AC: {e.title}. {e.summary}" for e in f.events]).strip()
            if fate_text:
                record_fate(s, cid, fate_text, source="woiaf")
            for k in d.knowledge[:6]:
                s.emit("belief_added", {"holder": cid, "subject": k.subject, "content": k.content,
                                        "source": "your own life"}, visibility="system")
            for a in d.ambitions[:3]:
                s.emit("goal_added", {"owner": cid, "level": "ambition", "text": a}, visibility="system")
            if d.summary:
                s.emit("memory_added", {"owner": cid, "kind": "reflection", "text": d.summary, "importance": 6,
                                        "entities": [page.display_name]}, visibility="system")
        return cid

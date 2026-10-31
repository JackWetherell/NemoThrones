"""Era packs: a whole starting world built from WOIAF.

player page -> cast (top N) -> per character: time-slice -> dossier (past) + fate ->
houses, places, relationships, canon timeline, opening scene -> WorldPack.
Dossiers are cached per (page, year), so the next start in the same era is quick.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

import yaml

from ..world.pack import (
    CanonEventSpec,
    CharacterSpec,
    HouseSpec,
    Knowledge,
    LocationSpec,
    OpeningSpec,
    RelationshipSpec,
    WorldPack,
)
from .cast import CastEntry, monarchs_at_start, season_at, select_cast
from .dossier import DossierOut, FateOut
from .parse import ParsedPage, parse_wikitext


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "x"


@lru_cache(maxsize=1)
def gazetteer() -> list[dict]:
    return yaml.safe_load(resources.files("nemothrones.data").joinpath("gazetteer.yaml").read_text())


def match_place(name: str | None) -> dict | None:
    if not name:
        return None
    low = name.lower()
    best = None
    for g in gazetteer():
        names = [g["name"].lower()] + [a.lower() for a in g.get("aliases", [])]
        for n in names:
            if n == low:
                return g
            if n in low and (best is None or len(n) > len(best[1])):
                best = (g, n)
    return best[0] if best else None


@dataclass
class Built:
    entry: CastEntry
    page: ParsedPage
    dossier: DossierOut
    fate: FateOut


class NameIndex:
    """Resolve the names an LLM writes ('Ned', 'Lord Tywin', 'Cersei Lannister') to cast ids."""

    def __init__(self):
        self.full: dict[str, str] = {}
        self.first: dict[str, list[str]] = {}

    def add(self, cid: str, *names: str) -> None:
        for n in names:
            if not n:
                continue
            n = n.lower().strip()
            self.full.setdefault(n, cid)
            first = re.sub(r"^(lord|lady|ser|maester|king|queen|prince|princess)\s+", "", n).split()[0]
            self.first.setdefault(first, [])
            if cid not in self.first[first]:
                self.first[first].append(cid)

    def get(self, name: str | None) -> str | None:
        if not name:
            return None
        n = re.sub(r"^(lord|lady|ser|maester|king|queen|prince|princess)\s+", "", name.lower().strip())
        if name.lower().strip() in self.full:
            return self.full[name.lower().strip()]
        if n in self.full:
            return self.full[n]
        cands = self.first.get(n.split()[0], []) if n else []
        return cands[0] if len(cands) == 1 else None


async def build_era_pack(svc, player_title: str, year: int, moon: int | None, size: int,
                         progress=None) -> tuple[WorldPack, str]:
    say = progress or (lambda m: None)
    wp = await svc.wiki.page(player_title)
    if wp is None:
        raise ValueError(f"no WOIAF page called {player_title!r}")
    ppage = parse_wikitext(wp.title, wp.wikitext, wp.categories)
    if not ppage.is_character:
        raise ValueError(f"{wp.title!r} does not look like a character page")
    from .dates import alive_at

    if alive_at(ppage.born, ppage.died, year) is False:
        raise ValueError(f"{ppage.display_name} is not alive in {year} AC (born {ppage.infobox.get('born', '?')}, "
                         f"died {ppage.infobox.get('died', '?')})")
    key = f"{ppage.title}|{year}|{moon or 1}|{size}"
    cached = svc.cache.get_pack(key)
    if cached:
        say("Using the cached era pack.")
        pack = WorldPack.model_validate(cached)
        return pack, pack.default_player

    cast = await select_cast(svc.wiki, ppage, year, size, say)
    entries = [CastEntry(ppage.title, 1e9, ["player"], ppage.born, ppage.died, ppage)] + cast
    say(f"Writing {len(entries)} dossiers as of {year} AC (cached for next time)…")
    sem = asyncio.Semaphore(max(1, svc.settings.lore.dossier_concurrency))
    done = 0

    async def one(e: CastEntry) -> Built | None:
        nonlocal done
        async with sem:
            try:
                d, f = await svc.dossier_for(e.page, year)
            except Exception as exc:  # noqa: BLE001 - a bad page should not sink the era
                say(f"  skipped {e.title}: {exc}")
                return None
            done += 1
            if done % 10 == 0:
                say(f"  {done}/{len(entries)} dossiers")
            return Built(e, e.page, d, f)

    built = [b for b in await asyncio.gather(*(one(e) for e in entries)) if b]
    if not built or built[0].entry.title != ppage.title:
        raise RuntimeError("could not build the player's own dossier")
    pack = await assemble(svc, built, year, moon)
    svc.cache.put_pack(key, pack.model_dump())
    return pack, pack.default_player


async def assemble(svc, built: list[Built], year: int, moon: int | None) -> WorldPack:
    ids = NameIndex()
    cid_of: dict[str, str] = {}
    used: set[str] = set()
    for b in built:
        cid = slug(b.page.display_name)
        while cid in used:
            cid += "_x"
        used.add(cid)
        cid_of[b.page.title] = cid
        ids.add(cid, b.page.display_name, b.page.title, b.dossier.name)

    # ---- places
    locations: dict[str, LocationSpec] = {}

    def place(name: str | None) -> str | None:
        if not name:
            return None
        g = match_place(name)
        if g:
            locations.setdefault(g["id"], LocationSpec(**g))
            return g["id"]
        lid = slug(name)[:40]
        locations.setdefault(lid, LocationSpec(id=lid, name=name.strip()[:80]))
        return lid

    # ---- houses
    house_titles = list(dict.fromkeys(b.dossier.house for b in built if b.dossier.house))
    hpages = await svc.wiki.pages(house_titles) if house_titles else {}
    houses: dict[str, HouseSpec] = {}
    for title in house_titles:
        hp = hpages.get(title)
        info = parse_wikitext(hp.title, hp.wikitext).infobox if hp else {}
        seat_name = (info.get("seat") or "").split("\n")[0]
        hid = slug(re.sub(r"^house\s+", "", title, flags=re.I))
        houses[hid] = HouseSpec(id=hid, name=title, words=(info.get("words") or None),
                                sigil=(info.get("coat of arms") or info.get("sigil") or None),
                                seat=place(seat_name) if seat_name else None, region=info.get("region"))

    def house_id(title: str | None) -> str | None:
        if not title:
            return None
        hid = slug(re.sub(r"^house\s+", "", title, flags=re.I))
        return hid if hid in houses else None

    # ---- characters
    top = max((b.entry.score for b in built[1:]), default=1.0) or 1.0
    chars: list[CharacterSpec] = []
    for i, b in enumerate(built):
        d, page = b.dossier, b.page
        born = page.born
        fate_lines = [b.fate.summary] + [f"{e.year or '?'} AC: {e.title}. {e.summary}" for e in b.fate.events]
        chars.append(CharacterSpec(
            id=cid_of[page.title], name=page.display_name, wiki_title=page.title, house=house_id(d.house),
            born_year=(born[1] if born and born[1] is not None else (born[0] if born else None)),
            location=place(d.location), sublocation=d.sublocation or "",
            importance=1.0 if i == 0 else round(0.3 + 0.7 * max(0.0, b.entry.score) / top, 3),
            titles=d.titles[:4], gender=d.gender, culture=d.culture, faith=d.faith, appearance=d.appearance,
            traits=d.traits[:8], values=d.values[:6], fears=d.fears[:5], voice=d.voice, summary=d.summary,
            skills=d.skills, health=d.health or "hale", possessions=d.possessions[:6], secrets=d.secrets[:4],
            ambitions=d.ambitions[:3], plans=d.plans[:3],
            knowledge=[Knowledge(subject=k.subject[:120], content=k.content[:400], source="your own life")
                       for k in d.knowledge[:6]],
            activity=d.activity[:120],
            fate="\n".join(x for x in fate_lines if x).strip() or None,
        ))

    # ---- relationships: the dossier's view first, then kin from the infobox
    rels: dict[tuple[str, str], RelationshipSpec] = {}
    for b in built:
        a = cid_of[b.page.title]
        for r in b.dossier.relationships:
            other = ids.get(r.name)
            if other and other != a:
                rels.setdefault((a, other), RelationshipSpec(
                    a=a, b=other, kind=r.kind[:40] or "acquaintance", opinion=max(-100, min(100, r.opinion)),
                    trust=max(-100, min(100, r.trust)), notes=r.note[:200]))
    kin_rules = [("father", "father", "child"), ("mother", "mother", "child"),  # specific first
                 ("issue", "child", "parent"), ("children", "child", "parent")]
    for key, kind_ab, kind_ba in kin_rules:
        for b in built:
            a = cid_of[b.page.title]
            for t in b.page.infobox_links.get(key, []):
                other = cid_of.get(t)
                if not other or other == a:
                    continue
                rels.setdefault((a, other), RelationshipSpec(a=a, b=other, kind=kind_ab, opinion=50, trust=50))
                rels.setdefault((other, a), RelationshipSpec(a=other, b=a, kind=kind_ba, opinion=50, trust=50))

    # ---- canon timeline (Director-only)
    events: dict[str, CanonEventSpec] = {}
    for b in built:
        for e in b.fate.events:
            if e.year is None or e.year < year:
                continue
            words = [w for w in re.findall(r"[a-z]+", e.title.lower()) if len(w) > 3][:3]
            k = f"{e.year}:{'_'.join(words)}"
            parts = [p for p in (ids.get(n) for n in e.participants) if p]
            drivers = [p for p in (ids.get(n) for n in e.drivers) if p]
            pre = []
            for c in e.preconditions:
                who = ids.get(str(c.get("who", "")))
                if not who:
                    continue
                if c.get("type") == "at":
                    pre.append({"type": "at", "who": who, "where": place(str(c.get("where", ""))) or ""})
                else:
                    pre.append({"type": "alive", "who": who})
            if k in events:
                ev = events[k]
                ev.participants = list(dict.fromkeys(ev.participants + parts))
                ev.drivers = list(dict.fromkeys(ev.drivers + drivers))
                continue
            events[k] = CanonEventSpec(
                id=slug(k)[:60], year=e.year, moon=e.moon if e.moon and 1 <= e.moon <= 12 else None,
                title=e.title[:120], summary=e.summary[:400], participants=parts, drivers=drivers,
                location=place(e.location), preconditions=pre, chance=e.chance, wiki_ref=b.page.title)

    # ---- common knowledge and opening
    monarchs = monarchs_at_start(year)
    common = []
    if monarchs:
        names = " and ".join(re.sub(r"\s*\(.*\)$", "", m) for m in monarchs)
        common.append(Knowledge(subject="The Iron Throne",
                                content=f"{names} {'claim' if len(monarchs) > 1 else 'sits'} the Iron Throne."))
    player = chars[0]
    here = [c for c in chars[1:] if c.location and c.location == player.location]
    here.sort(key=lambda c: -c.importance)
    same_room = [c.id for c in here if (c.sublocation or "") == (player.sublocation or "")][:3]
    opening = OpeningSpec(location=player.location or (next(iter(locations), "unknown")),
                          sublocation=player.sublocation or "", present=same_room,
                          situation=f"You are {player.activity}." if player.activity else "")
    if not player.location:
        lid = opening.location
        locations.setdefault(lid, LocationSpec(id=lid, name="somewhere in the realm"))
        player.location = lid
    loc_name = locations[player.location].name if player.location in locations else "the realm"
    return WorldPack(
        id=f"woiaf_{player.id}_{year}", title=f"{player.name} — {loc_name}, {year} AC", source="woiaf",
        year=year, moon=moon or 1, day=1, hour=9, season=season_at(year), default_player=player.id,
        characters=chars, houses=list(houses.values()), locations=list(locations.values()),
        relationships=list(rels.values()), common_knowledge=common, canon_events=list(events.values()),
        openings={player.id: opening},
        notes="Generated from A Wiki of Ice and Fire (CC BY-SA). Skills, opinions and map positions are game estimates.")

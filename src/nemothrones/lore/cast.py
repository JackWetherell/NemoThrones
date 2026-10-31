"""Cast selection: the N most relevant canon characters alive at the start year.

Phase A (cheap): candidate titles -> page lengths only. Keep the top ~2.5N plus
anyone tied to the player. Phase B: fetch content, keep character pages alive at
the start year, and score:

    score = 1.0*notability + 1.2*rank + 2.0*proximity + 1.0*canon_involvement

No canon character is ever invented; sparse eras simply get smaller casts.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

import yaml

from .dates import alive_at, years_in
from .parse import ParsedPage, parse_wikitext
from .wiki import WikiClient, norm

RANK = [
    (re.compile(r"\b(king|queen)\b(?! ?s?guard)", re.I), 1.0),
    (re.compile(r"hand of the (king|queen)", re.I), 0.9),
    (re.compile(r"lord paramount|warden of the|lord protector", re.I), 0.8),
    (re.compile(r"lord commander|grand maester|master of (coin|whisperers|laws|ships)|lord regent", re.I), 0.7),
    (re.compile(r"\b(prince|princess)\b", re.I), 0.6),
    (re.compile(r"\blord of\b|\blady of\b|\bhead of house\b", re.I), 0.5),
    (re.compile(r"\bheir\b", re.I), 0.45),
    (re.compile(r"kingsguard|\bser\b|knight", re.I), 0.3),
    (re.compile(r"\bmaester\b|\bseptons?\b|\bsepta\b", re.I), 0.3),
]
NON_CHARACTER = re.compile(r"^(house |list of |\d+ (ac|bc)$|the |battle of |siege of |war of |category:)", re.I)


@lru_cache(maxsize=1)
def anchors() -> dict:
    return yaml.safe_load(resources.files("nemothrones.data").joinpath("era_anchors.yaml").read_text())


def anchors_at(year: int) -> tuple[list[str], list[str]]:
    """Everyone who reigns at any point in ``year`` (cast candidates), and the great houses."""
    a = anchors()
    monarchs = [m["title"] for m in a["monarchs"] if m["from"] <= year <= m["to"]]
    houses = [h["title"] for h in a["great_houses"] if h["from"] <= year < h["to"]]
    return monarchs, houses


def monarchs_at_start(year: int) -> list[str]:
    """Who sits the throne on the first day of ``year``. In a year of succession the old
    monarch still reigns at its start; naming the successor would be a spoiler."""
    ms = [m for m in anchors()["monarchs"] if m["from"] <= year <= m["to"]]
    already = [m for m in ms if m["from"] < year]
    return [m["title"] for m in (already or ms)]


def season_at(year: int) -> str:
    for s in anchors().get("seasons", []):
        if s["from"] <= year < s["to"]:
            return s["season"]
    return "unrecorded"


@dataclass
class CastEntry:
    title: str
    score: float
    reasons: list[str] = field(default_factory=list)
    born: tuple | None = None
    died: tuple | None = None
    page: ParsedPage | None = None
    length: int = 0


def rank_of(page: ParsedPage, year: int) -> float:
    """Rank from the infobox title list. It is a LIFETIME list, so it slightly favours
    people who will matter; that is acceptable for selection, and it is never shown
    to agents (dossiers build titles from the time-sliced past only)."""
    text = page.infobox.get("title", "")
    best = 0.0
    for rx, w in RANK:
        if rx.search(text):
            best = max(best, w)
    return best


def notability(length: int) -> float:
    return max(0.0, min(1.0, (math.log10(max(length, 1)) - 3.0) / 2.0))


async def select_cast(wiki: WikiClient, player: ParsedPage, year: int, size: int,
                      progress=None) -> list[CastEntry]:
    say = progress or (lambda m: None)
    monarchs, houses = anchors_at(year)
    year_pages = [f"{y} AC" if y >= 1 else f"{1 - y} BC" for y in (year - 1, year, year + 1, year + 2)]
    say(f"Gathering candidates around {player.display_name} in {year} AC…")
    ctx_pages = await wiki.pages(houses + year_pages)
    future_links: set[str] = set()
    candidates: list[str] = list(player.links) + monarchs
    for t, wp in ctx_pages.items():
        parsed = parse_wikitext(wp.title, wp.wikitext)
        candidates += parsed.links
        if t in year_pages[1:]:
            future_links |= set(parsed.links)
    kin = set(player.kin_links())
    must = set(kin) | set(monarchs)
    pool = [c for c in dict.fromkeys(norm(x) for x in candidates) if c and c != player.title
            and not NON_CHARACTER.match(c)]
    say(f"Weighing {len(pool)} names…")
    lengths = await wiki.info(pool)
    ranked = sorted(lengths, key=lambda t: -lengths[t])
    keep = list(dict.fromkeys([t for t in pool if t in must and t in lengths] + ranked[: int(size * 2.5)]))
    say(f"Reading {len(keep)} pages…")
    pages = await wiki.pages(keep)
    p_house = (player.allegiances() or [None])[0]
    out: list[CastEntry] = []
    seen: set[str] = set()
    for t, wp in pages.items():
        if wp.title in seen:  # two names (e.g. a redirect) for one page
            continue
        seen.add(wp.title)
        page = parse_wikitext(wp.title, wp.wikitext, wp.categories)
        if not page.is_character or page.title == player.title:
            continue
        alive = alive_at(page.born, page.died, year)
        if alive is False:
            continue
        reasons = []
        prox = 0.0
        names = {t, page.title} | {k for k, v in pages.items() if v.title == wp.title}
        if names & kin or player.title in page.kin_links():
            prox += 1.0
            reasons.append("kin")
        elif names & set(player.links):
            prox += 0.6
            reasons.append("linked from player")
        if player.title in page.links:
            prox += 0.3
            reasons.append("links to player")
        if p_house and p_house in page.allegiances():
            prox += 0.3
            reasons.append(f"sworn to {p_house}")
        prox = min(prox, 1.5)
        canon = 0.5 if names & future_links else 0.0
        if canon:
            reasons.append("in upcoming events")
        r = rank_of(page, year)
        if r:
            reasons.append(f"rank {r:.1f}")
        if names & set(monarchs):
            r = max(r, 1.0)
            reasons.append("reigning")
        n = notability(wp.length or len(wp.wikitext))
        unknown_birth_penalty = 0.4 if alive is None else 0.0
        score = 1.0 * n + 1.2 * r + 2.0 * prox + 1.0 * canon - unknown_birth_penalty
        out.append(CastEntry(page.title, round(score, 3), reasons, page.born, page.died, page, wp.length or 0))
    out.sort(key=lambda e: -e.score)
    return out[: max(0, size - 1)]


def year_mentions(text: str) -> list[int]:
    return years_in(text)

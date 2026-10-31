"""The time-slicer: split a WOIAF life into PAST (before the start year) and FATE.

Agents are built from the past only; fate becomes the Director's canon timeline.

Rules, in order:
1. Skipped sections (quotes, references, behind the scenes, ...) are dropped.
2. Book "Recent Events" sections are dated by data/books.yaml: wholly before the
   start year = past; starting at or after it = fate; straddling = per paragraph.
3. Other paragraphs are classified by the years they mention: all earlier = past,
   any later = fate, only the start year itself = ambiguous. A paragraph with none
   inherits the previous paragraph's class in chronological sections (History);
   in descriptive ones (lead, Appearance and Character) it is ambiguous.
4. A paragraph mentioning years on both sides is split by sentence.
5. Ambiguous passages go to an LLM classifier. If it is unavailable they are
   treated as fate: never risk a spoiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .dates import years_in
from .parse import ParsedPage, skip_section

CHRONO_RE = re.compile(r"(history|early life|biography|background|childhood|reign|war|rebellion|life)", re.I)
SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")


@lru_cache(maxsize=1)
def books() -> dict[str, tuple[int, int] | None]:
    raw = yaml.safe_load(resources.files("nemothrones.data").joinpath("books.yaml").read_text())
    return {k.lower(): (tuple(v) if v else None) for k, v in raw.items()}


@dataclass
class Passage:
    where: str
    text: str
    label: str  # past | fate | ambiguous


@dataclass
class Sliced:
    as_of: int
    passages: list[Passage] = field(default_factory=list)

    def text(self, label: str, limit: int = 12000) -> str:
        out, n = [], 0
        for p in self.passages:
            if p.label == label:
                chunk = f"[{p.where}] {p.text}"
                if n + len(chunk) > limit:
                    break
                out.append(chunk)
                n += len(chunk)
        return "\n\n".join(out)

    @property
    def past(self) -> str:
        return self.text("past")

    @property
    def fate(self) -> str:
        return self.text("fate")

    @property
    def ambiguous(self) -> list[Passage]:
        return [p for p in self.passages if p.label == "ambiguous"]


def _by_years(text: str, as_of: int) -> str | None:
    ys = years_in(text)
    if not ys:
        return None
    if max(ys) < as_of:
        return "past"
    if min(ys) > as_of:
        return "fate"
    if min(ys) == max(ys) == as_of:
        return "ambiguous"  # the start year itself: may already be true on its first day
    return "mixed"


def _split_sentences(where: str, para: str, as_of: int, default: str) -> list[Passage]:
    out: list[Passage] = []
    cur = default
    for sent in SENT_RE.split(para):
        lab = _by_years(sent, as_of)
        if lab == "mixed":
            out.append(Passage(where, sent, "ambiguous"))
            continue
        cur = lab or cur
        out.append(Passage(where, sent, cur))
    merged: list[Passage] = []
    for p in out:  # re-join consecutive sentences with the same label
        if merged and merged[-1].label == p.label:
            merged[-1].text += " " + p.text
        else:
            merged.append(p)
    return merged


def _book_of(path: tuple[str, ...]) -> tuple[int, int] | None | bool:
    """(start, end) for a dated book section; None for an undated anthology; False if not a book."""
    for t in reversed(path):
        key = t.lower().strip()
        if key in books():
            return books()[key]
    return False


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]


def slice_page(page: ParsedPage, as_of: int) -> Sliced:
    sl = Sliced(as_of)
    for para in _paragraphs(page.lead):
        sl.passages += _split_sentences("lead", para, as_of, "ambiguous")
    for sec in page.sections:
        if skip_section(sec) or not sec.text.strip():
            continue
        where = " > ".join(sec.path)
        book = _book_of(sec.path)
        if book is not False and book is not None:
            start, end = book
            if end < as_of:
                sl.passages += [Passage(where, p, "past") for p in _paragraphs(sec.text)]
                continue
            if start >= as_of:
                sl.passages += [Passage(where, p, "fate") for p in _paragraphs(sec.text)]
                continue
            default = "ambiguous"
        elif CHRONO_RE.search(sec.path[0]) or CHRONO_RE.search(sec.title):
            default = "past"
        else:
            default = "ambiguous"
        cur = default
        for para in _paragraphs(sec.text):
            lab = _by_years(para, as_of)
            if lab == "mixed":
                parts = _split_sentences(where, para, as_of, cur)
                sl.passages += parts
                cur = parts[-1].label if parts and parts[-1].label != "ambiguous" else cur
            elif lab is None:
                sl.passages.append(Passage(where, para, cur))
            else:
                sl.passages.append(Passage(where, para, lab))
                if default == "past":
                    cur = lab  # chronological: undated paragraphs follow their predecessor
    return sl


class SlicerLabels(BaseModel):
    model_config = ConfigDict(extra="ignore")
    labels: list[str] = Field(default_factory=list)


SLICER_SYSTEM = """You are a careful maester helping NemoThrones avoid spoilers.
For each numbered PASSAGE from A Wiki of Ice and Fire about {name}, decide whether it describes ONLY things true or done before {as_of} ("past"), or anything that happens in or after {as_of} ("fate"). General descriptions that would already be true before {as_of} are "past". If a passage mixes both or you are unsure, answer "fate".
Reply with ONLY JSON: {{"labels": ["past" or "fate", ...]}} with exactly one label per passage, in order."""


async def resolve_ambiguous(llm, name: str, sliced: Sliced, max_passages: int = 40) -> Sliced:
    """Ask the slicer role about ambiguous passages; anything unresolved becomes fate."""
    from ..agents.prompts import user_message
    from ..llm import LLMUnavailable
    from ..llm.budget import BudgetExceeded

    amb = sliced.ambiguous[:max_passages]
    if amb and llm is not None:
        ctx = {"name": name, "as_of": f"{sliced.as_of} AC",
               "passages": [f"{i + 1}. {p.text[:700]}" for i, p in enumerate(amb)]}
        try:
            out = await llm.call("slicer", SLICER_SYSTEM.format(name=name, as_of=f"{sliced.as_of} AC"),
                                 user_message(ctx), SlicerLabels)
            if len(out.labels) == len(amb):
                for p, lab in zip(amb, out.labels, strict=True):
                    p.label = "past" if str(lab).strip().lower() == "past" else "fate"
        except (LLMUnavailable, BudgetExceeded):
            pass
    for p in sliced.passages:
        if p.label == "ambiguous":
            p.label = "fate"
    return sliced

"""WOIAF date strings to year ranges (astronomical years: N AC = N, N BC = 1 - N)."""

from __future__ import annotations

import re

_ERA = r"(AC|BC|A\.C\.|B\.C\.)"
YEAR_RE = re.compile(rf"\b(\d{{1,5}})\s*{_ERA}", re.I)
BETWEEN_RE = re.compile(rf"between\s+(\d{{1,5}})\s*{_ERA}?\s*(?:and|&|-|–)\s*(\d{{1,5}})\s*{_ERA}", re.I)
SPAN_RE = re.compile(rf"\b(\d{{1,5}})\s*{_ERA}?\s*[–-]\s*(\d{{1,5}})\s*{_ERA}", re.I)
BEFORE_RE = re.compile(rf"\bbefore\s+(\d{{1,5}})\s*{_ERA}", re.I)
AFTER_RE = re.compile(rf"\bafter\s+(\d{{1,5}})\s*{_ERA}", re.I)


def to_year(n: str | int, era: str | None) -> int:
    n = int(n)
    return 1 - n if era and era.upper().startswith("B") else n


def parse_year_range(text: str | None) -> tuple[int | None, int | None] | None:
    """'In 263 AC' -> (263, 263); 'In or between 282 AC and 283 AC' -> (282, 283);
    'Before 280 AC' -> (None, 279); '2 BC' -> (-1, -1). None if no date is found."""
    if not text:
        return None
    s = text.replace(" ", " ")
    m = BETWEEN_RE.search(s)
    if m:
        a = to_year(m.group(1), m.group(2) or m.group(4))
        b = to_year(m.group(3), m.group(4))
        return (min(a, b), max(a, b))
    m = SPAN_RE.search(s)
    if m:
        a = to_year(m.group(1), m.group(2) or m.group(4))
        b = to_year(m.group(3), m.group(4))
        return (min(a, b), max(a, b))
    m = BEFORE_RE.search(s)
    if m:
        return (None, to_year(m.group(1), m.group(2)) - 1)
    m = AFTER_RE.search(s)
    if m:
        return (to_year(m.group(1), m.group(2)) + 1, None)
    m = YEAR_RE.search(s)
    if m:
        y = to_year(m.group(1), m.group(2))
        return (y, y)
    return None


def years_in(text: str) -> list[int]:
    return [to_year(n, era) for n, era in YEAR_RE.findall(text or "")]


def alive_at(born: tuple | None, died: tuple | None, year: int) -> bool | None:
    """True/False when the dates decide it; None when they cannot (unknown birth)."""
    if died is not None and died[1] is not None and died[1] < year:
        return False  # died before the start year (dying during it is fate, not history)
    if born is not None:
        lo, hi = born
        if lo is not None and lo > year:
            return False
        if hi is not None and hi <= year:
            return True
        if lo is not None and lo <= year:
            return True
    return None

"""Westerosi time.

Internal time ``t`` is an integer count of minutes. Year numbering is astronomical:
``N AC`` is year ``N``; ``N BC`` is year ``1 - N`` (so 1 BC is year 0, 2 BC is -1).
A year has 365 days in twelve moons; seasons are *not* tied to the calendar (they
are world state, declared by the Citadel's white ravens).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MOON_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
DAYS_PER_YEAR = sum(MOON_DAYS)
MIN_PER_HOUR = 60
MIN_PER_DAY = 24 * MIN_PER_HOUR
MIN_PER_YEAR = DAYS_PER_YEAR * MIN_PER_DAY
_CUM = [sum(MOON_DAYS[:i]) for i in range(12)]


@dataclass(frozen=True)
class WDate:
    year: int
    moon: int
    day: int
    hour: int = 0
    minute: int = 0


def to_t(year: int, moon: int = 1, day: int = 1, hour: int = 0, minute: int = 0) -> int:
    if not 1 <= moon <= 12:
        raise ValueError(f"moon must be 1..12, got {moon}")
    if not 1 <= day <= MOON_DAYS[moon - 1]:
        raise ValueError(f"day {day} out of range for moon {moon}")
    doy = _CUM[moon - 1] + (day - 1)
    return year * MIN_PER_YEAR + doy * MIN_PER_DAY + hour * MIN_PER_HOUR + minute


def from_t(t: int) -> WDate:
    year, rem = divmod(int(t), MIN_PER_YEAR)
    doy, mins = divmod(rem, MIN_PER_DAY)
    moon = 1
    while doy >= MOON_DAYS[moon - 1]:
        doy -= MOON_DAYS[moon - 1]
        moon += 1
    hour, minute = divmod(mins, MIN_PER_HOUR)
    return WDate(year, moon, doy + 1, hour, minute)


def year_of(t: int) -> int:
    return int(t) // MIN_PER_YEAR


def year_label(year: int) -> str:
    return f"{year} AC" if year >= 1 else f"{1 - year} BC"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def time_of_day(hour: int) -> str:
    if hour < 5:
        return "the dead of night"
    if hour < 7:
        return "dawn"
    if hour < 12:
        return "morning"
    if hour < 14:
        return "midday"
    if hour < 18:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "night"


def format_date(t: int, with_time: bool = True) -> str:
    d = from_t(t)
    s = f"{ordinal(d.day)} day of the {ordinal(d.moon)} moon, {year_label(d.year)}"
    if with_time:
        s += f", {time_of_day(d.hour)}"
    return s


def short_date(t: int) -> str:
    d = from_t(t)
    return f"{ordinal(d.moon)} moon {year_label(d.year)}"


def age_years(born_t: int | None, t: int) -> int | None:
    if born_t is None:
        return None
    return max(0, (int(t) - int(born_t)) // MIN_PER_YEAR)


_UNITS = {
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": MIN_PER_HOUR, "hr": MIN_PER_HOUR, "hrs": MIN_PER_HOUR, "hour": MIN_PER_HOUR,
    "hours": MIN_PER_HOUR,
    "d": MIN_PER_DAY, "day": MIN_PER_DAY, "days": MIN_PER_DAY,
    "w": 7 * MIN_PER_DAY, "week": 7 * MIN_PER_DAY, "weeks": 7 * MIN_PER_DAY,
    "fortnight": 14 * MIN_PER_DAY, "fortnights": 14 * MIN_PER_DAY,
    "moon": 30 * MIN_PER_DAY, "moons": 30 * MIN_PER_DAY,
    "month": 30 * MIN_PER_DAY, "months": 30 * MIN_PER_DAY,
    "y": MIN_PER_YEAR, "year": MIN_PER_YEAR, "years": MIN_PER_YEAR,
    "turn": 30 * MIN_PER_DAY,  # "a turn of the moon"
}
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "half": 0.5}


def parse_duration(text: str) -> int:
    """Parse '2 hours', '30m', 'a fortnight', '1 day 6 hours' into minutes."""
    s = text.strip().lower()
    if not s:
        raise ValueError("empty duration")
    total = 0.0
    found = False
    for num, unit in re.findall(r"([\d.]+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|half)?\s*([a-z]+)", s):
        if unit not in _UNITS:
            continue
        if not num:
            n = 1.0
        elif num in _WORDNUM:
            n = float(_WORDNUM[num])
        else:
            n = float(num)
        total += n * _UNITS[unit]
        found = True
    if not found:
        if s.isdigit():  # bare number = minutes
            return int(s)
        raise ValueError(f"cannot parse duration: {text!r}")
    return max(1, int(round(total)))


def format_duration(minutes: int) -> str:
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if minutes < MIN_PER_DAY:
        h = round(minutes / 60, 1)
        h = int(h) if h == int(h) else h
        return f"{h} hour{'s' if h != 1 else ''}"
    d = round(minutes / MIN_PER_DAY, 1)
    d = int(d) if d == int(d) else d
    return f"{d} day{'s' if d != 1 else ''}"


def next_morning(t: int, hour: int = 7) -> int:
    d = from_t(t)
    today = to_t(d.year, d.moon, d.day, hour)
    return today if today > t else today + MIN_PER_DAY

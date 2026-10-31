"""Health over time: wounds heal or kill, and age takes everyone eventually.

Background hazard is Gompertz-like in age (a game model, tuned so a 30-year-old has
~0.5%/year and a 70-year-old ~15%/year). Wounds add a daily hazard. All draws use
the 'mortality' dice stream so they replay deterministically.
"""

from __future__ import annotations

import math

from ..world.store import Store
from . import clock
from .rng import Dice

WOUND_DAILY_HAZARD = {1: 0.0, 2: 0.003, 3: 0.06}
WOUND_HEAL_DAYS = {1: 14, 2: 60, 3: 30}  # severity 3 that is survived for 30 days becomes 2


def annual_hazard(age: float) -> float:
    return min(0.9, 0.0004 * math.exp(0.085 * age))


def step(store: Store, dice: Dice, dt_minutes: int) -> list[tuple[str, str]]:
    """Advance health by ``dt_minutes``; returns [(character_id, cause)] of new deaths."""
    if dt_minutes <= 0:
        return []
    now = store.now
    deaths: list[tuple[str, str]] = []
    dt_days = dt_minutes / clock.MIN_PER_DAY
    for c in store.characters(alive=True):
        wounds = list(c["attrs"].get("wounds", []))
        p_survive = 1.0
        cause = None
        if dt_days >= 0.25:
            age = clock.age_years(c.get("born_t"), now)
            h = annual_hazard(age if age is not None else 40)
            p_survive *= math.exp(-h * dt_days / clock.DAYS_PER_YEAR)
            cause = "illness and age"
        worst = max((w.get("severity", 1) for w in wounds), default=0)
        if worst >= 2:
            p_survive *= (1 - WOUND_DAILY_HAZARD[worst]) ** dt_days
            cause = "wounds that would not heal"
        if p_survive < 1.0 and dice.chance("mortality", 1 - p_survive):
            deaths.append((c["id"], cause or "illness"))
            continue
        healed, changed = [], False
        for w in wounds:
            age_days = (now - w.get("t", now)) / clock.MIN_PER_DAY
            sev = w.get("severity", 1)
            if age_days >= WOUND_HEAL_DAYS.get(sev, 30):
                changed = True
                if sev == 3:
                    healed.append({**w, "severity": 2, "t": now, "desc": w.get("desc", "a wound") + " (mending)"})
                elif sev == 2:
                    healed.append({**w, "severity": 1, "t": now, "desc": w.get("desc", "a wound") + " (scarring)"})
                continue
            healed.append(w)
        if changed:
            store.emit("attrs_changed", {"id": c["id"], "set": {"wounds": healed}}, visibility="system")
    return deaths

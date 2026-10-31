"""Tiered level-of-detail scheduler: everyone acts; cadence follows relevance.

Tier 0  in the player's scene      every player turn (on-stage call)
Tier 1  ~25 most entangled         daily game time (individual background call)
Tier 2  rest of the cast           weekly (batched regional calls)
Tier 3  extras                     on demand (rule-based)
Budget pressure stretches Tier 1/2 cadence; the player's scene is never degraded.
"""

from __future__ import annotations

from ..config import GameConfig
from ..world.store import Store
from .rng import Dice


def entanglement(store: Store, c: dict, pc_id: str, pc_loc: str | None) -> float:
    score = float(c.get("importance", 0.5))
    rel = store.relationship(c["id"], pc_id)
    back = store.relationship(pc_id, c["id"])
    score += (abs(rel["opinion"]) + abs(back["opinion"])) / 100.0
    if rel["kind"] not in ("stranger", "acquaintance"):
        score += 0.4
    if pc_loc and c.get("location_id") == pc_loc:
        score += 0.8
    return score


def cadence(tier: int, cfg: GameConfig, pressure: float) -> int | None:
    base = {1: cfg.tier1_cadence_min, 2: cfg.tier2_cadence_min}.get(tier)
    if base is None:
        return None
    return int(base * (1.0 + 2.0 * pressure))


def retier(store: Store, cfg: GameConfig, scene_present: list[str], dice: Dice, pressure: float = 0.0) -> None:
    pc_id = store.player_id
    pc = store.character(pc_id)
    pc_loc = pc.get("location_id") if pc else None
    now = store.now
    others = [c for c in store.characters(alive=True) if c["id"] != pc_id]
    ranked = sorted(others, key=lambda c: -entanglement(store, c, pc_id, pc_loc))
    tier1 = {c["id"] for c in ranked[: cfg.tier1_size]}
    for c in others:
        if not c.get("canon") and c["tier"] == 3:
            want = 3
        elif c["id"] in scene_present:
            want = 0
        elif c["id"] in tier1:
            want = 1
        else:
            want = 2
        update: dict = {}
        if want != c["tier"]:
            update["tier"] = want
        if want in (1, 2) and (c.get("next_act_t") is None or want != c["tier"]):
            cad = cadence(want, cfg, pressure) or cfg.tier1_cadence_min
            update["next_act_t"] = now + int(cad * (0.25 + 0.75 * dice.uniform("sched")))
        if update:
            store.emit("tier_changed", {"id": c["id"], **update}, visibility="system")


def due(store: Store, now: int, scene_present: list[str]) -> tuple[list[dict], list[dict]]:
    pc_id = store.player_id
    t1, t2 = [], []
    for c in store.characters(alive=True):
        if c["id"] == pc_id or c["id"] in scene_present or c["tier"] in (0, 3):
            continue
        if c.get("next_act_t") is not None and c["next_act_t"] <= now:
            (t1 if c["tier"] == 1 else t2).append(c)
    return t1, t2


def reschedule(store: Store, cid: str, tier: int, cfg: GameConfig, dice: Dice, pressure: float) -> None:
    now = store.now
    cad = cadence(tier, cfg, pressure) or cfg.tier2_cadence_min
    nxt = now + int(cad * (0.75 + 0.5 * dice.uniform("sched")))
    store.emit("tier_changed", {"id": cid, "next_act_t": nxt, "last_act_t": now}, visibility="system")

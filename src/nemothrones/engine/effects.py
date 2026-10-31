"""Apply Warden-validated effects to the world (as events)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agents.schemas import Effect
from ..world.store import Store

WOUND_WORDS = {1: "lightly wounded", 2: "seriously wounded", 3: "grievously wounded"}


@dataclass
class EffectReport:
    impacts: dict[str, dict] = field(default_factory=dict)
    deaths: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def name_of(store: Store, cid: str | None) -> str:
    c = store.character(cid)
    return c["name"] if c else (cid or "someone")


def wound(store: Store, cid: str, severity: int, desc: str) -> None:
    c = store.character(cid)
    if not c or not c["alive"]:
        return
    wounds = list(c["attrs"].get("wounds", []))
    wounds.append({"severity": int(severity), "desc": desc[:120] or WOUND_WORDS.get(severity, "a wound"),
                   "t": store.now})
    store.emit("attrs_changed", {"id": cid, "set": {"wounds": wounds}}, actors=[cid],
               summary=f"{c['name']} is {WOUND_WORDS.get(severity, 'wounded')}", location_id=c.get("location_id"))


def kill(store: Store, cid: str, cause: str) -> None:
    c = store.character(cid)
    if not c or not c["alive"]:
        return
    store.emit("character_died", {"id": cid, "cause": cause[:200]}, actors=[cid],
               location_id=c.get("location_id"), summary=f"{c['name']} dies: {cause[:160]}")


def move_to(store: Store, cid: str, to: str) -> str:
    """Move within the current location (sublocation) or to a known location id."""
    loc = store.location(to) or store.find_location(to)
    c = store.character(cid)
    if loc and loc["id"] != c.get("location_id"):
        store.emit("character_moved", {"id": cid, "location_id": loc["id"], "sublocation": ""}, actors=[cid],
                   summary=f"{c['name']} goes to {loc['name']}")
        return loc["name"]
    store.emit("character_moved", {"id": cid, "sublocation": to[:60]}, actors=[cid],
               summary=f"{c['name']} goes to {to[:60]}")
    return to


def apply_effects(store: Store, effects: list[Effect], *, actor_id: str | None, context: str = "") -> EffectReport:
    rep = EffectReport()
    for e in effects:
        if e.type == "impact" and e.target:
            cur = rep.impacts.setdefault(e.target, {"value": 0, "note": ""})
            cur["value"] = max(-3, min(3, cur["value"] + e.value))
            cur["note"] = (cur["note"] + "; " if cur["note"] else "") + (e.note or "")
        elif e.type == "relationship" and e.a and e.b:
            store.emit("relationship_changed", {"a": e.a, "b": e.b, "delta": {
                "opinion": e.opinion, "trust": e.trust, "fear": e.fear, "respect": e.respect},
                "note": (e.note or context)[:120]}, actors=[e.a, e.b], visibility="private",
                summary=f"{name_of(store, e.a)}'s regard for {name_of(store, e.b)} shifts")
        elif e.type == "wound" and e.target:
            wound(store, e.target, e.severity, e.note or context or "a wound")
            rep.lines.append(f"{name_of(store, e.target)} is {WOUND_WORDS.get(e.severity, 'wounded')}.")
        elif e.type == "death" and e.target:
            kill(store, e.target, e.cause or context or "violence")
            rep.deaths.append(e.target)
            rep.lines.append(f"{name_of(store, e.target)} dies.")
        elif e.type == "belief" and e.holder:
            store.emit("belief_added", {"holder": e.holder, "subject": e.subject, "content": e.content,
                                        "source": "learned"}, visibility="private", actors=[e.holder])
        elif e.type == "move" and e.target and e.to:
            where = move_to(store, e.target, e.to)
            rep.lines.append(f"{name_of(store, e.target)} goes to {where}.")
        elif e.type == "goal" and e.target and e.text:
            store.emit("goal_added", {"owner": e.target, "level": "plan", "text": e.text[:300]},
                       visibility="private", actors=[e.target])
    return rep

"""The Ravenry: who learns what, and when.

M1 scope: witnessing (instant beliefs for those present) and letters (ravens between
seats with maesters, riders otherwise) delivered after a distance-based delay, with
a small interception risk. Rumour diffusion and proclamations arrive in M3.
"""

from __future__ import annotations

import math

from ..world.store import Store
from . import clock
from .rng import Dice

RAVEN_MILES_PER_DAY = 250.0  # game estimate, not canon
RIDER_MILES_PER_DAY = 40.0
PARTY_MILES_PER_DAY = 30.0  # a lord's party on the road
DEFAULT_MILES = 200.0
INTERCEPT_RISK = 0.02


def distance(store: Store, a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    if a == b:
        return 0.0
    la, lb = store.location(a), store.location(b)
    if not la or not lb:
        return None
    for loc in (la, lb):
        if loc.get("parent_id") and (loc.get("x") is None):
            parent = store.location(loc["parent_id"])
            if parent:
                loc["x"], loc["y"] = parent.get("x"), parent.get("y")
    if None in (la.get("x"), la.get("y"), lb.get("x"), lb.get("y")):
        return None
    return math.hypot(la["x"] - lb["x"], la["y"] - lb["y"])


def travel_minutes(miles: float, miles_per_day: float) -> int:
    return max(30, int(miles / miles_per_day * clock.MIN_PER_DAY))


def send_letter(store: Store, sender_id: str, recipient_id: str, content: str, *, sealed: bool = True) -> int:
    s, r = store.character(sender_id), store.character(recipient_id)
    miles = distance(store, s.get("location_id"), r.get("location_id"))
    sloc, rloc = store.location(s.get("location_id")), store.location(r.get("location_id"))
    raven = bool(sloc and rloc and sloc["data"].get("has_maester") and rloc["data"].get("has_maester"))
    if miles == 0:
        channel, minutes = "by hand", 20
    else:
        mpd = RAVEN_MILES_PER_DAY if raven else RIDER_MILES_PER_DAY
        minutes = travel_minutes(miles if miles is not None else DEFAULT_MILES, mpd)
        channel = "raven" if raven else "rider"
    return store.emit("delivery_scheduled", {
        "sender": sender_id, "recipient": recipient_id, "channel": channel,
        "payload": {"kind": "letter", "content": content, "sealed": sealed},
        "deliver_t": store.now + minutes, "intercept_risk": INTERCEPT_RISK if miles else 0.0,
    }, actors=[sender_id, recipient_id], visibility="private",
        summary=f"{s['name']} sends a letter to {r['name']} ({channel})")


def deliver_due(store: Store, dice: Dice) -> list[dict]:
    """Deliver everything due by now. Returns delivered items (for perception)."""
    out = []
    for d in store.due_deliveries(store.now):
        recipient = store.character(d["recipient_id"])
        sender = store.character(d["sender_id"]) if d.get("sender_id") else None
        if not recipient or not recipient["alive"]:
            store.emit("delivery_done", {"delivery_id": d["id"], "state": 3}, visibility="system")
            continue
        if d["intercept_risk"] and dice.chance("ravenry", d["intercept_risk"]):
            store.emit("delivery_done", {"delivery_id": d["id"], "state": 2}, visibility="secret",
                       summary=f"A letter for {recipient['name']} never arrives")
            continue
        payload = d["payload"]
        who = sender["name"] if sender else "an unknown hand"
        text = f"A letter from {who} ({d['channel']}): {payload.get('content', '')}"
        store.emit("memory_added", {"owner": recipient["id"], "kind": "letter", "text": text[:1200],
                                    "importance": 6, "entities": [who]}, visibility="private")
        store.emit("belief_added", {"holder": recipient["id"], "subject": f"Letter from {who}",
                                    "content": payload.get("content", "")[:400], "source": d["channel"]},
                   visibility="private")
        store.emit("delivery_done", {"delivery_id": d["id"], "state": 1}, visibility="private",
                   actors=[recipient["id"]], summary=f"{recipient['name']} receives a letter from {who}")
        out.append({"recipient": recipient["id"], "sender": d.get("sender_id"), "sender_name": who,
                    "channel": d["channel"], "content": payload.get("content", "")})
    return out


def witness(store: Store, present: list[str], subject: str, content: str, event_id: int | None = None) -> None:
    for cid in present:
        store.emit("belief_added", {"holder": cid, "subject": subject[:120], "content": content[:400],
                                    "source": "witnessed", "event_id": event_id}, visibility="system")

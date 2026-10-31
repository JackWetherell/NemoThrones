"""The Director (game manager): pacing, hooks drawn from the living world, scene
arrivals/departures, and awareness of the canon horizon (Director-only)."""

from __future__ import annotations

from ...config import Settings
from ...engine import clock
from ...llm import LLM
from ...world.store import Store
from .. import context as cx
from ..prompts import director_system, user_message
from ..schemas import DirectorBeat

HORIZON_DAYS = 120


def canon_horizon(store: Store, days: int = HORIZON_DAYS) -> list[str]:
    now = store.now
    out = []
    for ce in store.canon_events("pending"):
        t = ce.get("t_expected")
        if t is None or now - 30 * clock.MIN_PER_DAY <= t <= now + days * clock.MIN_PER_DAY:
            when = clock.short_date(t) if t is not None else "undated"
            out.append(f"{when}: {ce['title']} — {ce['summary'][:200]}")
    return out[:12]


def nearby_people(store: Store, pc_id: str, scene: dict | None) -> list[dict]:
    if not scene:
        return []
    present = set(scene.get("present", []))
    out = []
    for c in store.characters(alive=True, location_id=scene.get("location_id")):
        if c["id"] in present or c["id"] == pc_id:
            continue
        out.append({"id": c["id"], "name": c["name"],
                    "where": c["status"].get("sublocation") or "about the place",
                    "activity": c["status"].get("activity", ""),
                    "plans": [g["text"] for g in store.goals(c["id"]) if g["level"] == "plan"][:3]})
    return out[:12]


def director_context(store: Store, settings: Settings, pc_id: str, scene: dict | None,
                     idle_turns: int, reason: str) -> dict:
    present = [c for c in (scene or {}).get("present", []) if c != pc_id and store.character(c)]
    pending = [d for d in store.conn.execute(
        "SELECT sender_id, channel, deliver_t FROM deliveries WHERE recipient_id=? AND delivered=0", (pc_id,))]
    return {
        "reason": reason,
        "scene": cx.scene_view(store, scene),
        "player": {"id": pc_id, "name": store.character(pc_id)["name"], "goals": cx.goals_view(store, pc_id)},
        "present": [cx.public_card(store, store.character(c), None) for c in present],
        "nearby": nearby_people(store, pc_id, scene),
        "idle_turns": idle_turns,
        "idle_threshold": settings.game.idle_turns_for_hook,
        "recent": cx.recent_lines(store, scene, 10),
        "letters_on_the_way_to_player": len(pending),
        "canon_gravity": settings.game.canon_gravity,
        "canon_horizon": canon_horizon(store),
    }


async def beat(llm: LLM, store: Store, settings: Settings, pc_id: str, scene: dict | None,
               idle_turns: int, reason: str) -> DirectorBeat:
    ctx = director_context(store, settings, pc_id, scene, idle_turns, reason)
    return await llm.call("director", director_system(), user_message(ctx), DirectorBeat)

"""Nemo agents: every character is a persistent record (persona, memory, beliefs,
goals, relationships) materialised into a prompt only when it acts.

Three call shapes, by level of detail:
* on-stage (Tier 0): reacts to this turn in the player's scene
* background (Tier 1): one call covering the elapsed span
* batch (Tier 2): one call simulating several people in a region
plus *exchange*: two off-stage agents meeting in person.
"""

from __future__ import annotations

from ..engine import clock
from ..llm import LLM
from ..world.store import Store
from . import context as cx
from .prompts import agent_system, background_system, batch_system, exchange_system, user_message
from .schemas import AgentTurn, BackgroundTurn, BatchTurn, Exchange


def on_stage_context(store: Store, cid: str, pc_id: str, scene: dict | None, this_turn: list[str],
                     addressed: bool, impact: dict | None = None) -> dict:
    me = store.character(cid)
    present = [c for c in (scene or {}).get("present", []) if c != cid and store.character(c)]
    if pc_id not in present and pc_id != cid:
        present.append(pc_id)
    terms = cx.terms_for(store, present, this_turn)
    pc = store.character(pc_id)
    return {
        "you": cx.dossier(store, me),
        "your_goals": cx.goals_view(store, cid),
        "scene": cx.scene_view(store, scene),
        "present": [cx.public_card(store, store.character(c), cid) for c in present],
        "player_id": pc_id,
        "player_name": pc["name"] if pc else "",
        "relationships": cx.relationships_view(store, cid, 8),
        "beliefs": cx.belief_view(store, cid, terms, 10),
        "memories": cx.memory_view(store, cid, terms, 8),
        "recent": cx.recent_lines(store, scene, 10),
        "this_turn": this_turn,
        "addressed": addressed,
        "impact": impact,
    }


async def act_on_stage(llm: LLM, store: Store, cid: str, pc_id: str, scene: dict | None,
                       this_turn: list[str], addressed: bool, impact: dict | None = None) -> AgentTurn:
    me = store.character(cid)
    ctx = on_stage_context(store, cid, pc_id, scene, this_turn, addressed, impact)
    hint = ""
    if impact:
        hint = (f"\nThe player character's words just landed on you with force {impact.get('value', 0):+d} "
                f"({impact.get('note', '')}). Let that weigh on your reply, but you still choose freely.")
    return await llm.call("agent", agent_system(me["name"], clock.format_date(store.now)) + hint,
                          user_message(ctx), AgentTurn, agent_id=cid)


def background_context(store: Store, cid: str, span: str) -> dict:
    me = store.character(cid)
    nearby = [c for c in store.characters(alive=True, location_id=me.get("location_id")) if c["id"] != cid]
    terms = [me["name"]] + [g["text"] for g in store.goals(cid)][:3]
    return {
        "you": cx.dossier(store, me),
        "your_goals": cx.goals_view(store, cid),
        "where": cx.where(store, me.get("location_id"), me["status"].get("sublocation")),
        "date": clock.format_date(store.now),
        "span": span,
        "nearby": [{"id": c["id"], "name": c["name"]} for c in nearby[:10]],
        "relationships": cx.relationships_view(store, cid, 6),
        "beliefs": cx.belief_view(store, cid, terms, 8),
        "memories": cx.memory_view(store, cid, terms, 6),
        "known_locations": cx.known_locations(store, 40),
    }


async def act_background(llm: LLM, store: Store, cid: str, span: str) -> BackgroundTurn:
    me = store.character(cid)
    return await llm.call("background", background_system(me["name"], span),
                          user_message(background_context(store, cid, span)), BackgroundTurn, agent_id=cid)


async def act_batch(llm: LLM, store: Store, cids: list[str], span: str) -> BatchTurn:
    people = []
    for cid in cids:
        me = store.character(cid)
        d = cx.dossier(store, me)
        for k in ("possessions", "skills", "appearance"):
            d.pop(k, None)
        people.append({
            "id": cid, "name": me["name"], "dossier": d, "goals": cx.goals_view(store, cid)[:4],
            "where": cx.where(store, me.get("location_id"), me["status"].get("sublocation")),
            "nearby": [c["id"] for c in store.characters(alive=True, location_id=me.get("location_id"))
                       if c["id"] != cid][:6],
            "recent_memories": cx.memory_view(store, cid, [me["name"]], 3),
        })
    ctx = {"date": clock.format_date(store.now), "span": span, "people": people,
           "known_locations": cx.known_locations(store, 40)}
    return await llm.call("batch", batch_system(span), user_message(ctx), BatchTurn)


async def exchange(llm: LLM, store: Store, a: str, b: str, reason: str) -> Exchange:
    ca, cb = store.character(a), store.character(b)

    def side(me: dict, other: dict) -> dict:
        d = cx.dossier(store, me)
        d.pop("possessions", None)
        return {**d, "goals": cx.goals_view(store, me["id"])[:4],
                "feelings_about_other": cx.public_card(store, other, me["id"]).get("your_relation"),
                "memories": cx.memory_view(store, me["id"], [other["name"]], 4)}

    ctx = {"date": clock.format_date(store.now),
           "place": cx.where(store, ca.get("location_id"), ca["status"].get("sublocation")),
           "reason": reason, "a": side(ca, cb), "b": side(cb, ca)}
    return await llm.call("exchange", exchange_system(), user_message(ctx), Exchange, agent_id=a)

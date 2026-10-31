"""GM Resolve: interpret the player's input into a structured, adjudicable intent.

Routed to Hermes by default (it may consult the nemothrones plugin tools and
WOIAF), falling back to inference.local.
"""

from __future__ import annotations

from ...llm import LLM
from ...world.store import Store
from .. import context as cx
from ..prompts import gm_system, user_message
from ..schemas import Resolve


def gm_context(store: Store, pc_id: str, scene: dict | None, terms: list[str]) -> dict:
    pc = store.character(pc_id)
    present = [c for c in (scene or {}).get("present", []) if c != pc_id]
    cards = [cx.public_card(store, store.character(c), pc_id) for c in present if store.character(c)]
    for card in cards:
        other = store.character(card["id"])
        card["skills"] = other["attrs"].get("skills", {})
    me = cx.dossier(store, pc)
    me.pop("secrets", None)
    return {
        "scene": cx.scene_view(store, scene),
        "player": me,
        "player_goals": cx.goals_view(store, pc_id),
        "present": cards,
        "recent": cx.recent_lines(store, scene, 12),
        "player_beliefs": cx.belief_view(store, pc_id, terms, 8),
        "player_memories": cx.memory_view(store, pc_id, terms, 6),
        "known_locations": cx.known_locations(store, 40),
        "sublocations_here": ((store.location((scene or {}).get("location_id")) or {}).get("data") or {}).get("sublocations", []),
    }


async def resolve(llm: LLM, store: Store, pc_id: str, scene: dict | None, text: str) -> Resolve:
    terms = cx.terms_for(store, (scene or {}).get("present", []), [text])
    ctx = gm_context(store, pc_id, scene, terms)
    return await llm.call("gm", gm_system(), user_message(ctx, text), Resolve, agent_id=pc_id)

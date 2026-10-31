"""The Narrator: second-person prose of what the player character perceives, plus
3-5 precomputed options (so choosing a number skips the GM call)."""

from __future__ import annotations

from ...config import Settings
from ...llm import LLM
from ...world.store import Store
from .. import context as cx
from ..prompts import narrator_system, user_message
from ..schemas import Narration


def narrator_context(store: Store, pc_id: str, scene: dict | None, perception: list[str]) -> dict:
    pc = store.character(pc_id)
    present = [c for c in (scene or {}).get("present", []) if c != pc_id and store.character(c)]
    terms = cx.terms_for(store, present, perception)
    me = cx.dossier(store, pc)
    me.pop("secrets", None)
    return {
        "scene": cx.scene_view(store, scene),
        "player": me,
        "player_goals": cx.goals_view(store, pc_id),
        "present": [cx.public_card(store, store.character(c), pc_id) for c in present],
        "perception": perception,
        "recent": cx.recent_lines(store, scene, 8),
        "player_beliefs": cx.belief_view(store, pc_id, terms, 6),
        "sublocations_here": ((store.location((scene or {}).get("location_id")) or {}).get("data") or {}).get("sublocations", []),
        "known_locations": cx.known_locations(store, 25),
    }


async def narrate(llm: LLM, store: Store, settings: Settings, pc_id: str, scene: dict | None,
                  perception: list[str]) -> Narration:
    pc = store.character(pc_id)
    ctx = narrator_context(store, pc_id, scene, perception)
    return await llm.call("narrator", narrator_system(pc["name"], settings.game.verbosity),
                          user_message(ctx), Narration, agent_id=pc_id)


def fallback(perception: list[str]) -> Narration:
    """Used when no narrator model is reachable: the bare facts, still playable."""
    return Narration(prose=" ".join(perception) or "Nothing stirs.", options=[])


def ensure_lines(prose: str, lines: list[str]) -> str:
    """Warden check: every spoken line must reach the player verbatim."""
    missing = [ln for ln in lines if ln and ln not in prose]
    if not missing:
        return prose
    return prose + "\n\n" + "\n".join(missing)

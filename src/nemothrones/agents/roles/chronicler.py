"""The Chronicler: the death Chronicle (and, from M4, reflections and the journal).

Post-mortem, the Chronicler may read the player character's fate to compare canon
with the history the player made, so it is one of the few modules allowed to
import ``world.fate``.
"""

from __future__ import annotations

from ...engine import clock
from ...llm import LLM
from ...world.fate import fate_of
from ...world.store import Store
from .. import context as cx
from ..prompts import chronicler_system, user_message
from ..schemas import Chronicle


def chronicle_context(store: Store, pc_id: str) -> dict:
    pc = store.character(pc_id)
    mems = store.conn.execute(
        "SELECT t, text FROM memories WHERE owner_id=? AND importance>=4 ORDER BY t", (pc_id,)).fetchall()
    world = store.conn.execute(
        "SELECT t, summary FROM events WHERE kind IN ('character_died','outcome','hook') ORDER BY id DESC LIMIT 25").fetchall()
    ledger = [f"{ce['title']}: {ce['status']}" for ce in store.canon_events()]
    return {
        "player": cx.dossier(store, pc),
        "start": clock.format_date(store.meta_get("start_t", store.now), with_time=False),
        "death": {"date": clock.format_date(pc.get("death_t") or store.now, with_time=False),
                  "cause": pc["status"].get("cause_of_death", "unknown")},
        "life_events": [f"({clock.short_date(t)}) {txt}" for t, txt in mems][-40:],
        "world": [f"({clock.short_date(t)}) {s}" for t, s in reversed(world) if s],
        "canon_fate_of_player": fate_of(store, pc_id) or "",
        "canon_ledger": ledger[:30],
    }


async def chronicle(llm: LLM, store: Store, pc_id: str) -> Chronicle:
    return await llm.call("chronicler", chronicler_system(), user_message(chronicle_context(store, pc_id)),
                          Chronicle, agent_id=pc_id)

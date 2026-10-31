"""Director-only access to fate (what canon holds after the start date).

This is the ONLY module that reads the ``fate`` table. Agent, narrator and GM prompt
builders must never import it; ``tests/unit/test_fate_isolation.py`` enforces that
statically and by inspecting every prompt the engine builds.
"""

from __future__ import annotations

import json

from .store import Store


def record_fate(store: Store, character_id: str, text: str, source: str = "seed") -> None:
    store.emit("fate_recorded", {"character_id": character_id, "data": {"text": text, "source": source}},
               visibility="director", summary="fate recorded")


def read_fates(store: Store) -> dict[str, str]:
    rows = store.conn.execute("SELECT character_id, data FROM fate").fetchall()
    return {r[0]: json.loads(r[1]).get("text", "") for r in rows}


def fate_of(store: Store, character_id: str) -> str | None:
    r = store.conn.execute("SELECT data FROM fate WHERE character_id=?", (character_id,)).fetchone()
    return json.loads(r[0]).get("text") if r else None

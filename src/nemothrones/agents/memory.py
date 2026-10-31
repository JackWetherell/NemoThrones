"""Agent memory: write episodic memories; retrieve by recency x importance x relevance.

Relevance is SQLite FTS5 BM25 over the memory text and tagged entities, so no
embedding model is needed (inference.local serves a single chat model).
"""

from __future__ import annotations

import math

from ..engine import clock
from ..world.store import Store

RECENCY_HALF_LIFE_MIN = 30 * clock.MIN_PER_DAY


def remember(store: Store, owner: str, text: str, *, kind: str = "observation", importance: float = 3,
             entities: list[str] | None = None, event_id: int | None = None) -> int:
    return store.emit("memory_added", {"owner": owner, "kind": kind, "text": text[:1200],
                                       "importance": float(max(1, min(10, importance))),
                                       "entities": entities or [], "event_id": event_id},
                      visibility="private", actors=[owner])


def retrieve(store: Store, owner: str, terms: list[str], k: int = 8) -> list[dict]:
    now = store.now
    candidates: dict[int, dict] = {}
    matched = store.search_memories(owner, terms, limit=30)
    ranks = [m["rank"] for m in matched]
    lo, hi = (min(ranks), max(ranks)) if ranks else (0.0, 0.0)
    for m in matched:
        # bm25() is lower-is-better (negative); map to [0.3, 1].
        rel = 1.0 if hi == lo else 0.3 + 0.7 * (hi - m["rank"]) / (hi - lo)
        m["_rel"] = rel
        candidates[m["id"]] = m
    for m in store.memories(owner, limit=12):
        m.setdefault("_rel", 0.0)
        candidates.setdefault(m["id"], m)
    scored = []
    for m in candidates.values():
        age = max(0, now - m["t"])
        recency = math.exp(-math.log(2) * age / RECENCY_HALF_LIFE_MIN)
        score = 0.45 * m["_rel"] + 0.35 * recency + 0.20 * (m["importance"] / 10.0)
        scored.append((score, m))
    scored.sort(key=lambda x: (-x[0], -x[1]["t"]))
    top = [m for _, m in scored[:k]]
    top.sort(key=lambda m: m["t"])
    return top


def relevant_beliefs(store: Store, holder: str, terms: list[str], k: int = 10) -> list[dict]:
    out = {b["id"]: b for b in store.search_beliefs(holder, terms, limit=k)}
    for b in store.beliefs(holder, limit=6):
        if len(out) >= k:
            break
        out.setdefault(b["id"], b)
    return sorted(out.values(), key=lambda b: b["learned_t"])

"""Counter-based, save-friendly dice.

Each draw is ``sha256(seed:stream:counter)`` mapped to [0, 1). Counters are
persisted with the save, so a loaded game continues the exact same sequence, and a
recorded session replays bit-for-bit. Streams separate subsystems so that adding a
roll in one place does not reshuffle every other outcome.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


class Dice:
    def __init__(self, seed: int, counters: dict[str, int] | None = None):
        self.seed = int(seed)
        self.counters: dict[str, int] = dict(counters or {})
        self._forced: dict[str, list[float]] = {}

    def uniform(self, stream: str) -> float:
        forced = self._forced.get(stream)
        if forced:
            self.counters[stream] = self.counters.get(stream, 0) + 1
            return forced.pop(0)
        n = self.counters.get(stream, 0)
        self.counters[stream] = n + 1
        h = hashlib.sha256(f"{self.seed}:{stream}:{n}".encode()).digest()
        return int.from_bytes(h[:8], "big") / 2**64

    def pick(self, stream: str, probs: Sequence[float]) -> tuple[int, float]:
        """Sample an index from ``probs``; returns (index, u)."""
        u = self.uniform(stream)
        acc = 0.0
        for i, p in enumerate(probs):
            acc += p
            if u < acc:
                return i, u
        return len(probs) - 1, u

    def chance(self, stream: str, p: float) -> bool:
        return self.uniform(stream) < p

    def force(self, stream: str, values: Sequence[float]) -> None:
        """Test hook: the next draws on ``stream`` return these values."""
        self._forced.setdefault(stream, []).extend(float(v) for v in values)

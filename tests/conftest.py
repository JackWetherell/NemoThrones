from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nemothrones.config import Settings
from nemothrones.engine.game import Game
from nemothrones.llm import LLM
from nemothrones.world.pack import builtin_pack_path, load_pack


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(home=tmp_path / "home")
    s.force_backend("mock")
    s.game.color = False
    s.dev = True
    return s


@pytest.fixture
def pack():
    return load_pack(builtin_pack_path("winterfell_298"))


@pytest.fixture
def make_game(settings, pack, tmp_path):
    games = []

    def _make(player: str = "eddard_stark", seed: int = 42, name: str = "g.db", llm: LLM | None = None) -> Game:
        g = Game.create(settings, tmp_path / name, pack, player, seed=seed, llm=llm or LLM(settings))
        games.append(g)
        return g

    yield _make
    for g in games:
        try:
            asyncio.run(g.close())
        except Exception:
            pass


def run(coro):
    return asyncio.run(coro)

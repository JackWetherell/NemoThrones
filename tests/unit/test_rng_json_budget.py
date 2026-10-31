import pytest

from nemothrones.engine.rng import Dice
from nemothrones.llm.budget import Budget, BudgetExceeded
from nemothrones.llm.jsonutil import extract_json


def test_dice_deterministic_and_resumable():
    a = Dice(42)
    seq = [a.uniform("player") for _ in range(5)]
    b = Dice(42)
    assert [b.uniform("player") for _ in range(5)] == seq
    c = Dice(42, {"player": 3})  # a loaded save continues the same sequence
    assert [c.uniform("player") for _ in range(2)] == seq[3:]
    assert Dice(43).uniform("player") != seq[0]
    assert Dice(42).uniform("npc") != seq[0]  # streams are independent


def test_dice_force_and_pick():
    d = Dice(1)
    d.force("player", [0.995])
    idx, u = d.pick("player", [0.5, 0.49, 0.01])
    assert (idx, u) == (2, 0.995)
    assert d.counters["player"] == 1


@pytest.mark.parametrize("text", [
    '{"a": 1}', '```json\n{"a": 1}\n```', 'Here you go: {"a": 1} hope that helps',
    'noise {"bad": } then {"a": 1}', '{"a": 1, "s": "a } brace"}',
])
def test_extract_json(text):
    assert extract_json(text)["a"] == 1


def test_extract_json_fails_cleanly():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_budget_pressure_and_caps():
    b = Budget(0.14, 0.42, warn_usd=0.001, hard_usd=0.002)
    assert b.pressure() == 0
    b.charge("agent", 10_000, 0)  # $0.0014
    assert 0 < b.pressure() < 1
    b.check("narrator")
    b.charge("agent", 10_000, 0)
    with pytest.raises(BudgetExceeded):
        b.check("narrator")

import math

import pytest

from nemothrones.engine import odds


@pytest.mark.parametrize("n", [2, 3, 4, 5])
@pytest.mark.parametrize("margin", [-20, -6, -1, 0, 3, 9, 25])
def test_tiers_normalised_and_floored(n, margin):
    p = odds.apply_floor(odds.tier_probs(margin, n))
    assert math.isclose(sum(p), 1.0, abs_tol=1e-9)
    assert all(x >= odds.PROB_FLOOR - 1e-12 for x in p)


def test_monotone_in_skill():
    best = [odds.tier_probs(m, 4)[0] for m in range(-12, 13)]
    worst = [odds.tier_probs(m, 4)[-1] for m in range(-12, 13)]
    assert best == sorted(best)
    assert worst == sorted(worst, reverse=True)


def test_two_tier_is_a_normal_cdf():
    assert math.isclose(odds.tier_probs(0, 2)[0], 0.5)
    assert math.isclose(odds.tier_probs(odds.SIGMA, 2)[0], odds.phi(1.0))


def test_floor_redistributes_exactly():
    p = odds.apply_floor([0.999, 0.0005, 0.0005])
    assert math.isclose(sum(p), 1.0)
    assert p[1] == p[2] == odds.PROB_FLOOR


def test_modifier_vocabulary_and_caps():
    mods = odds.resolve_modifiers([
        {"name": "surprise", "value": 99},          # vocabulary: engine value wins
        {"name": "custom", "value": 9, "why": "x"},  # capped to +3
        {"name": "custom", "value": 3, "why": "y"},  # total custom capped at +5
        {"name": "custom", "value": 3, "why": "z"},  # no room left
        {"name": "surprise"},                         # duplicate ignored
    ])
    assert [(m.name, m.value) for m in mods] == [("surprise", 3), ("custom", 3), ("custom", 2)]


def test_check_table_and_lethal_risk():
    t = odds.compute_check(actor="Ned", skill="prowess", skill_value=14, difficulty=14,
                           modifiers=[], labels=["Win", "Hurt", "Die"], lethal=[False, False, True])
    assert math.isclose(sum(t.probs), 1.0)
    assert t.lethal_risk == t.probs[2]
    assert "Win" in t.render("exact") and "likely" not in t.render("exact")
    assert ":" in t.render("fuzzy")
    assert t.render("off") == ""
    assert "difficulty 14" in t.explain()


def test_chance_normalises_and_floors():
    t = odds.compute_chance(weights=[0, 1], labels=["Live", "Die"], lethal=[False, True])
    assert t.probs == pytest.approx([0.01, 0.99])

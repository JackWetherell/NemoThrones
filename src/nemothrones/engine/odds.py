"""The odds engine: LLMs set the stakes, the engine sets the probabilities.

Skill checks use a normal performance model. Performance ``X ~ N(mu, sigma)`` with
``mu = skill + sum(modifiers) - difficulty``. Outcome tiers (best -> worst) are
separated by evenly spaced cut points symmetric about 0, so tier probabilities are
CDF differences: always normalised, and monotone in skill. Every tier is then
floored at 1% (nothing is ever certain in Westeros), and the full derivation is kept
for ``/why``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

SKILLS = ("martial", "diplomacy", "intrigue", "stewardship", "learning", "prowess")
SIGMA = 4.0
SPACING = 4.0
PROB_FLOOR = 0.01
CUSTOM_MOD_CAP = 3
CUSTOM_TOTAL_CAP = 5

# Bounded modifier vocabulary. The LLM picks names; the engine owns the values.
MODIFIERS: dict[str, int] = {
    "wounded": -3,
    "grievously_wounded": -6,
    "exhausted": -2,
    "drunk": -2,
    "afraid": -2,
    "outnumbered": -3,
    "badly_outnumbered": -6,
    "superior_numbers": 3,
    "unarmed": -4,
    "unarmored": -2,
    "armored": 2,
    "superior_arms": 2,
    "valyrian_steel": 2,
    "mounted": 2,
    "surprise": 3,
    "ambushed": -3,
    "home_ground": 1,
    "favorable_terrain": 2,
    "unfavorable_terrain": -2,
    "darkness": -1,
    "ally_support": 2,
    "strong_bond": 2,
    "hostile_relation": -2,
    "deep_distrust": -3,
    "rank_advantage": 2,
    "rank_disadvantage": -2,
    "good_argument": 2,
    "weak_argument": -2,
    "appeals_to_values": 2,
    "offends_values": -3,
    "bribe": 2,
    "leverage": 3,
    "threat_backed_by_force": 2,
    "reputation": 1,
    "infamy": -1,
    "preparation": 2,
    "haste": -2,
    "expert_tools": 1,
}

DIFFICULTY_LABELS = [
    (4, "trivial"), (7, "easy"), (10, "moderate"), (13, "hard"),
    (16, "very hard"), (19, "heroic"), (22, "near-impossible"),
]


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def apply_floor(probs: list[float], floor: float = PROB_FLOOR) -> list[float]:
    """Floor every probability at ``floor`` and renormalise the rest exactly."""
    n = len(probs)
    if n == 0:
        return []
    if floor * n >= 1.0:
        return [1.0 / n] * n
    probs = [max(0.0, float(p)) for p in probs]
    fixed = [False] * n
    while True:
        free_mass = 1.0 - floor * sum(fixed)
        free_sum = sum(p for p, f in zip(probs, fixed, strict=True) if not f)
        if free_sum <= 0:
            k = n - sum(fixed)
            out = [floor if f else free_mass / k for f in fixed]
            return out
        out = [floor if f else p * free_mass / free_sum for p, f in zip(probs, fixed, strict=True)]
        newly = [i for i in range(n) if not fixed[i] and out[i] < floor]
        if not newly:
            return out
        for i in newly:
            fixed[i] = True


def tier_probs(margin: float, n_tiers: int, sigma: float = SIGMA, spacing: float = SPACING) -> list[float]:
    """Probabilities of ``n_tiers`` ordered outcomes (best first) for a given margin."""
    if n_tiers < 2:
        return [1.0]
    cuts = [((n_tiers - 2) / 2.0 - k) * spacing for k in range(n_tiers - 1)]  # descending
    surv = [phi((margin - c) / sigma) for c in cuts]  # P(X >= c), increasing
    probs = [surv[0]] + [surv[k] - surv[k - 1] for k in range(1, len(surv))] + [1.0 - surv[-1]]
    return [max(0.0, p) for p in probs]


@dataclass
class Modifier:
    name: str
    value: int
    why: str = ""


@dataclass
class OddsTable:
    kind: str  # check | opposed | chance
    labels: list[str]
    probs: list[float]
    lethal: list[bool]
    derivation: dict = field(default_factory=dict)

    @property
    def lethal_risk(self) -> float:
        return sum(p for p, dead in zip(self.probs, self.lethal, strict=True) if dead)

    def render(self, mode: str = "exact") -> str:
        if mode == "off":
            return ""
        parts = []
        for label, p in zip(self.labels, self.probs, strict=True):
            parts.append(f"{label} {fmt_pct(p)}" if mode == "exact" else f"{label}: {fuzzy(p)}")
        return " · ".join(parts)

    def explain(self) -> str:
        d = self.derivation
        if self.kind == "chance":
            return f"chance roll · weights {d.get('weights')} → floored and normalised"
        mods = ", ".join(f"{m['name']} {m['value']:+d}" for m in d.get("modifiers", [])) or "none"
        opp = f" vs {d['opponent']} {d['opponent_skill']} {d['difficulty']}" if self.kind == "opposed" else f" vs difficulty {d['difficulty']} ({difficulty_label(d['difficulty'])})"
        return (f"{d.get('actor', 'actor')} {d['skill']} {d['skill_value']}{opp}; "
                f"modifiers: {mods}; μ = {d['mu']:+.1f}, σ = {d['sigma']:.0f}; "
                f"{len(self.labels)} tiers, spacing {d['spacing']:.0f}, floor {PROB_FLOOR:.0%}")


def fmt_pct(p: float) -> str:
    v = p * 100
    return f"{v:.0f}%" if v >= 1.5 else "1%"


def fuzzy(p: float) -> str:
    if p >= 0.85:
        return "almost certain"
    if p >= 0.65:
        return "likely"
    if p >= 0.40:
        return "even odds"
    if p >= 0.20:
        return "unlikely"
    if p >= 0.07:
        return "slim"
    return "remote"


def difficulty_label(d: float) -> str:
    label = DIFFICULTY_LABELS[0][1]
    for threshold, name in DIFFICULTY_LABELS:
        if d >= threshold:
            label = name
    return label


def resolve_modifiers(raw: list[dict] | None) -> list[Modifier]:
    """Map LLM-proposed modifiers onto the vocabulary; cap custom ones."""
    out: list[Modifier] = []
    custom_total = 0
    seen: set[str] = set()
    for m in raw or []:
        name = str(m.get("name", "")).strip().lower().replace(" ", "_")
        if not name or name in seen:
            continue
        if name in MODIFIERS:
            out.append(Modifier(name, MODIFIERS[name], str(m.get("why", ""))))
            seen.add(name)
        elif name == "custom" or name not in MODIFIERS:
            try:
                v = int(round(float(m.get("value", 0))))
            except (TypeError, ValueError):
                continue
            v = max(-CUSTOM_MOD_CAP, min(CUSTOM_MOD_CAP, v))
            room = CUSTOM_TOTAL_CAP - abs(custom_total)
            if v == 0 or room <= 0:
                continue
            v = max(-room, min(room, v))
            custom_total += v
            label = str(m.get("why") or name)[:60]
            out.append(Modifier("custom", v, label))
    return out


def compute_check(*, actor: str, skill: str, skill_value: float, difficulty: float,
                  modifiers: list[Modifier], labels: list[str], lethal: list[bool],
                  kind: str = "check", opponent: str | None = None,
                  opponent_skill: str | None = None) -> OddsTable:
    mod_sum = sum(m.value for m in modifiers)
    mu = float(skill_value) + mod_sum - float(difficulty)
    probs = apply_floor(tier_probs(mu, len(labels)))
    deriv = {
        "actor": actor, "skill": skill, "skill_value": skill_value,
        "difficulty": difficulty, "modifiers": [m.__dict__ for m in modifiers],
        "mu": mu, "sigma": SIGMA, "spacing": SPACING,
    }
    if kind == "opposed":
        deriv.update({"opponent": opponent, "opponent_skill": opponent_skill})
    return OddsTable(kind, list(labels), probs, list(lethal), deriv)


def compute_chance(*, weights: list[float], labels: list[str], lethal: list[bool],
                   bias: list[float] | None = None) -> OddsTable:
    w = [max(0.0, float(x)) for x in weights]
    if bias:
        w = [x * max(0.0, b) for x, b in zip(w, bias, strict=False)] + w[len(bias):]
    total = sum(w)
    probs = [x / total for x in w] if total > 0 else [1.0 / len(w)] * len(w)
    probs = apply_floor(probs)
    return OddsTable("chance", list(labels), probs, list(lethal),
                     {"weights": [round(x, 3) for x in weights], "bias": bias})

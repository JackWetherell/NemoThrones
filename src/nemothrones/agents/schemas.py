"""Output contracts for every LLM role. Lenient on input (models drift), strict on
meaning: the Warden re-validates every id, number and effect before the engine
applies anything.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ effects
class Effect(Lenient):
    """Engine-applied consequence. ``type`` selects which fields matter."""

    type: Literal["impact", "relationship", "wound", "death", "belief", "move", "goal", "status"] = "impact"
    target: str | None = None
    a: str | None = None
    b: str | None = None
    holder: str | None = None
    value: int = 0
    opinion: int = 0
    trust: int = 0
    fear: int = 0
    respect: int = 0
    severity: int = 1
    cause: str = ""
    subject: str = ""
    content: str = ""
    to: str | None = None
    text: str = ""
    note: str = ""

    @field_validator("value", "opinion", "trust", "fear", "respect", "severity", mode="before")
    @classmethod
    def _ints(cls, v):
        return _int(v)


class Outcome(Lenient):
    label: str
    description: str = ""
    lethal: bool = False
    effects: list[Effect] = Field(default_factory=list)

    @field_validator("effects", mode="before")
    @classmethod
    def _effects(cls, v):
        return [e for e in _as_list(v) if isinstance(e, dict)]


class Adjudication(Lenient):
    kind: Literal["check", "opposed", "chance"] = "check"
    skill: str = "prowess"
    difficulty: float = 10
    opponent: str | None = None
    opponent_skill: str | None = None
    modifiers: list[dict] = Field(default_factory=list)
    weights: list[float] = Field(default_factory=list)
    outcomes: list[Outcome] = Field(default_factory=list)

    @field_validator("modifiers", mode="before")
    @classmethod
    def _mods(cls, v):
        out = []
        for m in _as_list(v):
            if isinstance(m, str):
                out.append({"name": m})
            elif isinstance(m, dict):
                out.append(m)
        return out

    @field_validator("weights", mode="before")
    @classmethod
    def _weights(cls, v):
        out = []
        for w in _as_list(v):
            try:
                out.append(float(w))
            except (TypeError, ValueError):
                out.append(1.0)
        return out


ResolveKind = Literal["speak", "act", "move", "wait", "write", "scheme", "clarify", "ooc"]


class Resolve(Lenient):
    """GM interpretation of one player input (also used for precomputed options)."""

    kind: ResolveKind = "act"
    action_summary: str = ""
    utterance: str | None = None
    targets: list[str] = Field(default_factory=list)
    destination: str | None = None
    duration_minutes: int = 2
    adjudication: Adjudication | None = None
    clarification: str | None = None
    mentions: list[str] = Field(default_factory=list)  # canon names not in context

    @field_validator("targets", "mentions", mode="before")
    @classmethod
    def _lists(cls, v):
        return [str(x) for x in _as_list(v) if x]

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _dur(cls, v):
        return max(1, _int(v, 2))

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        v = str(v or "act").lower().strip()
        aliases = {"say": "speak", "talk": "speak", "dialogue": "speak", "action": "act", "do": "act",
                   "travel": "move", "go": "move", "rest": "wait", "letter": "write", "plan": "scheme",
                   "question": "clarify"}
        return aliases.get(v, v)


class Option(Lenient):
    label: str
    resolve: Resolve


# ------------------------------------------------------------------ agents
class AgentAction(Lenient):
    type: Literal["none", "gesture", "move", "attack", "give", "leave", "write", "scheme", "other"] = "other"
    target: str | None = None
    detail: str = ""
    risky: bool = False
    to: str | None = None  # destination for move/leave

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v):
        v = str(v or "other").lower().strip()
        return v if v in {"none", "gesture", "move", "attack", "give", "leave", "write", "scheme", "other"} else "other"


class OpinionChange(Lenient):
    of: str
    opinion: int = 0
    trust: int = 0
    why: str = ""

    @field_validator("opinion", "trust", mode="before")
    @classmethod
    def _ints(cls, v):
        return _int(v)


class AgentTurn(Lenient):
    thought: str = ""
    speech: str | None = None
    speak_to: str | None = None
    action: AgentAction | None = None
    plan_update: str | None = None
    opinion_changes: list[OpinionChange] = Field(default_factory=list)
    remember: str | None = None

    @field_validator("opinion_changes", mode="before")
    @classmethod
    def _oc(cls, v):
        return [x for x in _as_list(v) if isinstance(x, dict) and x.get("of")]

    @field_validator("speech", mode="before")
    @classmethod
    def _speech(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None


class Letter(Lenient):
    to: str
    content: str


class BackgroundTurn(Lenient):
    id: str | None = None
    summary: str = ""
    location: str | None = None
    sublocation: str | None = None
    seek: str | None = None
    letters: list[Letter] = Field(default_factory=list)
    plan_update: str | None = None
    importance: int = 3

    @field_validator("letters", mode="before")
    @classmethod
    def _letters(cls, v):
        return [x for x in _as_list(v) if isinstance(x, dict) and x.get("to") and x.get("content")]

    @field_validator("importance", mode="before")
    @classmethod
    def _imp(cls, v):
        return max(1, min(10, _int(v, 3)))


class BatchTurn(Lenient):
    people: list[BackgroundTurn] = Field(default_factory=list)


class Exchange(Lenient):
    summary: str = ""
    a_remembers: str = ""
    b_remembers: str = ""
    a_opinion_of_b: int = 0
    b_opinion_of_a: int = 0
    agreement: str | None = None

    @field_validator("a_opinion_of_b", "b_opinion_of_a", mode="before")
    @classmethod
    def _ints(cls, v):
        return _int(v)


# ---------------------------------------------------------------- managers
class Narration(Lenient):
    prose: str = ""
    options: list[Option] = Field(default_factory=list)

    @field_validator("options", mode="before")
    @classmethod
    def _opts(cls, v):
        out = []
        for o in _as_list(v):
            if isinstance(o, dict) and o.get("label"):
                if "resolve" not in o:  # tolerate a flattened option
                    o = {"label": o["label"], "resolve": {k: val for k, val in o.items() if k != "label"}}
                out.append(o)
        return out


class DirectorBeat(Lenient):
    hook: str | None = None
    arrivals: list[str] = Field(default_factory=list)
    departures: list[str] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    tension: float = 0.3
    note: str = ""

    @field_validator("arrivals", "departures", mode="before")
    @classmethod
    def _lists(cls, v):
        return [str(x) for x in _as_list(v) if x]

    @field_validator("effects", mode="before")
    @classmethod
    def _effects(cls, v):
        return [e for e in _as_list(v) if isinstance(e, dict)]


class Chronicle(Lenient):
    title: str = "The Chronicle"
    text: str = ""


class LoreAnswer(Lenient):
    answer: str = ""
    sources: list[str] = Field(default_factory=list)

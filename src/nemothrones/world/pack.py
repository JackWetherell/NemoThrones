"""World packs: the starting state of a game.

A pack is either hand-authored (``data/seed_packs/*.yaml``, used offline and as the
test fixture) or generated from WOIAF by the lore pipeline (an *era pack*). Both use
this format. ``fate`` fields are Director-only and are split off at seeding time.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..engine.odds import SKILLS


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Knowledge(Lenient):
    subject: str
    content: str
    confidence: float = 1.0
    is_true: bool = True
    source: str = "common knowledge"


class CharacterSpec(Lenient):
    id: str
    name: str
    wiki_title: str | None = None
    house: str | None = None
    born_year: int | None = None
    born_moon: int | None = None
    location: str | None = None
    sublocation: str = ""
    importance: float = 0.5
    canon: bool = True
    titles: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    gender: str = ""
    culture: str = ""
    faith: str = ""
    appearance: str = ""
    traits: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    fears: list[str] = Field(default_factory=list)
    voice: str = ""
    summary: str = ""
    skills: dict[str, int] = Field(default_factory=dict)
    health: str = "hale"
    possessions: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    ambitions: list[str] = Field(default_factory=list)
    plans: list[str] = Field(default_factory=list)
    knowledge: list[Knowledge] = Field(default_factory=list)
    activity: str = ""
    fate: str | None = None  # Director-only: what canon holds for them after the start date

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: dict[str, int]) -> dict[str, int]:
        out = {s: 8 for s in SKILLS}
        for k, val in (v or {}).items():
            if k in out:
                try:
                    out[k] = max(0, min(20, int(val)))
                except (TypeError, ValueError):
                    pass
        return out


class HouseSpec(Lenient):
    id: str
    name: str
    seat: str | None = None
    liege: str | None = None
    head: str | None = None
    words: str | None = None
    sigil: str | None = None
    region: str | None = None


class LocationSpec(Lenient):
    id: str
    name: str
    region: str | None = None
    kind: str | None = None
    x: float | None = None  # rough game-map miles, east
    y: float | None = None  # rough game-map miles, south
    parent: str | None = None
    aliases: list[str] = Field(default_factory=list)
    sublocations: list[str] = Field(default_factory=list)
    has_maester: bool = False
    description: str = ""


class RouteSpec(Lenient):
    a: str
    b: str
    mode: str = "road"
    miles: float


class RelationshipSpec(Lenient):
    a: str
    b: str
    kind: str = "acquaintance"
    opinion: int = 0
    trust: int = 0
    fear: int = 0
    respect: int = 0
    notes: str = ""


class CanonEventSpec(Lenient):
    id: str
    year: int | None = None
    moon: int | None = None
    day: int | None = None
    title: str
    summary: str = ""
    participants: list[str] = Field(default_factory=list)
    location: str | None = None
    preconditions: list[dict] = Field(default_factory=list)
    drivers: list[str] = Field(default_factory=list)
    chance: bool = False
    wiki_ref: str | None = None


class OpeningSpec(Lenient):
    location: str
    sublocation: str = ""
    present: list[str] = Field(default_factory=list)
    situation: str = ""
    hook: str | None = None
    hook_arrivals: list[str] = Field(default_factory=list)


class WorldPack(Lenient):
    id: str
    title: str
    source: str = "seed"  # seed | woiaf
    year: int
    moon: int = 1
    day: int = 1
    hour: int = 9
    season: str = "summer"
    default_player: str | None = None
    characters: list[CharacterSpec] = Field(default_factory=list)
    houses: list[HouseSpec] = Field(default_factory=list)
    locations: list[LocationSpec] = Field(default_factory=list)
    routes: list[RouteSpec] = Field(default_factory=list)
    relationships: list[RelationshipSpec] = Field(default_factory=list)
    common_knowledge: list[Knowledge] = Field(default_factory=list)
    canon_events: list[CanonEventSpec] = Field(default_factory=list)
    openings: dict[str, OpeningSpec] = Field(default_factory=dict)  # per player id; "*" = default
    notes: str = ""

    def character(self, cid: str) -> CharacterSpec | None:
        return next((c for c in self.characters if c.id == cid), None)

    def opening_for(self, player_id: str) -> OpeningSpec:
        if player_id in self.openings:
            return self.openings[player_id]
        pc = self.character(player_id)
        if "*" in self.openings and pc and pc.location == self.openings["*"].location:
            return self.openings["*"]
        loc = pc.location if pc and pc.location else (self.locations[0].id if self.locations else "unknown")
        sub = pc.sublocation if pc else ""
        here = [c.id for c in self.characters
                if c.id != player_id and c.location == loc and c.sublocation == sub][:4]
        return OpeningSpec(location=loc, sublocation=sub, present=here,
                           situation=(f"You are {pc.activity}." if pc and pc.activity else ""))


def load_pack(path: str | Path) -> WorldPack:
    data = yaml.safe_load(Path(path).read_text())
    return WorldPack.model_validate(data)


def builtin_pack_path(name: str) -> Path:
    from importlib import resources

    return Path(str(resources.files("nemothrones.data").joinpath("seed_packs", f"{name}.yaml")))

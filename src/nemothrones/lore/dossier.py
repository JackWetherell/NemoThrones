"""Dossiers (past only -> agent record) and fates (fate only -> canon events).

Two separate LLM calls, so the dossier builder never sees the future. Hard facts
from the infobox (name, birth year) override whatever the model says.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..agents.prompts import CANON, user_message
from ..engine.odds import SKILLS
from .parse import ParsedPage
from .slicer import Sliced


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class KnowledgeOut(Lenient):
    subject: str
    content: str


class RelOut(Lenient):
    name: str
    kind: str = "acquaintance"
    opinion: int = 0
    trust: int = 0
    note: str = ""


def _list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


class DossierOut(Lenient):
    name: str = ""
    titles: list[str] = Field(default_factory=list)
    house: str | None = None
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
    location: str | None = None
    sublocation: str = ""
    activity: str = ""
    health: str = "hale"
    possessions: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    ambitions: list[str] = Field(default_factory=list)
    plans: list[str] = Field(default_factory=list)
    knowledge: list[KnowledgeOut] = Field(default_factory=list)
    relationships: list[RelOut] = Field(default_factory=list)

    @field_validator("titles", "traits", "values", "fears", "possessions", "secrets", "ambitions", "plans",
                     mode="before")
    @classmethod
    def _strs(cls, v):
        return [str(x) for x in _list(v) if x]

    @field_validator("knowledge", "relationships", mode="before")
    @classmethod
    def _dicts(cls, v):
        return [x for x in _list(v) if isinstance(x, dict)]

    @field_validator("skills", mode="before")
    @classmethod
    def _skills(cls, v):
        out = {}
        for k, val in (v or {}).items() if isinstance(v, dict) else []:
            if k in SKILLS:
                try:
                    out[k] = max(0, min(20, int(round(float(val)))))
                except (TypeError, ValueError):
                    pass
        return out


class FateEvent(Lenient):
    title: str
    year: int | None = None
    moon: int | None = None
    summary: str = ""
    participants: list[str] = Field(default_factory=list)
    location: str | None = None
    drivers: list[str] = Field(default_factory=list)
    preconditions: list[dict] = Field(default_factory=list)
    chance: bool = False

    @field_validator("participants", "drivers", mode="before")
    @classmethod
    def _strs(cls, v):
        return [str(x) for x in _list(v) if x]

    @field_validator("year", "moon", mode="before")
    @classmethod
    def _ints(cls, v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None


class FateOut(Lenient):
    summary: str = ""
    events: list[FateEvent] = Field(default_factory=list)

    @field_validator("events", mode="before")
    @classmethod
    def _ev(cls, v):
        return [x for x in _list(v) if isinstance(x, dict) and x.get("title")]


def dossier_system(name: str, as_of: str) -> str:
    return f"""You are a MAESTER of the Citadel writing the dossier for {name} as they are in {as_of}, for NemoThrones (a game in which an AI agent will play this person).
{CANON}
Use ONLY the INFOBOX and PAST text given (from A Wiki of Ice and Fire). They describe the person up to {as_of}. Never mention or hint at anything that happens later, even if you know it.
Describe them AS OF {as_of}: the titles they hold then, where they are, what they want, whom they love and hate, what they know, and secrets they keep.
- skills, 0 to 20 (8 = ordinary adult, 12 = trained, 16 = master, 19-20 = legendary): martial (command, tactics), diplomacy, intrigue, stewardship, learning, prowess (personal combat). Children score low.
- house: the WOIAF title of the house they belong to then, e.g. "House Stark", or null.
- location: the castle or city where they most plausibly are in {as_of}, by its usual name.
- knowledge: up to 6 things they know that matter (subject and content).
- relationships: up to 10 people that matter to them, with kind (e.g. father, sworn shield, rival), opinion and trust (-100..100).
- voice: how they speak, in a sentence. Do not quote the books.
Reply with ONLY one JSON object:
{{"name": "", "titles": [], "house": null, "gender": "", "culture": "", "faith": "", "appearance": "", "traits": [], "values": [], "fears": [], "voice": "", "summary": "their life up to {as_of}, 3-6 sentences", "skills": {{"martial": 8, "diplomacy": 8, "intrigue": 8, "stewardship": 8, "learning": 8, "prowess": 8}}, "location": null, "sublocation": "", "activity": "what they are doing at the start, a short gerund phrase", "health": "hale", "possessions": [], "secrets": [], "ambitions": [], "plans": [], "knowledge": [{{"subject": "", "content": ""}}], "relationships": [{{"name": "", "kind": "", "opinion": 0, "trust": 0, "note": ""}}]}}"""


def fate_system(name: str, as_of: str) -> str:
    return f"""You are the CHRONICLER of NemoThrones, preparing the Director's SECRET canon timeline. Nothing you write is shown to the player or to any character.
{CANON}
From the FATE text (A Wiki of Ice and Fire: what happens to {name} from {as_of} onward), extract up to 6 key canon events, in order. For each:
title; year (AC) and moon (1-12) if known, else null; summary (one or two sentences); participants (names); location (place name); drivers (whose choices cause it); preconditions (what must still be true for it to happen: {{"type": "alive", "who": name}} or {{"type": "at", "who": name, "where": place}}); chance (true if it hinges on luck, e.g. an accident or illness).
Also write summary: their canon fate in 2-3 sentences.
Reply with ONLY one JSON object: {{"summary": "", "events": [{{"title": "", "year": null, "moon": null, "summary": "", "participants": [], "location": null, "drivers": [], "preconditions": [], "chance": false}}]}}"""


# Infobox fields that cannot leak the future. Titles, allegiances, spouses, issue,
# siblings, aliases and death are LIFETIME lists on WOIAF (Jon Snow's titles include
# "Lord Commander", his allegiances "Night's Watch"), so they never reach a dossier;
# the model reads those facts from the time-sliced past instead. Only the first-listed
# house is passed, as a hint.
SAFE_INFOBOX = ("born", "father", "mother", "culture", "religion", "race", "seat", "words", "region")


def infobox_view(page: ParsedPage, as_of: int) -> dict:
    """The spoiler-safe subset of the infobox."""
    keep = SAFE_INFOBOX
    out = {}
    for k in keep:
        v = page.infobox.get(k)
        if v:
            out[k] = v[:400]
    return out


async def build_dossier(llm, page: ParsedPage, sliced: Sliced) -> DossierOut:
    as_of = f"{sliced.as_of} AC"
    ctx = {"name": page.display_name, "as_of": as_of, "infobox": infobox_view(page, sliced.as_of),
           "house_hint": (page.allegiances() or [None])[0], "location_hint": None,
           "past_text": sliced.past}
    d = await llm.call("dossier", dossier_system(page.display_name, as_of), user_message(ctx), DossierOut,
                       agent_id=page.title)
    d.name = page.display_name  # the wiki's name wins
    if not d.house and page.allegiances():
        d.house = page.allegiances()[0]
    return d


async def extract_fate(llm, page: ParsedPage, sliced: Sliced) -> FateOut:
    fate_text = sliced.fate
    if not fate_text.strip():
        return FateOut()
    as_of = f"{sliced.as_of} AC"
    ctx = {"name": page.display_name, "as_of": as_of, "fate_text": fate_text}
    return await llm.call("fate", fate_system(page.display_name, as_of), user_message(ctx), FateOut,
                          agent_id=page.title)

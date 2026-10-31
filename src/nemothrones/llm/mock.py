"""Deterministic mock LLM. Reads the engine's own ``<context>`` JSON and produces
schema-valid replies for every role, so the full game loop runs (and is tested)
with no network. Replies are a pure function of (role, context, input).
"""

from __future__ import annotations

import hashlib
import json
import re

LINES = [
    "As you say. Though I would have it plainly.",
    "The North remembers, and so shall I.",
    "I will think on it, and give you my answer when I have one.",
    "There is more to this than you know.",
    "Winter is coming. We would do well to remember it.",
    "The gods are watching, I think, even here.",
    "You ask a great deal of me.",
    "Is that a question, or a command?",
    "I had not thought to hear such words today.",
    "Then let it be so.",
]


def parse_user(content: str) -> tuple[dict, str]:
    ctx: dict = {}
    m = re.search(r"<context>\s*(.*?)\s*</context>", content, re.S)
    if m:
        try:
            ctx = json.loads(m.group(1))
        except json.JSONDecodeError:
            ctx = {}
    inp = ""
    m = re.search(r"<input>\s*(.*?)\s*</input>", content, re.S)
    if m:
        inp = m.group(1).strip()
    return ctx, inp


def _h(*parts: object) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _first_name(name: str) -> str:
    return (name or "").split()[0] if name else ""


def default_responder(role: str, messages: list[dict]) -> str:
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    ctx, inp = parse_user(user)
    fn = ROLES.get(role)
    out = fn(ctx, inp) if fn else {}
    return json.dumps(out)


# ----------------------------------------------------------------- the GM
def _target(ctx: dict, low: str) -> str | None:
    present = ctx.get("present", [])
    for p in present:
        for token in [p["name"].lower()] + p["name"].lower().split():
            if len(token) > 2 and token in low:
                return p["id"]
    return present[0]["id"] if present else None


def _skill_for(low: str) -> str:
    for words, skill in (
        (("read", "study", "recall", "remember", "tend", "heal"), "learning"),
        (("lie", "deceive", "spy", "eavesdrop", "sneak", "watch"), "intrigue"),
        (("count", "inspect", "provision", "accounts"), "stewardship"),
        (("command", "order", "muster", "rally"), "martial"),
        (("climb", "ride", "lift", "run", "swim", "fight", "spar"), "prowess"),
    ):
        if any(w in low for w in words):
            return skill
    return "diplomacy"


def mock_gm(ctx: dict, inp: str) -> dict:
    text = inp.strip()
    low = text.lower()
    pc = ctx.get("player", {})
    pc_id = pc.get("id")
    target = _target(ctx, low)
    base = {"targets": [target] if target else [], "duration_minutes": 2, "adjudication": None,
            "utterance": None, "destination": None, "clarification": None, "mentions": []}
    if low in {"?", "huh", "what"}:
        return {**base, "kind": "clarify", "action_summary": "", "targets": [],
                "clarification": "What would you have your character do?"}
    if low.startswith(('"', "'", "say ", "ask ", "tell ")):
        utter = re.sub(r"^(say|ask|tell)\s+", "", text, flags=re.I).strip().strip("\"'")
        adj = None
        if target and any(w in low for w in ("convince", "persuade", "must", "beg", "swear", "you should", "promise")):
            adj = {"kind": "opposed", "skill": "diplomacy", "opponent": target, "opponent_skill": "diplomacy",
                   "modifiers": [{"name": "good_argument"}],
                   "outcomes": [
                       {"label": "Convinced", "description": "Your words strike home.",
                        "effects": [{"type": "impact", "target": target, "value": 3, "note": "deeply persuaded"},
                                    {"type": "relationship", "a": target, "b": pc_id, "trust": 5}]},
                       {"label": "Wavering", "description": "Your words give them pause.",
                        "effects": [{"type": "impact", "target": target, "value": 1, "note": "uncertain"}]},
                       {"label": "Unmoved", "description": "Your words find no purchase.",
                        "effects": [{"type": "impact", "target": target, "value": -1, "note": "unconvinced"}]}]}
        return {**base, "kind": "speak", "action_summary": f"speaks: {utter[:80]}", "utterance": utter,
                "duration_minutes": 2, "adjudication": adj}
    if any(w in low for w in ("throw myself", "leap from", "jump from the")):
        return {**base, "kind": "act", "targets": [], "action_summary": "hurls themself from a great height",
                "duration_minutes": 1,
                "adjudication": {"kind": "chance", "weights": [0.0, 1.0], "outcomes": [
                    {"label": "Survive", "description": "By some miracle you live."},
                    {"label": "Death", "description": "The ground rushes up.", "lethal": True,
                     "effects": [{"type": "death", "target": pc_id, "cause": "a fall"}]}]}}
    if target and any(w in low for w in ("attack", "strike", "duel", "kill", "stab", "fight")):
        tname = next((p["name"] for p in ctx.get("present", []) if p["id"] == target), target)
        return {**base, "kind": "act", "action_summary": f"attacks {tname}", "duration_minutes": 3,
                "adjudication": {"kind": "opposed", "skill": "prowess", "opponent": target,
                                 "opponent_skill": "prowess", "modifiers": [{"name": "surprise"}],
                                 "outcomes": [
                                     {"label": "Victory", "description": "Your blade finds its mark.",
                                      "effects": [{"type": "wound", "target": target, "severity": 2}]},
                                     {"label": "Bloodied", "description": "You trade cuts.",
                                      "effects": [{"type": "wound", "target": target, "severity": 1},
                                                  {"type": "wound", "target": pc_id, "severity": 1}]},
                                     {"label": "Grievous wound", "description": "Steel bites deep into you.",
                                      "effects": [{"type": "wound", "target": pc_id, "severity": 3}]},
                                     {"label": "Death", "description": "You fall.", "lethal": True,
                                      "effects": [{"type": "death", "target": pc_id, "cause": "slain in a fight"}]}]}}
    m = re.match(r"^(?:go|walk|head|return|ride|travel|make my way)\s+(?:back\s+)?to\s+(.+)$", low)
    if m:
        return {**base, "kind": "move", "targets": [], "action_summary": f"goes to {m.group(1)}",
                "destination": m.group(1).strip(" ."), "duration_minutes": 10}
    if low.startswith(("wait", "rest", "sit and wait")):
        return {**base, "kind": "wait", "targets": [], "action_summary": "waits", "duration_minutes": 60}
    m = re.match(r"^(?:write|send a raven|send a letter)\s+to\s+([A-Za-z' ]+?)(?::|,|\s+saying)\s*(.+)$", text, re.I)
    if m:
        return {**base, "kind": "write", "targets": [m.group(1).strip()], "action_summary": f"writes to {m.group(1)}",
                "utterance": m.group(2).strip(), "duration_minutes": 30}
    return {**base, "kind": "act", "action_summary": text[:120], "duration_minutes": 10,
            "adjudication": {"kind": "check", "skill": _skill_for(low), "difficulty": 10, "outcomes": [
                {"label": "Success", "description": "It goes as you hoped."},
                {"label": "Partial", "description": "It half works."},
                {"label": "Failure", "description": "It does not go as you hoped."}]}}


# ------------------------------------------------------------- characters
def mock_agent(ctx: dict, inp: str) -> dict:
    you = ctx.get("you", {})
    addressed = bool(ctx.get("addressed"))
    turn_text = " ".join(ctx.get("this_turn", []))
    h = _h(you.get("id"), turn_text, ctx.get("scene", {}).get("date"))
    silent = {"thought": "Nothing here demands my voice.", "speech": None, "speak_to": None, "action": None,
              "plan_update": None, "opinion_changes": [], "remember": None}
    if not addressed and h % 3:
        return silent
    to = ctx.get("player_id")
    impact = ctx.get("impact")
    line = LINES[h % len(LINES)]
    if impact:
        line = "You may be right. I will do as you counsel." if impact.get("value", 0) >= 2 else line
    return {"thought": f"{_first_name(you.get('name', ''))} weighs what was said.",
            "speech": line, "speak_to": to,
            "action": {"type": "gesture", "target": to, "detail": "meets your eyes", "risky": False} if h % 2 else None,
            "plan_update": None,
            "opinion_changes": [{"of": to, "opinion": 1, "trust": 0, "why": "spoke with me"}] if to else [],
            "remember": f"Spoke with {ctx.get('player_name', 'them')} at {ctx.get('scene', {}).get('place', 'somewhere')}."}


def mock_background(ctx: dict, inp: str) -> dict:
    you = ctx.get("you", {})
    nearby = ctx.get("nearby", [])
    h = _h(you.get("id"), ctx.get("span"))
    seek = nearby[h % len(nearby)]["id"] if nearby and h % 5 == 0 else None
    return {"summary": f"{you.get('name', 'Someone')} attended to their duties at {ctx.get('where', 'home')}.",
            "location": None, "sublocation": None, "seek": seek, "letters": [], "plan_update": None,
            "importance": 2}


def mock_batch(ctx: dict, inp: str) -> dict:
    people = []
    for p in ctx.get("people", []):
        people.append({"id": p["id"], "summary": f"{p.get('name', p['id'])} kept to their routine at {p.get('where', 'home')}.",
                       "location": None, "sublocation": None, "seek": None, "letters": [], "plan_update": None,
                       "importance": 2})
    return {"people": people}


def mock_exchange(ctx: dict, inp: str) -> dict:
    a, b = ctx.get("a", {}), ctx.get("b", {})
    return {"summary": f"{a.get('name')} and {b.get('name')} spoke privately.",
            "a_remembers": f"A quiet word with {b.get('name')}.", "b_remembers": f"A quiet word with {a.get('name')}.",
            "a_opinion_of_b": 1, "b_opinion_of_a": 1, "agreement": None}


# --------------------------------------------------------------- managers
def mock_narrator(ctx: dict, inp: str) -> dict:
    scene = ctx.get("scene", {})
    present = ctx.get("present", [])
    perception = ctx.get("perception", [])
    prose = f"{scene.get('place', 'Here')}, {scene.get('time_of_day', 'day')}. " + " ".join(perception)
    options = []
    if present:
        p = present[0]
        options.append({"label": f"Ask {_first_name(p['name'])} for news",
                        "resolve": {"kind": "speak", "action_summary": f"asks {p['name']} for news",
                                    "utterance": "What news?", "targets": [p["id"]], "duration_minutes": 2}})
    options.append({"label": "Study your surroundings closely",
                    "resolve": {"kind": "act", "action_summary": "studies the surroundings", "duration_minutes": 10,
                                "adjudication": {"kind": "check", "skill": "intrigue", "difficulty": 7, "outcomes": [
                                    {"label": "Keen eye", "description": "You notice what others miss."},
                                    {"label": "Nothing", "description": "Nothing stands out."}]}}})
    options.append({"label": "Wait and watch",
                    "resolve": {"kind": "wait", "action_summary": "waits and watches", "duration_minutes": 30}})
    return {"prose": prose.strip(), "options": options}


def mock_director(ctx: dict, inp: str) -> dict:
    nearby = ctx.get("nearby", [])
    idle = int(ctx.get("idle_turns", 0))
    if nearby and (idle >= int(ctx.get("idle_threshold", 3)) or not ctx.get("present")):
        who = nearby[0]
        return {"hook": f"{who['name']} comes looking for you.", "arrivals": [who["id"]], "departures": [],
                "effects": [], "tension": 0.4, "note": "mock hook from nearby"}
    return {"hook": None, "arrivals": [], "departures": [], "effects": [], "tension": 0.3, "note": "quiet"}


def mock_chronicler(ctx: dict, inp: str) -> dict:
    pc = ctx.get("player", {})
    events = ctx.get("life_events", [])
    return {"title": f"The Chronicle of {pc.get('name', 'the Fallen')}",
            "text": f"{pc.get('name')} lived and died. " + " ".join(events[-8:])}


def mock_lore_deep(ctx: dict, inp: str) -> dict:
    return {"answer": f"The maesters record little on '{inp}' (mock).", "sources": []}


def mock_dossier(ctx: dict, inp: str) -> dict:
    info = ctx.get("infobox", {})
    past = ctx.get("past_text", "")
    first = re.split(r"(?<=[.!?])\s+", past.strip())[:3]
    return {
        "name": ctx.get("name", "Unknown"), "titles": [t.strip() for t in str(info.get("title", "")).split(",") if t.strip()][:4],
        "house": ctx.get("house_hint"), "gender": "", "culture": info.get("culture", ""), "faith": info.get("religion", ""),
        "appearance": "", "traits": ["guarded"], "values": ["family"], "fears": ["dishonour"],
        "voice": "measured and plain", "summary": " ".join(first)[:600],
        "skills": {"martial": 9, "diplomacy": 9, "intrigue": 8, "stewardship": 8, "learning": 8, "prowess": 9},
        "location": ctx.get("location_hint"), "sublocation": "", "activity": "", "health": "hale",
        "possessions": [], "secrets": [], "ambitions": ["protect their house"], "plans": [],
        "knowledge": [], "relationships": [],
    }


def mock_fate(ctx: dict, inp: str) -> dict:
    events = []
    for _i, line in enumerate(ctx.get("fate_text", "").split("\n")):
        m = re.search(r"(\d{1,3})\s*AC", line)
        if m and line.strip():
            events.append({"title": line.strip()[:80], "year": int(m.group(1)), "summary": line.strip()[:200],
                           "participants": [ctx.get("name")], "drivers": [ctx.get("name")],
                           "preconditions": [{"type": "alive", "who": ctx.get("name")}], "chance": False})
        if len(events) >= 3:
            break
    return {"summary": "mock fate", "events": events}


def mock_slicer(ctx: dict, inp: str) -> dict:
    # Conservative: anything ambiguous is treated as fate (never shown to agents).
    return {"labels": ["fate"] * len(ctx.get("passages", []))}


ROLES = {
    "gm": mock_gm, "agent": mock_agent, "background": mock_background, "batch": mock_batch,
    "exchange": mock_exchange, "narrator": mock_narrator, "director": mock_director,
    "chronicler": mock_chronicler, "lore_deep": mock_lore_deep, "dossier": mock_dossier,
    "fate": mock_fate, "slicer": mock_slicer,
}

"""The Warden: deterministic validation of every LLM output before it touches the
world. LLMs propose; the Warden enforces existence, liveness, presence, bounds and
the player's sovereignty over their own character.
"""

from __future__ import annotations

from ..agents.schemas import (
    AgentTurn,
    BackgroundTurn,
    DirectorBeat,
    Effect,
    Resolve,
)
from ..world.store import Store
from .odds import SKILLS


def _clamp(v: int, lim: int) -> int:
    return max(-lim, min(lim, int(v)))


class Warden:
    def __init__(self, store: Store):
        self.store = store
        self.warnings: list[str] = []

    @property
    def pc_id(self) -> str | None:
        return self.store.player_id

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    # --------------------------------------------------------------- ids
    def resolve_id(self, ref: str | None, among: list[str] | None = None) -> str | None:
        if not ref:
            return None
        c = self.store.character(ref)
        if c and (among is None or c["id"] in among):
            return c["id"]
        c = self.store.find_character(ref, among=among)
        return c["id"] if c else None

    def resolve_ids(self, refs: list[str], among: list[str] | None = None) -> tuple[list[str], list[str]]:
        ok, unknown = [], []
        for r in refs:
            cid = self.resolve_id(r, among)
            if cid and cid not in ok:
                ok.append(cid)
            elif not cid:
                unknown.append(r)
        return ok, unknown

    def alive(self, cid: str | None) -> bool:
        c = self.store.character(cid)
        return bool(c and c["alive"])

    # ------------------------------------------------------------ resolve
    def check_resolve(self, r: Resolve, present: list[str]) -> tuple[Resolve, list[str]]:
        """Returns the sanitised resolve plus unresolved names (candidates for promotion)."""
        r = r.model_copy(deep=True)
        pc = self.pc_id
        among = None if r.kind == "write" else present + ([pc] if pc else [])
        targets, unknown = self.resolve_ids(r.targets, among)
        r.targets = [t for t in targets if t != pc and self.alive(t)]
        if r.kind == "write" and not r.targets and not unknown:
            r.kind = "scheme"
        r.duration_minutes = max(1, min(r.duration_minutes, 60 * 24 * 60))
        adj = r.adjudication
        if adj is not None:
            if len(adj.outcomes) < 2:
                self.warn("adjudication with <2 outcomes dropped")
                r.adjudication = None
            else:
                adj.outcomes = adj.outcomes[:5]
                if adj.skill not in SKILLS:
                    adj.skill = "diplomacy" if r.kind == "speak" else "prowess"
                adj.difficulty = max(0.0, min(30.0, float(adj.difficulty)))
                if adj.kind == "opposed":
                    opp = self.resolve_id(adj.opponent, present)
                    if not opp or opp == pc or not self.alive(opp):
                        self.warn(f"opposed check without a present opponent ({adj.opponent}); using difficulty")
                        adj.kind = "check"
                        adj.opponent = None
                    else:
                        adj.opponent = opp
                        if adj.opponent_skill not in SKILLS:
                            adj.opponent_skill = adj.skill
                if adj.kind == "chance" and len(adj.weights) != len(adj.outcomes):
                    adj.weights = [1.0] * len(adj.outcomes)
                for o in adj.outcomes:
                    o.effects = self.check_effects(o.effects, actor=pc, present=present)
                    if any(e.type == "death" and e.target == pc for e in o.effects):
                        o.lethal = True
        return r, unknown

    # ------------------------------------------------------------ effects
    def check_effects(self, effects: list[Effect], *, actor: str | None, present: list[str]) -> list[Effect]:
        pc = self.pc_id
        here = set(present) | ({actor} if actor else set()) | ({pc} if pc else set())
        out: list[Effect] = []
        for e in effects:
            e = e.model_copy()
            if e.type == "impact":
                e.target = self.resolve_id(e.target, list(here))
                if not e.target or e.target == pc:
                    continue
                e.value = _clamp(e.value, 3)
            elif e.type == "relationship":
                e.a = self.resolve_id(e.a)
                e.b = self.resolve_id(e.b)
                if not e.a or not e.b or e.a == e.b:
                    continue
                if e.a == pc:  # nobody decides how the player character feels
                    continue
                e.opinion, e.trust = _clamp(e.opinion, 30), _clamp(e.trust, 30)
                e.fear, e.respect = _clamp(e.fear, 30), _clamp(e.respect, 30)
            elif e.type in ("wound", "death"):
                e.target = self.resolve_id(e.target, list(here))
                if not e.target or not self.alive(e.target):
                    continue
                e.severity = max(1, min(3, e.severity))
            elif e.type == "belief":
                e.holder = self.resolve_id(e.holder or e.target, list(here))
                if not e.holder or not (e.subject or e.content):
                    continue
                e.subject = (e.subject or "news")[:120]
                e.content = (e.content or e.subject)[:400]
            elif e.type == "move":
                e.target = self.resolve_id(e.target, list(here))
                if not e.target or not e.to:
                    continue
            elif e.type == "goal":
                e.target = self.resolve_id(e.target, list(here))
                if not e.target or e.target == pc or not e.text:
                    continue
            else:
                continue
            out.append(e)
        return out

    # -------------------------------------------------------------- agents
    def check_agent_turn(self, t: AgentTurn, cid: str, present: list[str]) -> AgentTurn:
        t = t.model_copy(deep=True)
        pc = self.pc_id
        here = [p for p in present if p != cid] + ([pc] if pc and pc != cid and pc not in present else [])
        if t.speech:
            t.speech = t.speech.strip().strip('"').strip()[:700] or None
        t.speak_to = self.resolve_id(t.speak_to, here) if t.speak_to else None
        if t.action is not None:
            a = t.action
            if a.type == "none" or (not a.detail and a.type not in ("leave", "move")):
                t.action = None
            else:
                a.target = self.resolve_id(a.target, here) if a.target else None
                if a.type == "attack" and not a.target:
                    t.action = None
                else:
                    a.detail = a.detail[:300]
                    if a.type == "attack":
                        a.risky = True
        changes = []
        for oc in t.opinion_changes:
            other = self.resolve_id(oc.of)
            if other and other != cid:
                oc.of = other
                oc.opinion, oc.trust = _clamp(oc.opinion, 20), _clamp(oc.trust, 20)
                changes.append(oc)
        t.opinion_changes = changes[:4]
        if t.plan_update:
            t.plan_update = t.plan_update[:300]
        if t.remember:
            t.remember = t.remember[:400]
        return t

    def check_background(self, b: BackgroundTurn, cid: str) -> BackgroundTurn:
        b = b.model_copy(deep=True)
        me = self.store.character(cid)
        if b.location:
            loc = self.store.location(b.location) or self.store.find_location(b.location)
            b.location = loc["id"] if loc else None
            if b.location == me.get("location_id"):
                b.location = None
        if b.seek:
            s = self.resolve_id(b.seek)
            b.seek = s if s and s != cid and self.alive(s) else None
        letters = []
        for letter in b.letters[:3]:
            to = self.resolve_id(letter.to)
            if to and to != cid and self.alive(to):
                letter.to = to
                letter.content = letter.content[:800]
                letters.append(letter)
        b.letters = letters
        b.summary = (b.summary or "")[:500]
        if b.sublocation:
            b.sublocation = b.sublocation[:60]
        return b

    def check_beat(self, beat: DirectorBeat, present: list[str], nearby: list[str]) -> DirectorBeat:
        beat = beat.model_copy(deep=True)
        pc = self.pc_id
        arr, _ = self.resolve_ids(beat.arrivals, nearby)
        beat.arrivals = [a for a in arr if a != pc and self.alive(a)][:3]
        dep, _ = self.resolve_ids(beat.departures, present)
        beat.departures = [d for d in dep if d != pc][:3]
        beat.effects = [e for e in self.check_effects(beat.effects, actor=None, present=present)
                        if e.type in ("belief", "relationship", "goal")]
        beat.tension = max(0.0, min(1.0, float(beat.tension)))
        if beat.hook:
            beat.hook = beat.hook[:500]
        return beat

"""The Game: one save, one life. Owns the turn pipeline.

Turn = input -> (GM Resolve | precomputed option) -> Warden -> odds -> roll ->
effects -> on-stage reactions (Tier 0, parallel) -> clock advance (background
tiers, letters, mortality) -> Director -> Narrator (+ next options) -> commit.
One SQLite transaction per turn, so a turn is atomic and every turn is an autosave.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from ..agents import character as agents
from ..agents import context as cx
from ..agents.memory import remember
from ..agents.roles import chronicler, director, gm, narrator
from ..agents.schemas import AgentTurn, BackgroundTurn, Chronicle, Option, Resolve
from ..config import Settings
from ..llm import LLM, LLMUnavailable
from ..llm.budget import BudgetExceeded
from ..world.pack import OpeningSpec, WorldPack
from ..world.seed import seed_world
from ..world.store import Store
from . import clock, mortality, ravenry, scheduler
from . import effects as fx
from .odds import (
    SKILLS,
    Modifier,
    OddsTable,
    compute_chance,
    compute_check,
    fmt_pct,
    resolve_modifiers,
)
from .rng import Dice
from .warden import Warden

LONG_WAIT_MIN = 6 * 60
MAX_PLANS = 5
SOFT_ERRORS = (LLMUnavailable, BudgetExceeded)


@dataclass
class Block:
    kind: str  # title|header|prose|roll|options|system|warn|error|chronicle|sheet|notice
    text: str


def wound_mods(c: dict) -> list[Modifier]:
    worst = max((w.get("severity", 1) for w in c["attrs"].get("wounds", [])), default=0)
    if worst >= 3:
        return [Modifier("grievously_wounded", -6, "engine: wounds")]
    if worst == 2:
        return [Modifier("wounded", -3, "engine: wounds")]
    return []


class Game:
    def __init__(self, store: Store, settings: Settings, llm: LLM | None = None, *,
                 path: Path | None = None, readonly: bool = False):
        self.store = store
        self.settings = settings.model_copy(deep=True)
        for k, v in (store.meta_get("settings", {}) or {}).items():
            if hasattr(self.settings.game, k):
                setattr(self.settings.game, k, v)
        self.llm = llm or LLM(self.settings)
        if not readonly:
            self.llm.log_sink = store.log_llm
        self.path = path
        self.readonly = readonly
        self.warden = Warden(store)
        self.lore = None  # optional LoreService (M2): /lore and promotion on encounter
        self._arrivals: list[str] = []
        self._load_state()

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def create(cls, settings: Settings, path: str | Path, pack: WorldPack, player_id: str, *,
               seed: int, llm: LLM | None = None) -> Game:
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"save already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path)
        with store.transaction():
            seed_world(store, pack, player_id, seed=seed)
            store.meta_set("opening", pack.opening_for(player_id).model_dump())
            g = settings.game
            store.meta_set("settings", {"canon_gravity": g.canon_gravity, "odds_display": g.odds_display,
                                        "verbosity": g.verbosity})
            store.meta_set("turn", 0)
            store.meta_set("opened", False)
        game = cls(store, settings, llm, path=path)
        with store.transaction():
            scheduler.retier(store, game.settings.game, [player_id], game.dice)
            game._persist()
        return game

    @classmethod
    def open(cls, settings: Settings, path: str | Path, llm: LLM | None = None) -> Game:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        readonly = not os.access(path, os.W_OK) or not (path.stat().st_mode & stat.S_IWUSR)
        store = Store(path, readonly=readonly)
        return cls(store, settings, llm, path=path, readonly=readonly)

    def _load_state(self) -> None:
        s = self.store
        self.dice = Dice(s.meta_get("seed", 0), s.meta_get("rng", {}))
        ui = s.meta_get("ui", {}) or {}
        self.options = [Option.model_validate(o) for o in ui.get("options", [])]
        self.pending = ui.get("pending")
        self.idle_turns = int(ui.get("idle", 0))
        self.last_prose = ui.get("last_prose", "")
        self.ended = bool(s.meta_get("ended", False))
        s.turn = int(s.meta_get("turn", 0))

    def _persist(self) -> None:
        s = self.store
        s.meta_set("ui", {"options": [o.model_dump() for o in self.options], "pending": self.pending,
                          "idle": self.idle_turns, "last_prose": self.last_prose})
        s.meta_set("rng", self.dice.counters)
        s.meta_set("turn", s.turn)
        s.meta_set("spent", float(s.meta_get("spent", 0.0)) + self._spent_delta())

    def _spent_delta(self) -> float:
        spent = self.llm.budget.spent
        delta = spent - getattr(self, "_spent_mark", 0.0)
        self._spent_mark = spent
        return delta

    async def close(self) -> None:
        await self.llm.aclose()
        if self.readonly:
            self.store.close()
            return
        self.store.close()
        if self.ended and self.path:
            self._archive()

    def _archive(self) -> None:
        """Ironman: a finished life becomes a read-only chronicle."""
        conn = Store(self.path)
        conn.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        os.chmod(self.path, stat.S_IRUSR | stat.S_IRGRP)

    # ------------------------------------------------------------ properties
    @property
    def pc_id(self) -> str:
        return self.store.player_id

    @property
    def pc(self) -> dict:
        return self.store.character(self.pc_id)

    @property
    def scene(self) -> dict | None:
        return self.store.scene()

    def present_npcs(self) -> list[str]:
        sc = self.scene
        if not sc:
            return []
        pc = self.pc
        out = []
        for cid in sc["present"]:
            if cid == self.pc_id:
                continue
            c = self.store.character(cid)
            if c and c["alive"] and c.get("location_id") == pc.get("location_id"):
                out.append(cid)
        return out

    def header(self) -> str:
        pc = self.pc
        place = cx.where(self.store, pc.get("location_id"), pc["status"].get("sublocation"))
        return f"── {clock.format_date(self.store.now)} · {place} · {self.store.meta_get('season', '')} ──"

    # ------------------------------------------------------------ entry points
    async def begin(self) -> list[Block]:
        """Opening scene for a new save, or a recap when resuming."""
        if self.ended:
            chron = self.store.meta_get("chronicle") or {}
            return [Block("title", chron.get("title", "This life has ended.")),
                    Block("chronicle", chron.get("text", "")),
                    Block("system", "This save is a finished chronicle. Use /new to begin another life.")]
        if self.store.meta_get("opened"):
            blocks = [Block("header", self.header()), Block("prose", self.last_prose or "You gather your thoughts.")]
            blocks += self._options_blocks()
            return blocks
        return await self._guard(self._opening)

    async def handle(self, text: str) -> list[Block]:
        text = (text or "").strip()
        if not text:
            return []
        if text.startswith("/"):
            from .commands import dispatch

            return await dispatch(self, text)
        if self.ended:
            return [Block("system", "This life has ended. Use /new to begin another, or /quit.")]
        if self.readonly:
            return [Block("system", "This save is read-only.")]
        if self.pending:
            return await self._guard(self._confirm, text)
        opt = self._option_for(text)
        if opt:
            return await self._guard(self._attempt, opt.resolve, opt.label)
        try:
            r = await gm.resolve(self.llm, self.store, self.pc_id, self.scene, text)
        except SOFT_ERRORS as e:
            return [Block("error", f"The Game Master is unreachable: {e}"), *self._notices()]
        return await self._guard(self._attempt, r, text)

    async def _guard(self, fn, *args) -> list[Block]:
        try:
            return await fn(*args)
        except SOFT_ERRORS as e:
            self._load_state()
            return [Block("error", f"The turn could not complete: {e}"), *self._notices()]
        except Exception as e:  # keep the REPL alive; the transaction has rolled back
            self._load_state()
            if self.settings.dev:
                raise
            return [Block("error", f"Internal error ({type(e).__name__}: {e}); the turn was undone.")]

    def _option_for(self, text: str) -> Option | None:
        m = re.fullmatch(r"#?(\d{1,2})\.?", text)
        if m and self.options:
            n = int(m.group(1))
            if 1 <= n <= len(self.options):
                return self.options[n - 1]
        return None

    async def _confirm(self, text: str) -> list[Block]:
        pending, self.pending = self.pending, None
        if text.lower() in ("y", "yes"):
            r = Resolve.model_validate(pending["resolve"])
            return await self._execute(r, self.compute_odds(r), pending.get("raw", ""))
        self._persist()
        return [Block("system", "You think better of it.")]

    # ------------------------------------------------------------ opening
    async def _opening(self) -> list[Block]:
        s = self.store
        op = OpeningSpec.model_validate(s.meta_get("opening") or {"location": self.pc.get("location_id") or ""})
        with s.transaction():
            pc_id = self.pc_id
            present = [p for p in op.present if self.warden.alive(p) and p != pc_id]
            for cid in [pc_id] + present:
                s.emit("character_moved", {"id": cid, "location_id": op.location, "sublocation": op.sublocation},
                       visibility="system")
            self._start_scene(op.location, op.sublocation, op.situation, present=[pc_id] + present)
            perception, observed = [], []
            if op.situation:
                perception.append(op.situation)
            if op.hook:
                s.emit("hook", summary=op.hook, location_id=op.location, visibility="scene")
                perception.append(op.hook)
                observed.append(op.hook)
            arrivals = [a for a in op.hook_arrivals if self.warden.alive(a) and a != pc_id]
            for a in arrivals:
                self._join_scene(a)
            lines = await self._reactions(observed or perception, addressed=arrivals, impacts={})
            perception += lines
            s.meta_set("opened", True)
            blocks = [Block("title", s.meta_get("title", "NemoThrones"))]
            blocks += await self._narrate(perception)
            self._persist()
        return blocks

    # ------------------------------------------------------------ odds
    def compute_odds(self, r: Resolve, actor_id: str | None = None) -> OddsTable | None:
        adj = r.adjudication
        if adj is None or len(adj.outcomes) < 2:
            return None
        actor = self.store.character(actor_id or self.pc_id)
        labels = [o.label for o in adj.outcomes]
        lethal = [bool(o.lethal) for o in adj.outcomes]
        if adj.kind == "chance":
            return compute_chance(weights=adj.weights or [1.0] * len(labels), labels=labels, lethal=lethal)
        skill = adj.skill if adj.skill in SKILLS else "prowess"
        value = actor["attrs"].get("skills", {}).get(skill, 8)
        mods = resolve_modifiers(adj.modifiers) + wound_mods(actor)
        if adj.kind == "opposed" and adj.opponent and self.store.character(adj.opponent):
            opp = self.store.character(adj.opponent)
            oskill = adj.opponent_skill if adj.opponent_skill in SKILLS else skill
            difficulty = opp["attrs"].get("skills", {}).get(oskill, 8) + sum(m.value for m in wound_mods(opp))
            return compute_check(actor=actor["name"], skill=skill, skill_value=value, difficulty=difficulty,
                                 modifiers=mods, labels=labels, lethal=lethal, kind="opposed",
                                 opponent=opp["name"], opponent_skill=oskill)
        return compute_check(actor=actor["name"], skill=skill, skill_value=value, difficulty=adj.difficulty,
                             modifiers=mods, labels=labels, lethal=lethal)

    # ------------------------------------------------------------ the turn
    async def _attempt(self, r: Resolve, raw: str) -> list[Block]:
        r, unknown = self.warden.check_resolve(r, self.present_npcs())
        if r.kind == "write" and unknown and self.lore is not None:
            for name in unknown:
                cid = await self.lore.promote(self, name)
                if cid:
                    r.targets.append(cid)
        if r.kind == "clarify":
            return [Block("system", r.clarification or "Say a little more about what you do.")]
        if r.kind == "ooc":
            return [Block("system", r.clarification or "Out-of-character help: /help, /me, /who, /know <x>, /lore <topic>.")]
        if r.kind == "wait":
            return await self.wait(r.duration_minutes, r.action_summary or "wait")
        odds = self.compute_odds(r)
        if odds and odds.lethal_risk >= self.settings.game.lethal_confirm_threshold:
            self.pending = {"resolve": r.model_dump(), "raw": raw}
            self._persist()
            return [Block("roll", f"{r.action_summary or raw}\n   {odds.render('exact')}"),
                    Block("warn", f"This could be the death of you ({fmt_pct(odds.lethal_risk)}). Proceed? (y/n)")]
        return await self._execute(r, odds, raw)

    async def _execute(self, r: Resolve, odds: OddsTable | None, raw: str) -> list[Block]:
        s = self.store
        blocks: list[Block] = []
        with s.transaction():
            s.turn += 1
            pc = self.pc
            name = pc["name"]
            loc = pc.get("location_id")
            present = self.present_npcs()
            perception: list[str] = []
            observed: list[str] = []
            eventful = False
            scene_changed = False
            to_names = ", ".join(fx.name_of(s, t) for t in r.targets)
            summ = (r.action_summary or raw).strip()
            s.add_transcript("player", raw)

            if r.kind == "speak" and r.utterance:
                to_obs = f" to {to_names}" if r.targets else ""
                line = f'{name} says{to_obs}: "{r.utterance}"'
                s.emit("speech", {"speech": r.utterance, "to": r.targets}, actors=[self.pc_id, *r.targets],
                       location_id=loc, summary=line, visibility="scene")
                perception.append(f'You say{to_obs}: "{r.utterance}"')
                observed.append(line)
            elif r.kind not in ("move", "write"):
                obs = summ if name.split()[0].lower() in summ.lower()[:40] else f"{name} {summ}"
                s.emit("player_action", {"kind": r.kind, "raw": raw}, actors=[self.pc_id, *r.targets],
                       location_id=loc, summary=obs, visibility="scene")
                perception.append(f"You attempt: {summ}")
                observed.append(obs)

            impacts: dict[str, dict] = {}
            if odds and r.adjudication:
                eventful = True
                idx, u = self.dice.pick("player", odds.probs)
                outcome = r.adjudication.outcomes[idx]
                s.log_roll(actor_id=self.pc_id, action=summ, kind=odds.kind, labels=odds.labels,
                           probs=odds.probs, derivation=odds.derivation, u=u, outcome_index=idx)
                oline = f"{outcome.label}: {outcome.description}".strip(": ")
                eid = s.emit("outcome", {"label": outcome.label, "index": idx}, actors=[self.pc_id, *r.targets],
                             location_id=loc, summary=f"{name}'s attempt ({summ[:80]}) — {oline}", visibility="scene")
                blocks.append(Block("roll", f"🎲 {outcome.label}   ·   {odds.render('exact')}"))
                perception.append(f"Outcome — {oline}")
                observed.append(f"The result — {oline}")
                rep = fx.apply_effects(s, outcome.effects, actor_id=self.pc_id, context=summ)
                perception += rep.lines
                observed += rep.lines
                impacts = rep.impacts
                ravenry.witness(s, present + [self.pc_id], f"{name}: {summ[:80]}", oline, eid)

            if r.kind == "write":
                perception += self._write_letters(r)
                eventful = True
            if r.kind == "move" and r.destination:
                perception += await self._move(r.destination)
                scene_changed = True
                eventful = True

            if self.pc["alive"] and r.kind != "move":
                lines = await self._reactions(observed, addressed=r.targets, impacts=impacts)
                perception += lines
                eventful = eventful or bool(lines)

            if self.pc["alive"] and r.kind != "move":
                adv = await self._advance(r.duration_minutes, interruptible=False)
                perception += adv
                eventful = eventful or bool(adv)
            perception += await self._greet_arrivals()

            if self.pc["alive"]:
                self.idle_turns = 0 if eventful else self.idle_turns + 1
                if scene_changed or self.idle_turns >= self.settings.game.idle_turns_for_hook:
                    perception += await self._direct("new scene" if scene_changed else "the scene is idle")

            if not self.pc["alive"]:
                blocks += await self._death(perception)
            else:
                blocks += await self._narrate(perception)
            self._persist()
        return blocks

    async def wait(self, minutes: int, reason: str = "wait") -> list[Block]:
        s = self.store
        with s.transaction():
            s.turn += 1
            minutes = max(1, int(minutes))
            reason = reason.strip() or "wait"
            perception = [f"You {reason} ({clock.format_duration(minutes)})."]
            s.add_transcript("player", f"(wait {clock.format_duration(minutes)})")
            long_wait = minutes >= LONG_WAIT_MIN
            if long_wait:
                self._end_scene()
            perception += await self._advance(minutes, interruptible=True)
            if self.pc["alive"] and long_wait:
                pc = self.pc
                self._start_scene(pc.get("location_id"), pc["status"].get("sublocation", ""), "")
                here = [fx.name_of(s, c) for c in self.present_npcs()]
                perception.append(("With you now: " + ", ".join(here) + ".") if here else "You are alone.")
            perception += await self._greet_arrivals()
            if self.pc["alive"]:
                perception += await self._direct("after a wait")
            if not self.pc["alive"]:
                blocks = await self._death(perception)
            else:
                blocks = await self._narrate(perception)
            self._persist()
        return blocks

    # ------------------------------------------------------------ scenes
    def _start_scene(self, location_id: str | None, sublocation: str, situation: str,
                     present: list[str] | None = None) -> None:
        s = self.store
        if self.scene and self.scene.get("ended_t") is None:
            self._end_scene()
        if present is None:
            present = s.present_at(location_id, sublocation)
        if self.pc_id not in present:
            present = [self.pc_id] + present
        s.emit("scene_started", {"location_id": location_id, "sublocation": sublocation, "present": present,
                                 "situation": situation}, location_id=location_id, visibility="system")
        scheduler.retier(s, self.settings.game, present, self.dice, self.llm.budget.pressure())

    def _end_scene(self) -> None:
        sc = self.scene
        if not sc or sc.get("ended_t") is not None:
            return
        self.store.emit("scene_ended", {"scene_id": sc["id"]}, visibility="system")
        for cid in sc["present"]:
            if cid != self.pc_id and self.warden.alive(cid):
                self.store.emit("tier_changed", {"id": cid, "tier": 1, "next_act_t": self.store.now},
                                visibility="system")

    def _join_scene(self, cid: str) -> None:
        s = self.store
        pc = self.pc
        s.emit("character_moved", {"id": cid, "location_id": pc.get("location_id"),
                                   "sublocation": pc["status"].get("sublocation", "")}, visibility="system")
        sc = self.scene
        if sc and cid not in sc["present"]:
            s.emit("scene_updated", {"scene_id": sc["id"], "present": sc["present"] + [cid]}, visibility="system")
            s.emit("tier_changed", {"id": cid, "tier": 0}, visibility="system")

    def _leave_scene(self, cid: str, to: str | None = None) -> str:
        s = self.store
        pc = self.pc
        subs = ((s.location(pc.get("location_id")) or {}).get("data") or {}).get("sublocations", [])
        here = pc["status"].get("sublocation", "")
        dest = to or next((x for x in subs if x != here), "elsewhere")
        where = fx.move_to(s, cid, dest)
        sc = self.scene
        if sc and cid in sc["present"]:
            s.emit("scene_updated", {"scene_id": sc["id"], "present": [p for p in sc["present"] if p != cid]},
                   visibility="system")
            s.emit("tier_changed", {"id": cid, "tier": 1, "next_act_t": s.now + 60}, visibility="system")
        return where

    async def _move(self, destination: str) -> list[str]:
        s = self.store
        pc = self.pc
        dest = destination.strip()
        loc = s.location(dest) or s.find_location(dest)
        lines: list[str] = []
        if loc and loc["id"] != pc.get("location_id"):
            miles = ravenry.distance(s, pc.get("location_id"), loc["id"])
            miles = miles if miles is not None else ravenry.DEFAULT_MILES
            minutes = ravenry.travel_minutes(miles, ravenry.PARTY_MILES_PER_DAY)
            lines.append(f"You set out for {loc['name']}, some {int(miles)} miles away: "
                         f"{clock.format_duration(minutes)} on the road.")
            self._end_scene()
            s.emit("status_changed", {"id": self.pc_id, "set": {"activity": f"travelling to {loc['name']}"}},
                   visibility="system")
            lines += await self._advance(minutes, interruptible=False)
            if not self.pc["alive"]:
                return lines
            sub = ((loc.get("data") or {}).get("sublocations") or [""])[0]
            s.emit("character_moved", {"id": self.pc_id, "location_id": loc["id"], "sublocation": sub},
                   actors=[self.pc_id], summary=f"{pc['name']} arrives at {loc['name']}")
            s.emit("status_changed", {"id": self.pc_id, "set": {"activity": ""}}, visibility="system")
            self._start_scene(loc["id"], sub, f"just arrived at {loc['name']}")
            lines.append(f"You arrive at {loc['name']}{', ' + sub if sub else ''}.")
        else:
            subs = ((s.location(pc.get("location_id")) or {}).get("data") or {}).get("sublocations", [])
            low = dest.lower().removeprefix("the ")
            sub = next((x for x in subs if low in x.lower() or x.lower().removeprefix("the ") in low), dest[:60])
            s.emit("character_moved", {"id": self.pc_id, "sublocation": sub}, actors=[self.pc_id],
                   summary=f"{pc['name']} goes to {sub}")
            self._start_scene(pc.get("location_id"), sub, "")
            lines.append(f"You make your way to {sub}.")
        here = [fx.name_of(s, c) for c in self.present_npcs()]
        lines.append(("Here: " + ", ".join(here) + ".") if here else "There is no one here.")
        return lines

    # ------------------------------------------------------------ letters
    def _write_letters(self, r: Resolve) -> list[str]:
        s = self.store
        lines = []
        if not r.utterance:
            return ["You set quill to parchment, but find no words."]
        for t in r.targets:
            if t == self.pc_id or not self.warden.alive(t):
                continue
            ravenry.send_letter(s, self.pc_id, t, r.utterance)
            d = s.conn.execute("SELECT channel FROM deliveries ORDER BY id DESC LIMIT 1").fetchone()
            lines.append(f"You write to {fx.name_of(s, t)} and seal the letter; it goes {'by ' + d[0] if d else ''}.")
            remember(s, self.pc_id, f"Wrote to {fx.name_of(s, t)}: {r.utterance}", kind="letter", importance=5,
                     entities=[fx.name_of(s, t)])
        return lines or ["You know of no way to reach them."]

    # ------------------------------------------------------------ on-stage agents
    async def _agent_call(self, cid: str, this_turn: list[str], addressed: bool,
                          impact: dict | None) -> AgentTurn | None:
        try:
            return await agents.act_on_stage(self.llm, self.store, cid, self.pc_id, self.scene, this_turn,
                                             addressed, impact)
        except SOFT_ERRORS:
            return None

    async def _reactions(self, observed: list[str], *, addressed: list[str], impacts: dict) -> list[str]:
        s = self.store
        present = self.present_npcs()
        if not present or not observed:
            return []
        addressed = [a for a in addressed if a in present]
        others = sorted((p for p in present if p not in addressed),
                        key=lambda c: -s.character(c)["importance"])
        room = max(1, self.settings.game.max_speakers - len(addressed))
        callers = addressed + others[:room]
        turns = await asyncio.gather(*(self._agent_call(c, observed, c in addressed, impacts.get(c)) for c in callers))
        lines: list[str] = []
        spoke: set[str] = set()
        follow: tuple[str, str] | None = None
        for cid, t in zip(callers, turns, strict=True):
            if t is None or not self.pc["alive"]:
                continue
            t = self.warden.check_agent_turn(t, cid, self.scene["present"])
            new = self._apply_agent_turn(cid, t)
            lines += new
            if t.speech:
                spoke.add(cid)
                if (t.speak_to and t.speak_to != self.pc_id and t.speak_to in present
                        and t.speak_to not in callers and follow is None):
                    follow = (t.speak_to, new[0] if new else "")
        if follow and self.pc["alive"]:
            t = await self._agent_call(follow[0], observed + lines, True, None)
            if t:
                lines += self._apply_agent_turn(follow[0], self.warden.check_agent_turn(t, follow[0], self.scene["present"]))
        names = [fx.name_of(s, c) for c in present] + [self.pc["name"]]
        text = " ".join(observed + lines)[:1100]
        for cid in present + [self.pc_id]:
            if self.warden.alive(cid):
                remember(s, cid, text, kind="dialogue", importance=3 + (1 if impacts else 0), entities=names)
        return lines

    def _apply_agent_turn(self, cid: str, t: AgentTurn) -> list[str]:
        s = self.store
        c = s.character(cid)
        if not c or not c["alive"]:
            return []
        name = c["name"]
        loc = c.get("location_id")
        lines: list[str] = []
        if t.thought:
            s.emit("thought", {"thought": t.thought}, actors=[cid], visibility="private", summary=f"{name} thinks")
        if t.speech:
            to = t.speak_to
            to_pc = " to you" if to == self.pc_id else (f" to {fx.name_of(s, to)}" if to else "")
            to_obs = f" to {fx.name_of(s, to)}" if to else ""
            s.emit("speech", {"speech": t.speech, "to": [to] if to else []}, actors=[cid] + ([to] if to else []),
                   location_id=loc, summary=f'{name} says{to_obs}: "{t.speech}"', visibility="scene")
            lines.append(f'{name} says{to_pc}: "{t.speech}"')
        a = t.action
        if a is not None:
            if a.type == "attack" and a.target:
                lines += self._npc_attack(cid, a.target, a.detail)
            elif a.type in ("leave", "move"):
                where = self._leave_scene(cid, a.to)
                s.emit("departure", actors=[cid], location_id=loc, summary=f"{name} leaves for {where}",
                       visibility="scene")
                lines.append(f"{name} leaves{(' for ' + where) if where and where != 'elsewhere' else ''}.")
            elif a.risky:
                odds = compute_check(actor=name, skill="intrigue", skill_value=c["attrs"]["skills"].get("intrigue", 8),
                                     difficulty=12, modifiers=wound_mods(c), labels=["Succeeds", "Fails"],
                                     lethal=[False, False])
                idx, u = self.dice.pick("npc", odds.probs)
                s.log_roll(actor_id=cid, action=a.detail, kind=odds.kind, labels=odds.labels, probs=odds.probs,
                           derivation=odds.derivation, u=u, outcome_index=idx)
                res = odds.labels[idx].lower()
                s.emit("action", {"detail": a.detail, "result": res}, actors=[cid], location_id=loc,
                       summary=f"{name} {a.detail} — and {res}", visibility="scene")
                lines.append(f"{name} {a.detail} — and {res}.")
            elif a.detail:
                s.emit("action", {"detail": a.detail, "type": a.type}, actors=[cid] + ([a.target] if a.target else []),
                       location_id=loc, summary=f"{name} {a.detail}", visibility="scene")
                lines.append(f"{name} {a.detail}.")
        for oc in t.opinion_changes:
            if oc.opinion or oc.trust:
                s.emit("relationship_changed", {"a": cid, "b": oc.of, "delta": {"opinion": oc.opinion, "trust": oc.trust},
                                                "note": oc.why[:120]}, actors=[cid, oc.of], visibility="private")
        if t.plan_update:
            self._add_plan(cid, t.plan_update)
        if t.remember:
            remember(s, cid, t.remember, importance=5, entities=[name])
        return lines

    def _add_plan(self, cid: str, text: str) -> None:
        s = self.store
        s.emit("goal_added", {"owner": cid, "level": "plan", "text": text[:300]}, visibility="private", actors=[cid])
        plans = [g for g in s.goals(cid) if g["level"] == "plan"]
        for g in plans[:-MAX_PLANS]:
            s.emit("goal_updated", {"goal_id": g["id"], "status": "abandoned"}, visibility="system")

    def _npc_attack(self, attacker: str, target: str, detail: str) -> list[str]:
        s = self.store
        a, d = s.character(attacker), s.character(target)
        if not d or not d["alive"]:
            return []
        labels = ["Kills", "Grievous wound", "Wounds", "Glancing", "Repelled"]
        dv = d["attrs"]["skills"].get("prowess", 8) + sum(m.value for m in wound_mods(d))
        odds = compute_check(actor=a["name"], skill="prowess", skill_value=a["attrs"]["skills"].get("prowess", 8),
                             difficulty=dv, modifiers=wound_mods(a), labels=labels, lethal=[False] * 5,
                             kind="opposed", opponent=d["name"], opponent_skill="prowess")
        idx, u = self.dice.pick("npc", odds.probs)
        s.log_roll(actor_id=attacker, action=f"attacks {d['name']}", kind=odds.kind, labels=labels,
                   probs=odds.probs, derivation=odds.derivation, u=u, outcome_index=idx)
        to = "you" if target == self.pc_id else d["name"]
        cause = f"slain by {a['name']}"
        if idx == 0:
            fx.kill(s, target, cause)
        elif idx in (1, 2, 3):
            fx.wound(s, target, {1: 3, 2: 2, 3: 1}[idx], f"struck by {a['name']}")
        else:
            fx.wound(s, attacker, 1, f"repelled by {d['name']}")
        line = f"{a['name']} attacks {to}{(' (' + detail + ')') if detail else ''} — {labels[idx].lower()}."
        eid = s.emit("outcome", {"label": labels[idx]}, actors=[attacker, target], location_id=a.get("location_id"),
                     summary=line.replace(" you", " " + d["name"]), visibility="scene")
        ravenry.witness(s, self.present_npcs() + [self.pc_id], f"{a['name']} attacked {d['name']}", labels[idx], eid)
        return [line]

    # ------------------------------------------------------------ time
    def _steps(self, minutes: int) -> list[int]:
        now = self.store.now
        if minutes <= clock.MIN_PER_DAY:
            return [now + minutes]
        step = max(clock.MIN_PER_DAY, min(7 * clock.MIN_PER_DAY, minutes // 4))
        n = max(1, math.ceil(minutes / step))
        return [now + min(minutes, (i + 1) * step) for i in range(n)]

    async def _advance(self, minutes: int, *, interruptible: bool) -> list[str]:
        s = self.store
        perception: list[str] = []
        if minutes <= 0:
            return perception
        for st in self._steps(minutes):
            dt = st - s.now
            s.emit("clock_advanced", {"to": st}, t=st, visibility="system")
            for cid, cause in mortality.step(s, self.dice, dt):
                fx.kill(s, cid, cause)
                if cid == self.pc_id:
                    perception.append(f"Your strength fails you at last: {cause}.")
                    return perception
                if cid in self.present_npcs():
                    perception.append(f"{fx.name_of(s, cid)} dies before your eyes.")
            for d in ravenry.deliver_due(s, self.dice):
                if d["recipient"] == self.pc_id:
                    perception.append(f'A letter reaches you ({d["channel"]}) from {d["sender_name"]}: "{d["content"]}"')
            perception += await self._background()
            if interruptible and perception:
                break
        return perception

    async def _bg_call(self, c: dict, span: str) -> list[tuple[str, BackgroundTurn | None]]:
        try:
            return [(c["id"], await agents.act_background(self.llm, self.store, c["id"], span))]
        except SOFT_ERRORS:
            return [(c["id"], None)]

    async def _batch_call(self, chunk: list[dict], span: str) -> list[tuple[str, BackgroundTurn | None]]:
        ids = [c["id"] for c in chunk]
        try:
            res = await agents.act_batch(self.llm, self.store, ids, span)
        except SOFT_ERRORS:
            return [(i, None) for i in ids]
        by_id = {p.id: p for p in res.people if p.id}
        return [(i, by_id.get(i)) for i in ids]

    async def _background(self) -> list[str]:
        s = self.store
        sc = self.scene
        present = sc["present"] if sc else []
        t1, t2 = scheduler.due(s, s.now, present)
        if not t1 and not t2:
            return []
        now_label = clock.format_date(s.now)

        def span(c: dict) -> str:
            last = c.get("last_act_t") or s.now
            return f"from {clock.format_date(last)} to {now_label}"

        regions: dict[str, list[dict]] = {}
        for c in t2:
            loc = s.location(c.get("location_id")) or {}
            regions.setdefault(loc.get("region") or "elsewhere", []).append(c)
        chunks = []
        for group in regions.values():
            for i in range(0, len(group), self.settings.game.batch_size):
                chunks.append(group[i:i + self.settings.game.batch_size])
        calls = [self._bg_call(c, span(c)) for c in t1]
        calls += [self._batch_call(ch, f"the days up to {now_label}") for ch in chunks]
        results = [pair for group in await asyncio.gather(*calls) for pair in group]
        pressure = self.llm.budget.pressure()
        lines: list[str] = []
        meetings: list[tuple[str, str]] = []
        for cid, bt in results:
            c = s.character(cid)
            if not c or not c["alive"]:
                continue
            if bt is not None:
                lines += self._apply_background(cid, bt, meetings)
            scheduler.reschedule(s, cid, c["tier"] if c["tier"] in (1, 2) else 2, self.settings.game,
                                 self.dice, pressure)
        if meetings:
            lines += await self._meetings(meetings)
        return lines

    def _apply_background(self, cid: str, bt: BackgroundTurn, meetings: list[tuple[str, str]]) -> list[str]:
        s = self.store
        bt = self.warden.check_background(bt, cid)
        c = s.character(cid)
        if bt.summary:
            s.emit("background", {"importance": bt.importance}, actors=[cid], location_id=c.get("location_id"),
                   summary=f"{c['name']}: {bt.summary}", visibility="private")
            remember(s, cid, bt.summary, importance=bt.importance, entities=[c["name"]])
            s.emit("status_changed", {"id": cid, "set": {"activity": bt.summary[:160]}}, visibility="system")
        if bt.location:
            fx.move_to(s, cid, bt.location)
        elif bt.sublocation:
            s.emit("character_moved", {"id": cid, "sublocation": bt.sublocation}, visibility="system")
        for letter in bt.letters:
            ravenry.send_letter(s, cid, letter.to, letter.content)
        if bt.plan_update:
            self._add_plan(cid, bt.plan_update)
        lines: list[str] = []
        if bt.seek:
            c = s.character(cid)
            other = s.character(bt.seek)
            if other and other["alive"] and other.get("location_id") == c.get("location_id"):
                if bt.seek == self.pc_id:
                    self._join_scene(cid)
                    self._arrivals.append(cid)
                    lines.append(f"{c['name']} comes looking for you.")
                elif bt.seek not in (self.scene or {}).get("present", []):
                    meetings.append((cid, bt.seek))
        return lines

    async def _meetings(self, pairs: list[tuple[str, str]]) -> list[str]:
        s = self.store
        seen: set[str] = set()
        todo = []
        for a, b in pairs:
            if a in seen or b in seen:
                continue
            seen |= {a, b}
            todo.append((a, b))

        async def one(a: str, b: str):
            try:
                return a, b, await agents.exchange(self.llm, s, a, b, "an off-stage meeting")
            except SOFT_ERRORS:
                return a, b, None

        for a, b, ex in await asyncio.gather(*(one(a, b) for a, b in todo)):
            if ex is None:
                continue
            na, nb = fx.name_of(s, a), fx.name_of(s, b)
            s.emit("exchange", {"agreement": ex.agreement}, actors=[a, b], location_id=s.character(a).get("location_id"),
                   summary=f"{na} and {nb}: {ex.summary}"[:600], visibility="private")
            remember(s, a, ex.a_remembers or ex.summary, importance=4, entities=[nb])
            remember(s, b, ex.b_remembers or ex.summary, importance=4, entities=[na])
            for x, y, v in ((a, b, ex.a_opinion_of_b), (b, a, ex.b_opinion_of_a)):
                if v:
                    s.emit("relationship_changed", {"a": x, "b": y, "delta": {"opinion": max(-20, min(20, v))}},
                           visibility="private")
        return []

    async def _greet_arrivals(self) -> list[str]:
        arrivals, self._arrivals = [a for a in self._arrivals if a in self.present_npcs()], []
        if not arrivals or not self.pc["alive"]:
            return []
        names = ", ".join(fx.name_of(self.store, a) for a in arrivals)
        return await self._reactions([f"{names} arrives, seeking {self.pc['name']}."], addressed=arrivals, impacts={})

    # ------------------------------------------------------------ director
    async def _direct(self, reason: str) -> list[str]:
        s = self.store
        sc = self.scene
        present = self.present_npcs()
        nearby = director.nearby_people(s, self.pc_id, sc)
        try:
            beat = await director.beat(self.llm, s, self.settings, self.pc_id, sc, self.idle_turns, reason)
        except SOFT_ERRORS:
            return []
        beat = self.warden.check_beat(beat, present, [n["id"] for n in nearby])
        lines: list[str] = []
        observed: list[str] = []
        if beat.hook:
            s.emit("hook", {"note": beat.note[:300]}, location_id=self.pc.get("location_id"), summary=beat.hook,
                   visibility="scene")
            lines.append(beat.hook)
            observed.append(beat.hook)
        for a in beat.arrivals:
            self._join_scene(a)
            s.emit("arrival", actors=[a], location_id=self.pc.get("location_id"),
                   summary=f"{fx.name_of(s, a)} arrives", visibility="scene")
            if not beat.hook:
                lines.append(f"{fx.name_of(s, a)} arrives.")
            observed.append(f"{fx.name_of(s, a)} arrives.")
        for d in beat.departures:
            where = self._leave_scene(d)
            s.emit("departure", actors=[d], summary=f"{fx.name_of(s, d)} leaves for {where}", visibility="scene")
            lines.append(f"{fx.name_of(s, d)} takes their leave.")
        fx.apply_effects(s, beat.effects, actor_id=None, context="director")
        if sc:
            s.emit("scene_updated", {"scene_id": sc["id"], "tension": beat.tension}, visibility="system")
        if beat.hook or beat.arrivals:
            self.idle_turns = 0
            lines += await self._reactions(observed, addressed=beat.arrivals, impacts={})
        return lines

    # ------------------------------------------------------------ narration
    def _notices(self) -> list[Block]:
        out = [Block("notice", n) for n in self.llm.notices]
        self.llm.notices.clear()
        b = self.llm.budget
        if b.pressure() > 0 and not b.warned:
            b.warned = True
            out.append(Block("notice", f"Session spend ${b.spent:.2f} has passed the warning line "
                                       f"(${b.warn_usd:.2f}); background agents will slow down."))
        return out

    def _options_blocks(self) -> list[Block]:
        if not self.options:
            return []
        mode = self.settings.game.odds_display
        rows = []
        for i, o in enumerate(self.options, 1):
            odds = self.compute_odds(o.resolve)
            tail = ""
            if odds and mode != "off":
                tail = f"  [{odds.render(mode)}]" + (" ⚠" if odds.lethal_risk >= 0.05 else "")
            rows.append(f"{i}. {o.label}{tail}")
        return [Block("options", "\n".join(rows))]

    async def _narrate(self, perception: list[str]) -> list[Block]:
        s = self.store
        sc = self.scene
        try:
            narr = await narrator.narrate(self.llm, s, self.settings, self.pc_id, sc, perception)
        except SOFT_ERRORS as e:
            self.llm.notices.append(f"Narrator unavailable ({str(e)[:100]}); showing the bare facts.")
            narr = narrator.fallback(perception)
        speech = [re.search(r'"(.+)"', p).group(0) for p in perception if re.search(r'says[^"]*: "(.+)"', p)]
        prose = narrator.ensure_lines(narr.prose.strip(), speech)
        present = self.present_npcs()
        options = []
        for o in narr.options[:5]:
            r, _ = self.warden.check_resolve(o.resolve, present)
            if r.kind in ("clarify", "ooc"):
                continue
            options.append(Option(label=o.label[:90], resolve=r))
        self.options = options
        self.last_prose = prose
        s.add_transcript("narration", prose)
        blocks = [Block("header", self.header()), Block("prose", prose)]
        blocks += self._options_blocks()
        blocks += self._notices()
        return blocks

    async def _death(self, perception: list[str]) -> list[Block]:
        s = self.store
        pc = self.pc
        cause = pc["status"].get("cause_of_death", "unknown")
        perception = perception + [f"You die: {cause}."]
        try:
            narr = await narrator.narrate(self.llm, s, self.settings, self.pc_id, self.scene, perception)
            prose = narr.prose
        except SOFT_ERRORS:
            prose = " ".join(perception)
        try:
            chron = await chronicler.chronicle(self.llm, s, self.pc_id)
        except SOFT_ERRORS:
            mems = [m["text"] for m in s.memories(self.pc_id, 12)]
            chron = Chronicle(title=f"The Chronicle of {pc['name']}",
                              text=f"{pc['name']} died: {cause}.\n\n" + "\n".join(reversed(mems)))
        self.ended = True
        self.options = []
        self.pending = None
        self.last_prose = prose
        s.meta_set("ended", True)
        s.meta_set("chronicle", chron.model_dump())
        s.add_transcript("death", prose)
        s.add_transcript("chronicle", chron.text)
        return [Block("header", self.header()), Block("prose", prose),
                Block("title", f"☠  {pc['name']} is dead.  ☠"),
                Block("title", chron.title), Block("chronicle", chron.text),
                Block("system", "Your life is over; the save is now a read-only chronicle. /new to begin another.")]

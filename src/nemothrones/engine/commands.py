"""Slash commands. Deterministic and LLM-free (except /lore deep). Everything the
player sees about the world is filtered through the player character's beliefs,
memories and relationships; ``/debug`` is the explicit out-of-character exception.

Session commands (/new, /load, /saves, /save, /quit) are handled by the REPL.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Awaitable, Callable

from ..agents import context as cx
from ..world.fate import read_fates
from . import clock
from .odds import fmt_pct

Handler = Callable[["Game", str], Awaitable[list]]  # noqa: F821
COMMANDS: dict[str, tuple[Handler, str]] = {}
KIN = {"father", "mother", "son", "daughter", "brother", "sister", "wife", "husband", "uncle", "aunt",
       "nephew", "niece", "cousin", "grandfather", "grandmother", "grandson", "granddaughter", "half-brother",
       "half-sister", "bastard son", "bastard daughter", "ward", "foster brother", "goodbrother", "goodsister",
       "goodfather", "goodmother", "twin"}


def command(name: str, help_text: str):
    def deco(fn: Handler) -> Handler:
        COMMANDS[name] = (fn, help_text)
        return fn
    return deco


def _b(kind: str, text: str):
    from .game import Block

    return Block(kind, text)


async def dispatch(game, text: str) -> list:
    parts = text[1:].strip().split(maxsplit=1)
    if not parts:
        return [_b("system", "Type /help for commands.")]
    name, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    entry = COMMANDS.get(name)
    if not entry:
        return [_b("system", f"Unknown command /{name}. Type /help.")]
    return await entry[0](game, arg.strip())


# ---------------------------------------------------------------- help
@command("help", "this list")
async def c_help(game, arg):
    rows = [f"/{k:<10} {v}" for k, (_, v) in sorted(COMMANDS.items())]
    rows += ["/new       begin a new life", "/load      load a save", "/saves     list saves",
             "/save      snapshot this save", "/quit      leave (the game autosaves every turn)",
             "", "Play by typing what you do or say. Quote speech: \"My lord, a word.\"",
             "Pick a numbered option by typing its number."]
    return [_b("sheet", "\n".join(rows))]


# ---------------------------------------------------------------- self
@command("me", "your character sheet")
async def c_me(game, arg):
    s = game.store
    pc = game.pc
    p = pc["persona"]
    sk = pc["attrs"].get("skills", {})
    age = clock.age_years(pc.get("born_t"), s.now)
    lines = [f"{pc['name']}" + (f", {', '.join(p.get('titles', [])[:3])}" if p.get("titles") else ""),
             f"House: {cx.house_name(s, pc.get('house_id')) or '—'}    Age: {age if age is not None else 'unknown'}"
             f"    Culture: {p.get('culture') or '—'}    Faith: {p.get('faith') or '—'}",
             f"Where: {cx.where(s, pc.get('location_id'), pc['status'].get('sublocation'))}",
             f"Condition: {cx.health_line(pc)}",
             "Skills: " + "  ".join(f"{k} {v}" for k, v in sk.items()),
             f"Traits: {', '.join(p.get('traits', [])) or '—'}"]
    if p.get("possessions"):
        lines.append(f"Possessions: {', '.join(p['possessions'])}")
    goals = cx.goals_view(s, game.pc_id)
    if goals:
        lines.append("Aims: " + "; ".join(goals[:5]))
    if p.get("secrets"):
        lines.append("Secrets you keep: " + "; ".join(p["secrets"]))
    return [_b("sheet", "\n".join(lines))]


@command("goals", "your ambitions and plans (/goal add <text>, /goal done <n>)")
async def c_goals(game, arg):
    gs = game.store.goals(game.pc_id)
    if not gs:
        return [_b("sheet", "You have set yourself no aims. /goal add <text>")]
    return [_b("sheet", "\n".join(f"{i}. [{g['level']}] {g['text']}" for i, g in enumerate(gs, 1)))]


@command("goal", "add or complete an aim: /goal add <text> | /goal done <n>")
async def c_goal(game, arg):
    s = game.store
    sub, _, rest = arg.partition(" ")
    if sub == "add" and rest.strip():
        with s.transaction():
            s.emit("goal_added", {"owner": game.pc_id, "level": "plan", "text": rest.strip()[:300], "by_player": True},
                   visibility="private")
        return [_b("system", f"Aim recorded: {rest.strip()}")]
    if sub in ("done", "drop") and rest.strip().isdigit():
        gs = s.goals(game.pc_id)
        i = int(rest) - 1
        if 0 <= i < len(gs):
            with s.transaction():
                s.emit("goal_updated", {"goal_id": gs[i]["id"], "status": "done" if sub == "done" else "abandoned"},
                       visibility="private")
            return [_b("system", f"{'Completed' if sub == 'done' else 'Abandoned'}: {gs[i]['text']}")]
    return [_b("system", "Usage: /goal add <text>  |  /goal done <n>  |  /goal drop <n>")]


@command("inventory", "what you carry and own")
async def c_inventory(game, arg):
    items = game.pc["persona"].get("possessions", [])
    return [_b("sheet", "You carry: " + (", ".join(items) if items else "nothing of note."))]


@command("journal", "what you remember of recent days")
async def c_journal(game, arg):
    mems = game.store.memories(game.pc_id, 15)
    if not mems:
        return [_b("sheet", "Your journal is empty.")]
    rows = [f"({clock.short_date(m['t'])}) {textwrap.shorten(m['text'], 220)}" for m in reversed(mems)]
    return [_b("sheet", "\n".join(rows))]


@command("relations", "how you feel about those you know")
async def c_relations(game, arg):
    rows = cx.relationships_view(game.store, game.pc_id, 20)
    return [_b("sheet", "\n".join(rows) if rows else "You have formed no strong feelings yet.")]


@command("family", "your kin")
async def c_family(game, arg):
    s = game.store
    rows = []
    for r in s.relationships_of(game.pc_id):
        if r["kind"].lower() in KIN or any(k in r["kind"].lower() for k in ("brother", "sister", "son", "daughter", "father", "mother", "wife", "husband", "uncle", "cousin")):
            c = s.character(r["b"])
            if c:
                rows.append(f"{r['kind']}: {c['name']}")
    return [_b("sheet", "\n".join(rows) if rows else "You know of no kin here.")]


# ---------------------------------------------------------------- scene
@command("look", "describe the scene again")
async def c_look(game, arg):
    out = [_b("header", game.header()), _b("prose", game.last_prose or "You look about you.")]
    out += game._options_blocks()
    return out


@command("who", "who is here, as you know them")
async def c_who(game, arg):
    s = game.store
    present = game.present_npcs()
    if not present:
        return [_b("sheet", "You are alone.")]
    rows = []
    for cid in present:
        card = cx.public_card(s, s.character(cid), game.pc_id)
        rel = card.get("your_relation", {})
        title = f", {card['titles'][0]}" if card.get("titles") else ""
        age = f", about {card['age']}" if card.get("age") is not None else ""
        rows.append(f"{card['name']}{title}{age} — {card['condition']}; you: {rel.get('kind', 'stranger')}, "
                    f"opinion {rel.get('opinion', 0):+d}, trust {rel.get('trust', 0):+d}")
    return [_b("sheet", "\n".join(rows))]


@command("why", "the odds behind the last roll")
async def c_why(game, arg):
    r = game.store.last_roll()
    if not r:
        return [_b("sheet", "No dice have been cast yet.")]
    from .odds import OddsTable

    table = OddsTable(r["kind"], r["labels"], r["probs"], [False] * len(r["labels"]), r["derivation"])
    probs = " · ".join(f"{label} {fmt_pct(p)}" for label, p in zip(r["labels"], r["probs"], strict=True))
    return [_b("sheet", f"Last roll — {r['action']}\n  {probs}\n  {table.explain()}\n"
                        f"  rolled u = {r['u']:.3f} → {r['labels'][r['outcome_index']]}")]


# ---------------------------------------------------------------- knowledge
@command("know", "what you know of a person, house, place or matter")
async def c_know(game, arg):
    s = game.store
    if not arg:
        return [_b("system", "Usage: /know <name, house, place or topic>")]
    rows = []
    c = s.find_character(arg)
    if c:
        if c["id"] == game.pc_id:
            return await c_me(game, "")
        rel = s.relationship(game.pc_id, c["id"])
        rows.append(f"{c['name']}: {rel['kind']}; your opinion {rel['opinion']:+d}, trust {rel['trust']:+d}")
    for b in s.search_beliefs(game.pc_id, arg, limit=10):
        conf = "" if b["confidence"] >= 0.9 else " (uncertain)"
        rows.append(f"• {b['subject']}: {b['content']}{conf}  [as of {clock.short_date(b['learned_t'])}, {b['source']}]")
    for m in s.search_memories(game.pc_id, arg, limit=5):
        rows.append(f"• you recall ({clock.short_date(m['t'])}): {textwrap.shorten(m['text'], 200)}")
    if not rows:
        return [_b("sheet", f"You have never heard of '{arg}'.")]
    return [_b("sheet", "\n".join(rows))]


@command("house", "what you know of a house (default: your own)")
async def c_house(game, arg):
    s = game.store
    h = s.find_house(arg) if arg else s.house(game.pc["house_id"])
    if not h:
        return [_b("sheet", f"You know of no house called '{arg}'.")]
    seat = cx.loc_name(s, h.get("seat_id")) if h.get("seat_id") else "—"
    rows = [h["name"], f"Words: {h.get('words') or '—'}", f"Sigil: {h.get('sigil') or '—'}", f"Seat: {seat}"]
    for b in s.search_beliefs(game.pc_id, [h["name"].removeprefix("House ")], limit=8):
        rows.append(f"• {b['subject']}: {b['content']}  [as of {clock.short_date(b['learned_t'])}]")
    return [_b("sheet", "\n".join(rows))]


@command("realm", "the state of the realm, as you have heard it")
async def c_realm(game, arg):
    s = game.store
    hits = s.search_beliefs(game.pc_id, ["king", "throne", "hand", "realm", "war", "queen", "council"], limit=12)
    if not hits:
        return [_b("sheet", "You have heard little of the wider realm.")]
    return [_b("sheet", "\n".join(f"• {b['subject']}: {b['content']}  [as of {clock.short_date(b['learned_t'])}, {b['source']}]"
                                  for b in hits))]


@command("letters", "letters you have received and sent")
async def c_letters(game, arg):
    s = game.store
    rows = []
    for d in s.conn.execute("SELECT * FROM deliveries WHERE recipient_id=? AND delivered=1 ORDER BY deliver_t DESC LIMIT 10",
                            (game.pc_id,)):
        sender = s.character(d["sender_id"])
        payload = json.loads(d["payload"])
        rows.append(f"← ({clock.short_date(d['deliver_t'])}) from {sender['name'] if sender else '?'}: "
                    f"{textwrap.shorten(payload.get('content', ''), 240)}")
    for d in s.conn.execute("SELECT * FROM deliveries WHERE sender_id=? ORDER BY sent_t DESC LIMIT 10", (game.pc_id,)):
        to = s.character(d["recipient_id"])
        state = {0: "on its way", 1: "delivered", 2: "delivered", 3: "undeliverable"}.get(d["delivered"], "?")
        payload = json.loads(d["payload"])
        rows.append(f"→ ({clock.short_date(d['sent_t'])}) to {to['name'] if to else '?'} [{state}]: "
                    f"{textwrap.shorten(payload.get('content', ''), 160)}")
    return [_b("sheet", "\n".join(rows) if rows else "No letters.")]


# ---------------------------------------------------------------- time
@command("date", "the date, hour and season")
async def c_date(game, arg):
    s = game.store
    return [_b("sheet", f"It is the {clock.format_date(s.now)}. The season: {s.meta_get('season', 'unknown')}.")]


@command("wait", "let time pass: /wait 2 hours | 3 days | a fortnight")
async def c_wait(game, arg):
    if game.ended or game.readonly:
        return [_b("system", "Time no longer passes for you.")]
    try:
        minutes = clock.parse_duration(arg or "1 hour")
    except ValueError as e:
        return [_b("system", str(e))]
    return await game._guard(game.wait, minutes, "wait")


@command("sleep", "sleep until morning")
async def c_sleep(game, arg):
    if game.ended or game.readonly:
        return [_b("system", "Time no longer passes for you.")]
    minutes = clock.next_morning(game.store.now) - game.store.now
    return await game._guard(game.wait, minutes, "sleep")


# ---------------------------------------------------------------- out of character
@command("lore", "WOIAF lore, filtered to the current date: /lore <topic> | /lore deep <question>")
async def c_lore(game, arg):
    if not arg:
        return [_b("system", "Usage: /lore <topic>")]
    if game.lore is None:
        return [_b("system", "The lore service (WOIAF) is not available in this session.")]
    return await game.lore.command(game, arg)


@command("history", "the story so far (out of character)")
async def c_history(game, arg):
    rows = game.store.transcript(30)
    out = []
    for r in rows:
        prefix = "> " if r["kind"] == "player" else ""
        out.append(prefix + textwrap.shorten(r["text"], 300))
    return [_b("sheet", "\n".join(out) if out else "Nothing has happened yet.")]


@command("cost", "LLM spend and calls by role")
async def c_cost(game, arg):
    s = game.store
    b = game.llm.budget
    rows = [f"This session: ${b.spent:.4f} ({b.tokens_in:,} in / {b.tokens_out:,} out tokens); "
            f"warn at ${b.warn_usd:.2f}, stop at ${b.hard_usd:.2f}.",
            f"This save, all sessions: ${float(s.meta_get('spent', 0.0)) + b.spent - getattr(game, '_spent_mark', 0.0):.4f}"]
    for r in s.llm_totals():
        rows.append(f"  {r['role']:<11} {r['backend']:<16} {r['calls']:>4} calls  ${r['cost'] or 0:.4f}"
                    f"  avg {r['lat'] or 0:.1f}s  failures {r['failures'] or 0}")
    return [_b("sheet", "\n".join(rows))]


@command("settings", "per-save settings")
async def c_settings(game, arg):
    g = game.settings.game
    return [_b("sheet", f"odds display: {g.odds_display} (/odds exact|fuzzy|off)\n"
                        f"verbosity: {g.verbosity} (/verbosity terse|standard|lush)\n"
                        f"canon gravity: {g.canon_gravity:.2f} (/gravity 0-1; 0.6 = butterfly)")]


def _save_setting(game, key, value):
    s = game.store
    cur = s.meta_get("settings", {}) or {}
    cur[key] = value
    with s.transaction():
        s.meta_set("settings", cur)
    setattr(game.settings.game, key, value)


@command("odds", "how odds are shown: exact | fuzzy | off")
async def c_odds(game, arg):
    if arg not in ("exact", "fuzzy", "off"):
        return [_b("system", "Usage: /odds exact|fuzzy|off")]
    _save_setting(game, "odds_display", arg)
    return [_b("system", f"Odds will be shown: {arg}.")]


@command("verbosity", "narration length: terse | standard | lush")
async def c_verbosity(game, arg):
    if arg not in ("terse", "standard", "lush"):
        return [_b("system", "Usage: /verbosity terse|standard|lush")]
    _save_setting(game, "verbosity", arg)
    return [_b("system", f"Narration: {arg}.")]


@command("gravity", "canon gravity 0..1 (0 sandbox, 0.6 butterfly, 1 fated)")
async def c_gravity(game, arg):
    try:
        v = max(0.0, min(1.0, float(arg)))
    except ValueError:
        return [_b("system", "Usage: /gravity 0.6")]
    _save_setting(game, "canon_gravity", v)
    return [_b("system", f"Canon gravity set to {v:.2f}.")]


# ---------------------------------------------------------------- debug (OOC, spoilers)
@command("debug", "OOC inspection: truth <x> | agents | canon | llm | thoughts | rebuild")
async def c_debug(game, arg):
    s = game.store
    sub, _, rest = arg.partition(" ")
    if sub == "truth":
        c = s.find_character(rest)
        if not c:
            return [_b("system", f"No character '{rest}'.")]
        view = {k: c[k] for k in ("id", "name", "alive", "location_id", "tier", "next_act_t", "importance")}
        view["status"] = c["status"]
        view["wounds"] = c["attrs"].get("wounds")
        view["plans"] = [g["text"] for g in s.goals(c["id"])]
        return [_b("sheet", json.dumps(view, indent=1, ensure_ascii=False))]
    if sub == "agents":
        rows = []
        for c in s.characters(alive=True):
            nxt = clock.short_date(c["next_act_t"]) if c.get("next_act_t") else "—"
            rows.append(f"T{c['tier']} {c['name']:<28} at {cx.loc_name(s, c.get('location_id')):<20} next {nxt}")
        return [_b("sheet", "\n".join(rows))]
    if sub == "canon":
        rows = [f"[{ce['status']}] {clock.short_date(ce['t_expected']) if ce.get('t_expected') else '—'} {ce['title']}"
                for ce in s.canon_events()]
        fates = read_fates(s)
        rows.append(f"({len(fates)} fates recorded; spoilers withheld)")
        return [_b("sheet", "\n".join(rows) if rows else "No canon events recorded.")]
    if sub == "llm":
        rows = [f"#{r['id']} t{r['turn']} {r['role']:<10} {r['backend']:<15} {r['tokens_in']}/{r['tokens_out']} tok"
                f" ${r['cost'] or 0:.4f} {r['latency'] or 0:.1f}s {'ok' if r['ok'] else 'FAIL ' + (r['error'] or '')[:80]}"
                for r in s.llm_rows(20)]
        rows.append("GM served by: " + ", ".join(f"{k}={v}" for k, v in game.llm.last_backend.items()))
        return [_b("sheet", "\n".join(rows))]
    if sub == "thoughts":
        rows = [f"{e['summary'].removesuffix(' thinks')}: {e['data'].get('thought', '')}"
                for e in s.events(kinds=("thought",), desc=True, limit=8)]
        return [_b("sheet", "\n".join(reversed(rows)) if rows else "No thoughts recorded.")]
    if sub == "rebuild":
        fresh = s.rebuild()
        same = fresh.snapshot() == s.snapshot()
        fresh.close()
        return [_b("sheet", f"Event-log replay {'matches' if same else 'DIFFERS FROM'} the materialised world.")]
    return [_b("system", "Usage: /debug truth <x> | agents | canon | llm | thoughts | rebuild")]

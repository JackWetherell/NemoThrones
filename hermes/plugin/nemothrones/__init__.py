"""NemoThrones plugin for Hermes Agent: read-only tools for the Game Master.

The NemoThrones engine is the only writer of game state. These tools open the
active save and the shared WOIAF cache READ-ONLY, so the Hermes GM can look things
up mid-reasoning. Stdlib only: Hermes runs its own venv.

Install:  copy this directory to ~/.hermes/plugins/nemothrones/ (in the sandbox:
/sandbox/.hermes/plugins/nemothrones/) and add `nemothrones` to `plugins.enabled`
in ~/.hermes/config.yaml. `scripts/deploy.sh` in the NemoThrones repo does both.

The registration call follows the Hermes plugin guide (ctx.register_tool with a
name, toolset, schema and handler). If your Hermes version uses a different
signature, adjust `_register` below.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

TOOLSET = "nemothrones"


def _home() -> Path:
    env = os.environ.get("NEMOTHRONES_HOME")
    if env:
        return Path(env)
    sandbox = Path("/sandbox/.hermes/workspace/nemothrones")
    return sandbox if sandbox.is_dir() else Path.home() / ".nemothrones"


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _save() -> sqlite3.Connection | None:
    pointer = _home() / "active_save"
    if not pointer.is_file():
        return None
    path = Path(pointer.read_text().strip())
    return _connect(path) if path.is_file() else None


def _j(v):
    try:
        return json.loads(v) if isinstance(v, str) else v
    except json.JSONDecodeError:
        return v


def _out(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)[:12000]


def _args(args, kw, keys):
    a = dict(args or {}) if isinstance(args, dict) else {}
    for k in keys:
        if k in kw and k not in a:
            a[k] = kw[k]
    return a


# ------------------------------------------------------------------ tools
def nt_world(args=None, **kw) -> str:
    """Ground truth about a character, house or location (GM-only; never fate)."""
    a = _args(args, kw, ("kind", "id"))
    conn = _save()
    if conn is None:
        return _out({"error": "no active NemoThrones save"})
    kind, ref = a.get("kind", "character"), str(a.get("id", "")).strip()
    try:
        if kind == "character":
            row = conn.execute("SELECT * FROM characters WHERE id=? OR lower(name)=lower(?)", (ref, ref)).fetchone()
            if not row:
                return _out({"error": f"no character {ref!r}"})
            d = {k: _j(row[k]) for k in row.keys()}
            d["relationships"] = [dict(r) for r in conn.execute(
                "SELECT b, kind, opinion, trust, fear, respect FROM relationships WHERE a=? LIMIT 20", (d["id"],))]
            d["plans"] = [r[0] for r in conn.execute(
                "SELECT text FROM goals WHERE owner_id=? AND status='active'", (d["id"],))]
            return _out(d)
        if kind == "house":
            row = conn.execute("SELECT * FROM houses WHERE id=? OR lower(name)=lower(?)", (ref, ref)).fetchone()
            return _out(dict(row) if row else {"error": f"no house {ref!r}"})
        if kind == "location":
            row = conn.execute("SELECT * FROM locations WHERE id=? OR lower(name)=lower(?)", (ref, ref)).fetchone()
            if not row:
                return _out({"error": f"no location {ref!r}"})
            d = dict(row)
            d["people"] = [r[0] for r in conn.execute(
                "SELECT name FROM characters WHERE location_id=? AND alive=1", (d["id"],))]
            return _out(d)
        if kind == "events":
            rows = conn.execute(
                "SELECT t, kind, summary FROM events WHERE visibility NOT IN ('system','director','private')"
                " ORDER BY id DESC LIMIT 30").fetchall()
            return _out([dict(r) for r in rows])
        return _out({"error": "kind must be character | house | location | events"})
    finally:
        conn.close()


def nt_scene(args=None, **kw) -> str:
    """The player's current scene: place, time, who is present."""
    conn = _save()
    if conn is None:
        return _out({"error": "no active NemoThrones save"})
    try:
        sid = _j((conn.execute("SELECT value FROM meta WHERE key='scene_id'").fetchone() or [None])[0])
        now = _j((conn.execute("SELECT value FROM meta WHERE key='now_t'").fetchone() or [0])[0])
        row = conn.execute("SELECT * FROM scenes WHERE id=?", (sid,)).fetchone() if sid else None
        if not row:
            return _out({"error": "no scene"})
        d = {k: _j(row[k]) for k in row.keys()}
        d["now_t"] = now
        d["present_names"] = [r[0] for r in conn.execute(
            f"SELECT name FROM characters WHERE id IN ({','.join('?' * len(d['present']))})", d["present"])]
        return _out(d)
    finally:
        conn.close()


def nt_lore(args=None, **kw) -> str:
    """Cached A Wiki of Ice and Fire page (infobox + lead). Cache only: if missing, use web tools on
    awoiaf.westeros.org. Respect `as_of_year`: never reveal events after it."""
    a = _args(args, kw, ("title", "as_of_year"))
    path = _home() / "lore.db"
    if not path.is_file():
        return _out({"error": "no lore cache yet"})
    conn = _connect(path)
    try:
        title = str(a.get("title", "")).strip()
        row = conn.execute("SELECT title, revid, wikitext FROM pages WHERE lower(title)=lower(?)", (title,)).fetchone()
        if not row:
            row = conn.execute("SELECT title, revid, wikitext FROM pages WHERE title LIKE ? LIMIT 1",
                               (f"%{title}%",)).fetchone()
        if not row:
            return _out({"error": f"{title!r} not cached; search awoiaf.westeros.org"})
        text = row["wikitext"] or ""
        info = dict(re.findall(r"^\|\s*([A-Za-z ]+?)\s*=\s*(.+)$", text[:6000], re.M))
        lead = re.sub(r"\{\{.*?\}\}|\[\[(?:[^|\]]*\|)?([^\]]*)\]\]|<ref.*?</ref>|<.*?>", r"\1", text, flags=re.S)
        return _out({"title": row["title"], "revid": row["revid"], "infobox": info,
                     "excerpt": lead.strip()[:2500], "as_of_year": a.get("as_of_year"),
                     "license": "Text from A Wiki of Ice and Fire, CC BY-SA"})
    finally:
        conn.close()


def nt_canon(args=None, **kw) -> str:
    """Director-only canon horizon: pending canon events (never reveal to the player)."""
    conn = _save()
    if conn is None:
        return _out({"error": "no active NemoThrones save"})
    try:
        rows = conn.execute("SELECT id, t_expected, title, summary, status FROM canon_events ORDER BY t_expected")
        return _out([dict(r) for r in rows])
    finally:
        conn.close()


TOOLS = [
    (nt_world, {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["character", "house", "location", "events"]},
        "id": {"type": "string", "description": "id or exact name"}}, "required": ["kind"]}),
    (nt_scene, {"type": "object", "properties": {}}),
    (nt_lore, {"type": "object", "properties": {
        "title": {"type": "string", "description": "WOIAF page title"},
        "as_of_year": {"type": "integer", "description": "in-game year AC; nothing after it may be revealed"}},
        "required": ["title"]}),
    (nt_canon, {"type": "object", "properties": {}}),
]


def _register(ctx, fn, params) -> None:
    schema = {"name": fn.__name__, "description": (fn.__doc__ or "").strip(), "parameters": params}
    try:
        ctx.register_tool(name=fn.__name__, toolset=TOOLSET, schema=schema, handler=fn)
    except TypeError:
        ctx.register_tool(fn.__name__, TOOLSET, schema, fn)


def register(ctx) -> None:
    for fn, params in TOOLS:
        _register(ctx, fn, params)

"""The world store: an event-sourced SQLite database.

Every change to the world is an event appended to ``events`` and applied by a
reducer in the same transaction. ``rebuild()`` replays the log into a fresh database,
which is how tests prove the materialised tables are a pure function of the log.
The engine is the only writer; the Hermes plugin opens saves read-only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any

WORLD_TABLES = (
    "characters", "fate", "relationships", "houses", "locations", "routes", "beliefs",
    "memories", "goals", "deliveries", "canon_events", "scenes",
)
FTS_TABLES = ("beliefs_fts", "memories_fts")
WORLD_META = ("now_t", "start_t", "player_id", "season", "title", "scene_id")
JSON_COLS = {"attrs", "persona", "status", "data", "actors", "causes", "present",
             "participants", "preconditions", "drivers"}
REL_FIELDS = ("opinion", "trust", "fear", "respect")


def _schema() -> str:
    return resources.files("nemothrones.world").joinpath("schema.sql").read_text()


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _clamp(v: float, lo: int = -100, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(v))))


def fts_query(terms: list[str] | str, op: str = "OR") -> str | None:
    """Build a safe FTS5 query (OR by default) from free text or a list of terms."""
    if isinstance(terms, str):
        terms = [terms]
    words: list[str] = []
    for term in terms:
        for w in re.findall(r"[A-Za-z][A-Za-z']{2,}", term or ""):
            w = w.strip("'").lower()
            if w and w not in _STOP and w not in words:
                words.append(w)
    if not words:
        return None
    return f" {op} ".join(f'"{w}"' for w in words[:24])


_STOP = set("""the and for with that this from your you are was were his her she him they them
their what when where which who whom will would shall should could have has had not but into
upon about there here then than been being its our ours out off over very just also some any
all one two lord lady ser""".split())


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = str(path)
        if readonly:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            self.conn = sqlite3.connect(self.path, isolation_level=None)
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_schema())
        self.conn.row_factory = sqlite3.Row
        self.turn = 0
        self._in_tx = False

    # ------------------------------------------------------------------ basics
    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._in_tx:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._in_tx = False

    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def meta_set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, _dumps(value)))

    @property
    def now(self) -> int:
        return int(self.meta_get("now_t", 0))

    @property
    def player_id(self) -> str | None:
        return self.meta_get("player_id")

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, data: dict | None = None, *, t: int | None = None,
             actors: list[str] | tuple = (), location_id: str | None = None,
             summary: str = "", visibility: str = "public", causes: list[int] | tuple = (),
             canon_ref: str | None = None) -> int:
        data = data or {}
        t = self.now if t is None else int(t)
        cur = self.conn.execute(
            "INSERT INTO events(t, turn, kind, location_id, actors, summary, data, visibility, causes, canon_ref)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (t, self.turn, kind, location_id, _dumps(list(actors)), summary, _dumps(data),
             visibility, _dumps(list(causes)), canon_ref),
        )
        eid = int(cur.lastrowid)
        reducer = REDUCERS.get(kind)
        if reducer:
            reducer(self.conn, eid, t, data)
        return eid

    def events(self, *, since_id: int = 0, kinds: tuple[str, ...] | None = None,
               visibility: tuple[str, ...] | None = None, limit: int | None = None,
               desc: bool = False) -> list[dict]:
        q = "SELECT * FROM events WHERE id > ?"
        args: list[Any] = [since_id]
        if kinds:
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        if visibility:
            q += f" AND visibility IN ({','.join('?' * len(visibility))})"
            args += list(visibility)
        q += " ORDER BY id " + ("DESC" if desc else "ASC")
        if limit:
            q += f" LIMIT {int(limit)}"
        return [_row(r) for r in self.conn.execute(q, args)]

    # ------------------------------------------------------------- characters
    def character(self, cid: str | None) -> dict | None:
        if not cid:
            return None
        r = self.conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        return _row(r) if r else None

    def characters(self, *, alive: bool | None = True, location_id: str | None = None,
                   include_player: bool = True) -> list[dict]:
        q = "SELECT * FROM characters WHERE 1=1"
        args: list[Any] = []
        if alive is not None:
            q += " AND alive=?"
            args.append(1 if alive else 0)
        if location_id:
            q += " AND location_id=?"
            args.append(location_id)
        if not include_player:
            q += " AND is_player=0"
        q += " ORDER BY importance DESC, id"
        return [_row(r) for r in self.conn.execute(q, args)]

    def find_character(self, query: str, *, among: list[str] | None = None) -> dict | None:
        """Resolve an id, a full name, a wiki title, an alias, or an unambiguous first name."""
        if not query:
            return None
        raw = query.strip().lower().strip("\"'.,!?")
        q = re.sub(r"^(lord|lady|ser|maester|king|queen|prince|princess|septa|septon)\s+", "", raw)
        keys = {raw, q}
        pool = [c for c in self.characters(alive=None) if among is None or c["id"] in among]
        for c in pool:
            if keys & {c["id"].lower(), c["name"].lower(), (c["wiki_title"] or "").lower()}:
                return c
        for c in pool:
            if keys & {a.lower() for a in c["persona"].get("aliases", [])}:
                return c
        first = [c for c in pool if c["name"].lower().split()[0] == q]
        if len(first) == 1:
            return first[0]
        partial = [c for c in pool if q and q in c["name"].lower()]
        if len(partial) == 1:
            return partial[0]
        alive = [c for c in first if c["alive"]]
        return alive[0] if len(alive) == 1 else None

    def present_at(self, location_id: str | None, sublocation: str | None) -> list[str]:
        out = []
        for c in self.characters(alive=True, location_id=location_id):
            if (c["status"].get("sublocation") or "") == (sublocation or ""):
                out.append(c["id"])
        return out

    # ------------------------------------------------------- relationships etc
    def relationship(self, a: str, b: str) -> dict:
        r = self.conn.execute("SELECT * FROM relationships WHERE a=? AND b=?", (a, b)).fetchone()
        if r:
            return dict(r)
        return {"a": a, "b": b, "kind": "stranger", "opinion": 0, "trust": 0, "fear": 0,
                "respect": 0, "notes": ""}

    def relationships_of(self, a: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM relationships WHERE a=? ORDER BY abs(opinion)+abs(trust) DESC", (a,))]

    def house(self, hid: str | None) -> dict | None:
        if not hid:
            return None
        r = self.conn.execute("SELECT * FROM houses WHERE id=?", (hid,)).fetchone()
        return _row(r) if r else None

    def houses(self) -> list[dict]:
        return [_row(r) for r in self.conn.execute("SELECT * FROM houses ORDER BY name")]

    def find_house(self, query: str) -> dict | None:
        q = query.strip().lower()
        q2 = q if q.startswith("house ") else f"house {q}"
        for h in self.houses():
            if q in (h["id"].lower(), h["name"].lower()) or q2 == h["name"].lower():
                return h
        return None

    def location(self, lid: str | None) -> dict | None:
        if not lid:
            return None
        r = self.conn.execute("SELECT * FROM locations WHERE id=?", (lid,)).fetchone()
        return _row(r) if r else None

    def locations(self) -> list[dict]:
        return [_row(r) for r in self.conn.execute("SELECT * FROM locations ORDER BY name")]

    def find_location(self, query: str) -> dict | None:
        q = (query or "").strip().lower()
        if not q:
            return None
        locs = self.locations()
        for loc in locs:
            names = [loc["id"].lower(), loc["name"].lower()] + [a.lower() for a in loc["data"].get("aliases", [])]
            if q in names:
                return loc
        partial = [loc for loc in locs if q in loc["name"].lower()]
        return partial[0] if len(partial) == 1 else None

    def goals(self, owner: str, status: str | None = "active") -> list[dict]:
        q = "SELECT * FROM goals WHERE owner_id=?"
        args: list[Any] = [owner]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY CASE level WHEN 'ambition' THEN 0 WHEN 'plan' THEN 1 ELSE 2 END, id"
        return [dict(r) for r in self.conn.execute(q, args)]

    # ------------------------------------------------------ beliefs & memory
    def beliefs(self, holder: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM beliefs WHERE holder_id=? ORDER BY learned_t DESC, id DESC LIMIT ?",
            (holder, limit))]

    def _fts(self, sql: str, owner: str, terms: list[str] | str, limit: int) -> list[dict]:
        """AND-match first (precise), then fill with OR-matches (broad)."""
        out: dict[int, dict] = {}
        for op in ("AND", "OR"):
            q = fts_query(terms, op)
            if not q:
                return []
            for r in self.conn.execute(sql, (q, owner, limit)):
                out.setdefault(r["id"], dict(r))
            if len(out) >= limit:
                break
        return list(out.values())[:limit]

    def search_beliefs(self, holder: str, terms: list[str] | str, limit: int = 8) -> list[dict]:
        return self._fts(
            "SELECT b.*, bm25(beliefs_fts) AS rank FROM beliefs_fts JOIN beliefs b ON b.id = beliefs_fts.rowid"
            " WHERE beliefs_fts MATCH ? AND b.holder_id=? ORDER BY rank LIMIT ?", holder, terms, limit)

    def memories(self, owner: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM memories WHERE owner_id=? ORDER BY t DESC, id DESC LIMIT ?", (owner, limit))]

    def search_memories(self, owner: str, terms: list[str] | str, limit: int = 30) -> list[dict]:
        return self._fts(
            "SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid"
            " WHERE memories_fts MATCH ? AND m.owner_id=? ORDER BY rank LIMIT ?", owner, terms, limit)

    # ----------------------------------------------------------------- scenes
    def scene(self, sid: int | None = None) -> dict | None:
        if sid is None:
            sid = self.meta_get("scene_id")
        if sid is None:
            return None
        r = self.conn.execute("SELECT * FROM scenes WHERE id=?", (sid,)).fetchone()
        return _row(r) if r else None

    def due_deliveries(self, now: int) -> list[dict]:
        return [_row(r) for r in self.conn.execute(
            "SELECT * FROM deliveries WHERE delivered=0 AND deliver_t<=? ORDER BY deliver_t, id", (now,))]

    def canon_events(self, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM canon_events"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY t_expected, id"
        return [_row(r) for r in self.conn.execute(q, args)]

    # ------------------------------------------------------------------- logs
    def log_roll(self, *, actor_id: str, action: str, kind: str, labels: list[str],
                 probs: list[float], derivation: dict, u: float, outcome_index: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO rolls(t, turn, actor_id, action, kind, labels, probs, derivation, u, outcome_index)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.now, self.turn, actor_id, action, kind, _dumps(labels), _dumps(probs),
             _dumps(derivation), u, outcome_index))
        return int(cur.lastrowid)

    def last_roll(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM rolls ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("labels", "probs", "derivation"):
            d[k] = json.loads(d[k])
        return d

    def log_llm(self, rec: dict) -> None:
        self.conn.execute(
            "INSERT INTO llm_log(ts, turn, role, backend, agent_id, prompt_hash, request, response,"
            " tokens_in, tokens_out, cost, latency, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["ts"], self.turn, rec["role"], rec["backend"], rec.get("agent_id"), rec["prompt_hash"],
             rec["request"], rec["response"], rec["tokens_in"], rec["tokens_out"], rec["cost"],
             rec["latency"], 1 if rec["ok"] else 0, rec.get("error")))

    def llm_rows(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, turn, role, backend, agent_id, tokens_in, tokens_out, cost, latency, ok, error"
            " FROM llm_log ORDER BY id DESC LIMIT ?", (limit,))]

    def llm_totals(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT role, backend, count(*) AS calls, sum(tokens_in) AS tin, sum(tokens_out) AS tout,"
            " sum(cost) AS cost, avg(latency) AS lat, sum(1-ok) AS failures FROM llm_log"
            " GROUP BY role, backend ORDER BY cost DESC")]

    def add_transcript(self, kind: str, text: str) -> None:
        self.conn.execute("INSERT INTO transcript(turn, t, kind, text) VALUES (?,?,?,?)",
                          (self.turn, self.now, kind, text))

    def transcript(self, limit: int = 40) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM transcript ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------------------------------------------------------------- rebuild
    def rebuild(self, path: str | Path = ":memory:") -> Store:
        """Replay the event log into a fresh store (world tables only)."""
        fresh = Store(path)
        with fresh.transaction():
            for ev in self.conn.execute("SELECT * FROM events ORDER BY id"):
                ev = dict(ev)
                fresh.conn.execute(
                    "INSERT INTO events(id, t, turn, kind, location_id, actors, summary, data, visibility, causes, canon_ref)"
                    " VALUES (:id,:t,:turn,:kind,:location_id,:actors,:summary,:data,:visibility,:causes,:canon_ref)", ev)
                reducer = REDUCERS.get(ev["kind"])
                if reducer:
                    reducer(fresh.conn, ev["id"], ev["t"], json.loads(ev["data"]))
        return fresh

    def snapshot(self) -> dict[str, list[tuple]]:
        snap: dict[str, list[tuple]] = {}
        for t in WORLD_TABLES:
            snap[t] = [tuple(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
        for t in FTS_TABLES:
            snap[t] = [tuple(r) for r in self.conn.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid")]
        snap["meta"] = [(k, self.meta_get(k)) for k in WORLD_META]
        return snap


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in JSON_COLS:
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


# =============================================================== reducers
def _meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, _dumps(value)))


def _insert_character(conn: sqlite3.Connection, c: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO characters(id, name, wiki_title, house_id, born_t, alive, death_t,"
        " location_id, tier, next_act_t, last_act_t, importance, canon, is_player, attrs, persona, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c["id"], c["name"], c.get("wiki_title"), c.get("house_id"), c.get("born_t"),
         1 if c.get("alive", True) else 0, c.get("death_t"), c.get("location_id"),
         int(c.get("tier", 2)), c.get("next_act_t"), c.get("last_act_t"),
         float(c.get("importance", 0.5)), 1 if c.get("canon", True) else 0,
         1 if c.get("is_player") else 0, _dumps(c.get("attrs", {})), _dumps(c.get("persona", {})),
         _dumps(c.get("status", {}))))


def _merge_json(conn: sqlite3.Connection, table: str, col: str, key_col: str, key: Any, updates: dict) -> None:
    r = conn.execute(f"SELECT {col} FROM {table} WHERE {key_col}=?", (key,)).fetchone()
    if not r:
        return
    cur = json.loads(r[0] or "{}")
    for k, v in updates.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    conn.execute(f"UPDATE {table} SET {col}=? WHERE {key_col}=?", (_dumps(cur), key))


def r_world_seeded(conn, eid, t, d):
    for h in d.get("houses", []):
        conn.execute("INSERT OR REPLACE INTO houses(id, name, seat_id, liege_id, head_id, words, sigil, data)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (h["id"], h["name"], h.get("seat_id"), h.get("liege_id"), h.get("head_id"),
                      h.get("words"), h.get("sigil"), _dumps(h.get("data", {}))))
    for loc in d.get("locations", []):
        r_location_added(conn, eid, t, loc)
    for rt in d.get("routes", []):
        conn.execute("INSERT OR REPLACE INTO routes(a, b, mode, miles) VALUES (?,?,?,?)",
                     (rt["a"], rt["b"], rt.get("mode", "road"), float(rt["miles"])))
    for c in d.get("characters", []):
        _insert_character(conn, c)
    for rel in d.get("relationships", []):
        conn.execute("INSERT OR REPLACE INTO relationships(a, b, kind, opinion, trust, fear, respect, notes)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (rel["a"], rel["b"], rel.get("kind", "acquaintance"), _clamp(rel.get("opinion", 0)),
                      _clamp(rel.get("trust", 0)), _clamp(rel.get("fear", 0)),
                      _clamp(rel.get("respect", 0)), rel.get("notes", "")))
    for ce in d.get("canon_events", []):
        r_canon_added(conn, eid, t, ce)
    for k in ("player_id", "start_t", "season", "title"):
        if k in d:
            _meta(conn, k, d[k])
    _meta(conn, "now_t", d.get("start_t", t))


def r_location_added(conn, eid, t, loc):
    conn.execute("INSERT OR REPLACE INTO locations(id, name, region, kind, x, y, parent_id, data)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (loc["id"], loc["name"], loc.get("region"), loc.get("kind"), loc.get("x"), loc.get("y"),
                  loc.get("parent_id"), _dumps(loc.get("data", {}))))


def r_fate_recorded(conn, eid, t, d):
    conn.execute("INSERT OR REPLACE INTO fate(character_id, data) VALUES (?, ?)",
                 (d["character_id"], _dumps(d["data"])))


def r_clock_advanced(conn, eid, t, d):
    _meta(conn, "now_t", int(d["to"]))


def r_season_changed(conn, eid, t, d):
    _meta(conn, "season", d["season"])


def r_character_added(conn, eid, t, d):
    _insert_character(conn, d)


def r_character_moved(conn, eid, t, d):
    if d.get("location_id"):
        conn.execute("UPDATE characters SET location_id=? WHERE id=?", (d["location_id"], d["id"]))
    if "sublocation" in d:
        _merge_json(conn, "characters", "status", "id", d["id"], {"sublocation": d["sublocation"] or ""})


def r_character_died(conn, eid, t, d):
    conn.execute("UPDATE characters SET alive=0, death_t=?, next_act_t=NULL WHERE id=?", (t, d["id"]))
    _merge_json(conn, "characters", "status", "id", d["id"], {"cause_of_death": d.get("cause", "unknown")})


def r_attrs_changed(conn, eid, t, d):
    _merge_json(conn, "characters", "attrs", "id", d["id"], d["set"])


def r_status_changed(conn, eid, t, d):
    _merge_json(conn, "characters", "status", "id", d["id"], d["set"])


def r_persona_changed(conn, eid, t, d):
    _merge_json(conn, "characters", "persona", "id", d["id"], d["set"])


def r_tier_changed(conn, eid, t, d):
    sets, args = [], []
    for k in ("tier", "next_act_t", "last_act_t"):
        if k in d:
            sets.append(f"{k}=?")
            args.append(d[k])
    if sets:
        conn.execute(f"UPDATE characters SET {', '.join(sets)} WHERE id=?", (*args, d["id"]))


def r_relationship_changed(conn, eid, t, d):
    a, b = d["a"], d["b"]
    r = conn.execute("SELECT * FROM relationships WHERE a=? AND b=?", (a, b)).fetchone()
    cur = dict(r) if r else {"kind": "acquaintance", "opinion": 0, "trust": 0, "fear": 0, "respect": 0, "notes": ""}
    delta = d.get("delta", {})
    for f in REL_FIELDS:
        cur[f] = _clamp(cur[f] + float(delta.get(f, 0)))
    if d.get("kind"):
        cur["kind"] = d["kind"]
    if d.get("note"):
        cur["notes"] = (cur["notes"] + " | " if cur["notes"] else "") + d["note"]
        cur["notes"] = cur["notes"][-600:]
    conn.execute("INSERT OR REPLACE INTO relationships(a, b, kind, opinion, trust, fear, respect, notes)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (a, b, cur["kind"], cur["opinion"], cur["trust"], cur["fear"], cur["respect"], cur["notes"]))


def r_belief_added(conn, eid, t, d):
    conn.execute("INSERT INTO beliefs(id, holder_id, subject, content, source, event_id, learned_t, confidence, is_true)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (eid, d["holder"], d["subject"], d["content"], d.get("source", "witnessed"),
                  d.get("event_id"), t, float(d.get("confidence", 1.0)), 1 if d.get("is_true", True) else 0))
    conn.execute("INSERT INTO beliefs_fts(rowid, subject, content) VALUES (?,?,?)",
                 (eid, d["subject"], d["content"]))


def r_memory_added(conn, eid, t, d):
    ents = " ".join(d.get("entities", []))
    conn.execute("INSERT INTO memories(id, owner_id, t, kind, text, importance, entities, event_id)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (eid, d["owner"], t, d.get("kind", "observation"), d["text"], float(d.get("importance", 3)),
                  ents, d.get("event_id")))
    conn.execute("INSERT INTO memories_fts(rowid, text, entities) VALUES (?,?,?)", (eid, d["text"], ents))


def r_goal_added(conn, eid, t, d):
    conn.execute("INSERT INTO goals(id, owner_id, level, text, status, parent_id, created_t, canon_ref, by_player)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (eid, d["owner"], d.get("level", "plan"), d["text"], d.get("status", "active"),
                  d.get("parent_id"), t, d.get("canon_ref"), 1 if d.get("by_player") else 0))


def r_goal_updated(conn, eid, t, d):
    if "status" in d:
        conn.execute("UPDATE goals SET status=? WHERE id=?", (d["status"], d["goal_id"]))
    if "text" in d:
        conn.execute("UPDATE goals SET text=? WHERE id=?", (d["text"], d["goal_id"]))


def r_scene_started(conn, eid, t, d):
    conn.execute("INSERT INTO scenes(id, location_id, sublocation, present, started_t, tension, situation)"
                 " VALUES (?,?,?,?,?,?,?)",
                 (eid, d.get("location_id"), d.get("sublocation", ""), _dumps(d.get("present", [])), t,
                  float(d.get("tension", 0.3)), d.get("situation", "")))
    _meta(conn, "scene_id", eid)


def r_scene_updated(conn, eid, t, d):
    if "present" in d:
        conn.execute("UPDATE scenes SET present=? WHERE id=?", (_dumps(d["present"]), d["scene_id"]))
    if "tension" in d:
        conn.execute("UPDATE scenes SET tension=? WHERE id=?", (float(d["tension"]), d["scene_id"]))
    if "situation" in d:
        conn.execute("UPDATE scenes SET situation=? WHERE id=?", (d["situation"], d["scene_id"]))


def r_scene_ended(conn, eid, t, d):
    conn.execute("UPDATE scenes SET ended_t=? WHERE id=?", (t, d["scene_id"]))


def r_delivery_scheduled(conn, eid, t, d):
    conn.execute("INSERT INTO deliveries(id, sent_t, deliver_t, sender_id, recipient_id, channel, payload, intercept_risk)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (eid, t, int(d["deliver_t"]), d.get("sender"), d["recipient"], d.get("channel", "raven"),
                  _dumps(d["payload"]), float(d.get("intercept_risk", 0))))


def r_delivery_done(conn, eid, t, d):
    conn.execute("UPDATE deliveries SET delivered=? WHERE id=?", (int(d.get("state", 1)), d["delivery_id"]))


def r_canon_added(conn, eid, t, ce):
    conn.execute("INSERT OR REPLACE INTO canon_events(id, t_expected, title, summary, participants, location_id,"
                 " preconditions, drivers, chance, status, note, wiki_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ce["id"], ce.get("t_expected"), ce["title"], ce.get("summary", ""),
                  _dumps(ce.get("participants", [])), ce.get("location_id"),
                  _dumps(ce.get("preconditions", [])), _dumps(ce.get("drivers", [])),
                  1 if ce.get("chance") else 0, ce.get("status", "pending"), ce.get("note", ""),
                  ce.get("wiki_ref")))


def r_canon_status(conn, eid, t, d):
    conn.execute("UPDATE canon_events SET status=?, note=? WHERE id=?", (d["status"], d.get("note", ""), d["id"]))


def r_house_changed(conn, eid, t, d):
    for k in ("head_id", "seat_id", "liege_id"):
        if k in d.get("set", {}):
            conn.execute(f"UPDATE houses SET {k}=? WHERE id=?", (d["set"][k], d["id"]))


REDUCERS = {
    "world_seeded": r_world_seeded,
    "location_added": r_location_added,
    "fate_recorded": r_fate_recorded,
    "clock_advanced": r_clock_advanced,
    "season_changed": r_season_changed,
    "character_added": r_character_added,
    "character_moved": r_character_moved,
    "character_died": r_character_died,
    "attrs_changed": r_attrs_changed,
    "status_changed": r_status_changed,
    "persona_changed": r_persona_changed,
    "tier_changed": r_tier_changed,
    "relationship_changed": r_relationship_changed,
    "belief_added": r_belief_added,
    "memory_added": r_memory_added,
    "goal_added": r_goal_added,
    "goal_updated": r_goal_updated,
    "scene_started": r_scene_started,
    "scene_updated": r_scene_updated,
    "scene_ended": r_scene_ended,
    "delivery_scheduled": r_delivery_scheduled,
    "delivery_done": r_delivery_done,
    "canon_added": r_canon_added,
    "canon_status": r_canon_status,
    "house_changed": r_house_changed,
}

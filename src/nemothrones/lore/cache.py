"""lore.db: the shared WOIAF cache (pages, redirects, searches, dossiers, era packs).

Lives in the game home on the sandbox volume and is never committed (WOIAF text is
CC BY-SA; it stays on your machine). Once warm, the lore pipeline works offline.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
  title TEXT PRIMARY KEY, revid INTEGER, length INTEGER, wikitext TEXT,
  categories TEXT NOT NULL DEFAULT '[]', fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS info (title TEXT PRIMARY KEY, length INTEGER, missing INTEGER, fetched_at REAL);
CREATE TABLE IF NOT EXISTS redirects (src TEXT PRIMARY KEY, dst TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS searches (query TEXT PRIMARY KEY, results TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dossiers (title TEXT, year INTEGER, data TEXT NOT NULL, built_at REAL,
  PRIMARY KEY (title, year));
CREATE TABLE IF NOT EXISTS fates (title TEXT, year INTEGER, data TEXT NOT NULL, built_at REAL,
  PRIMARY KEY (title, year));
CREATE TABLE IF NOT EXISTS packs (key TEXT PRIMARY KEY, data TEXT NOT NULL, built_at REAL);
"""


class LoreCache:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def fresh(fetched_at: float | None, max_age_days: float) -> bool:
        return fetched_at is not None and (time.time() - fetched_at) < max_age_days * 86400

    # ------------------------------------------------------------- pages
    def resolve(self, title: str) -> str:
        seen = set()
        while title not in seen:
            seen.add(title)
            r = self.conn.execute("SELECT dst FROM redirects WHERE src=?", (title,)).fetchone()
            if not r:
                break
            title = r[0]
        return title

    def get_page(self, title: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM pages WHERE title=?", (self.resolve(title),)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["categories"] = json.loads(d["categories"])
        return d

    def put_page(self, title: str, revid: int | None, length: int | None, wikitext: str,
                 categories: list[str]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?)",
                          (title, revid, length, wikitext, json.dumps(categories), time.time()))
        self.conn.commit()

    def put_redirect(self, src: str, dst: str) -> None:
        if src != dst:
            self.conn.execute("INSERT OR REPLACE INTO redirects VALUES (?,?)", (src, dst))
            self.conn.commit()

    def get_info(self, title: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM info WHERE title=?", (self.resolve(title),)).fetchone()
        return dict(r) if r else None

    def put_info(self, title: str, length: int | None, missing: bool) -> None:
        self.conn.execute("INSERT OR REPLACE INTO info VALUES (?,?,?,?)",
                          (title, length, 1 if missing else 0, time.time()))

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------ searches
    def get_search(self, query: str, max_age_days: float) -> list[str] | None:
        r = self.conn.execute("SELECT results, fetched_at FROM searches WHERE query=?", (query.lower(),)).fetchone()
        if r and self.fresh(r[1], max_age_days):
            return json.loads(r[0])
        return None

    def put_search(self, query: str, results: list[str]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?)",
                          (query.lower(), json.dumps(results), time.time()))
        self.conn.commit()

    # --------------------------------------------------- dossiers and packs
    def _get(self, table: str, title: str, year: int) -> dict | None:
        r = self.conn.execute(f"SELECT data FROM {table} WHERE title=? AND year=?", (title, year)).fetchone()
        return json.loads(r[0]) if r else None

    def _put(self, table: str, title: str, year: int, data: dict) -> None:
        self.conn.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?)",
                          (title, year, json.dumps(data, ensure_ascii=False), time.time()))
        self.conn.commit()

    def get_dossier(self, title: str, year: int) -> dict | None:
        return self._get("dossiers", title, year)

    def put_dossier(self, title: str, year: int, data: dict) -> None:
        self._put("dossiers", title, year, data)

    def get_fate(self, title: str, year: int) -> dict | None:
        return self._get("fates", title, year)

    def put_fate(self, title: str, year: int, data: dict) -> None:
        self._put("fates", title, year, data)

    def get_pack(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT data FROM packs WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def put_pack(self, key: str, data: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO packs VALUES (?,?,?)",
                          (key, json.dumps(data, ensure_ascii=False), time.time()))
        self.conn.commit()

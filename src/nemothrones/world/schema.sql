-- NemoThrones save schema. One SQLite file per save (WAL mode).
-- Ground truth is the append-only `events` table; the other world tables are
-- materialised by reducers and can be rebuilt by replaying `events`.
-- Log tables (rolls, llm_log, transcript) are not part of the world state.

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  t INTEGER NOT NULL,
  turn INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL,
  location_id TEXT,
  actors TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}',
  visibility TEXT NOT NULL DEFAULT 'public',  -- public|scene|private|secret|system|director
  causes TEXT NOT NULL DEFAULT '[]',
  canon_ref TEXT
);
CREATE INDEX IF NOT EXISTS events_t ON events(t);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS characters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  wiki_title TEXT,
  house_id TEXT,
  born_t INTEGER,
  alive INTEGER NOT NULL DEFAULT 1,
  death_t INTEGER,
  location_id TEXT,
  tier INTEGER NOT NULL DEFAULT 2,
  next_act_t INTEGER,
  last_act_t INTEGER,
  importance REAL NOT NULL DEFAULT 0.5,
  canon INTEGER NOT NULL DEFAULT 1,
  is_player INTEGER NOT NULL DEFAULT 0,
  attrs TEXT NOT NULL DEFAULT '{}',    -- skills, health, wounds
  persona TEXT NOT NULL DEFAULT '{}',  -- titles, traits, voice, secrets, possessions...
  status TEXT NOT NULL DEFAULT '{}'    -- sublocation, activity, cause of death...
);

-- Director-only. Never read by agent, narrator or GM prompt builders (see world/fate.py).
CREATE TABLE IF NOT EXISTS fate (character_id TEXT PRIMARY KEY, data TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS relationships (
  a TEXT NOT NULL, b TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'acquaintance',
  opinion INTEGER NOT NULL DEFAULT 0,
  trust INTEGER NOT NULL DEFAULT 0,
  fear INTEGER NOT NULL DEFAULT 0,
  respect INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS houses (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, seat_id TEXT, liege_id TEXT, head_id TEXT,
  words TEXT, sigil TEXT, data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS locations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT, kind TEXT,
  x REAL, y REAL, parent_id TEXT, data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS routes (
  a TEXT NOT NULL, b TEXT NOT NULL, mode TEXT NOT NULL, miles REAL NOT NULL,
  PRIMARY KEY (a, b, mode)
);

CREATE TABLE IF NOT EXISTS beliefs (
  id INTEGER PRIMARY KEY,
  holder_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'witnessed',
  event_id INTEGER,
  learned_t INTEGER NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  is_true INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS beliefs_holder ON beliefs(holder_id);
CREATE VIRTUAL TABLE IF NOT EXISTS beliefs_fts USING fts5(subject, content);

CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY,
  owner_id TEXT NOT NULL,
  t INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'observation',  -- observation|dialogue|letter|reflection|plan
  text TEXT NOT NULL,
  importance REAL NOT NULL DEFAULT 3,
  entities TEXT NOT NULL DEFAULT '',
  event_id INTEGER
);
CREATE INDEX IF NOT EXISTS memories_owner ON memories(owner_id, t);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, entities);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY,
  owner_id TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'plan',  -- ambition|plan|step
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',  -- active|done|abandoned
  parent_id INTEGER,
  created_t INTEGER NOT NULL,
  canon_ref TEXT,
  by_player INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY,
  sent_t INTEGER NOT NULL,
  deliver_t INTEGER NOT NULL,
  sender_id TEXT,
  recipient_id TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'raven',
  payload TEXT NOT NULL,
  intercept_risk REAL NOT NULL DEFAULT 0,
  delivered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS canon_events (
  id TEXT PRIMARY KEY,
  t_expected INTEGER,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  participants TEXT NOT NULL DEFAULT '[]',
  location_id TEXT,
  preconditions TEXT NOT NULL DEFAULT '[]',
  drivers TEXT NOT NULL DEFAULT '[]',
  chance INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|as-canon|altered|averted|impossible
  note TEXT NOT NULL DEFAULT '',
  wiki_ref TEXT
);

CREATE TABLE IF NOT EXISTS scenes (
  id INTEGER PRIMARY KEY,
  location_id TEXT,
  sublocation TEXT,
  present TEXT NOT NULL DEFAULT '[]',
  started_t INTEGER NOT NULL,
  ended_t INTEGER,
  tension REAL NOT NULL DEFAULT 0.3,
  situation TEXT NOT NULL DEFAULT ''
);

-- Logs (not world state)
CREATE TABLE IF NOT EXISTS rolls (
  id INTEGER PRIMARY KEY, t INTEGER, turn INTEGER, actor_id TEXT, action TEXT,
  kind TEXT, labels TEXT, probs TEXT, derivation TEXT, u REAL, outcome_index INTEGER
);
CREATE TABLE IF NOT EXISTS llm_log (
  id INTEGER PRIMARY KEY, ts REAL, turn INTEGER, role TEXT, backend TEXT, agent_id TEXT,
  prompt_hash TEXT, request TEXT, response TEXT, tokens_in INTEGER, tokens_out INTEGER,
  cost REAL, latency REAL, ok INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS transcript (
  id INTEGER PRIMARY KEY, turn INTEGER, t INTEGER, kind TEXT, text TEXT
);

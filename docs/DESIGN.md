# NemoThrones: Design

*Winter is coming.*

NemoThrones is a deep-lore, character-driven text adventure set in George R. R.
Martin's *A Song of Ice and Fire*. You choose any canon character and a start date,
then live that life through dialogue and decisions until the character dies. There is
no objective. Uncertain outcomes are rolled from odds you can see. Every other important
character is a persistent **nemo agent**. Each keeps living, scheming, writing letters
and meeting others in the background, at a level of detail set by its relevance to you:
Dwarf Fortress depth, but in text and centred on character.

**Canon** is the books, as recorded by **A Wiki of Ice and Fire (WOIAF)**. The game
runs on the author's **nemo** stack: NemoClaw + an OpenShell sandbox (`prometheus`) +
Hermes Agent + DeepSeek v4.1 Flash via OpenRouter.

This document is the design of record. Sections marked *(later)* are designed but
not yet built; see [§12 Roadmap](#12-roadmap).

---

## 1. Principles

1. **The engine owns the facts; LLMs supply the voice.** A deterministic engine owns
   the world state, the clock, the dice, the rules, the flow of information and the
   saves. LLMs supply intention, dialogue, interpretation and prose. They propose; the
   engine validates, computes odds, rolls and commits.
2. **Everyone lives, at an affordable level of detail.** Every agent acts; only the
   cadence and fidelity vary with relevance.
3. **Characters know only what they could know.** Ground truth, each character's
   beliefs and the player character's view are kept separate. Fate (canon after the
   start date) is Director-only.
4. **The player is sovereign over their character.** No system decides what the player
   character thinks, feels, says or does.
5. **Canon is gravity, not rails.** History leans toward the books until your choices
   break its causes.
6. **Every turn is atomic, auditable and replayable.** One transaction per turn, an
   append-only event log, counter-based dice and logged LLM calls.

---

## 2. Architecture

```
 player ─► NemoThrones REPL (prompt_toolkit; slash commands are pure engine, no LLM)
              │ free text / numbered choice
 ┌────────────▼──────────── OpenShell sandbox `prometheus` ───────────────────────┐
 │  ENGINE (python, game venv on /sandbox/.hermes)                                │
 │   turn pipeline · clock · LOD scheduler · odds + dice · Warden · Ravenry      │
 │   event log + world DB (SQLite WAL + FTS5) · lore cache (lore.db)             │
 │      │                         │                              │                │
 │      ▼ GM roles                ▼ nemo agents (100+)           ▼ Maester        │
 │   Hermes API :8642 ◄─ plugin `nemothrones` (read-only world/lore/canon tools)  │
 │      │  (GM brain, tool use, optional delegate_task lore research)             │
 │      └──────────────┬─────────────────────┘                    │               │
 │                inference.local  ─────────────────────────► awoiaf.westeros.org│
 └─────────────────────┼──────────────────────────────────────────────────────────┘
                       ▼ supervisor injects key → OpenRouter → DeepSeek v4.1 Flash
```

### 2.1 Why this split

Hermes's `delegate_task` subagents are ephemeral leaf workers with no conversation
history. That is right for parallel research, wrong for a hundred persistent
characters ticking for years. OpenShell's `inference.local` gives the sandbox one
model, credentials injected by the supervisor, and no key inside the sandbox. So:

- **Hermes is the Game Master's brain.** It interprets player intent and directs
  pacing, with tool use, memory and wiki access through the `nemothrones` plugin.
- **Nemo agents are NemoThrones's own lightweight agent runtime.** Each character is a
  persistent record (persona, memory, beliefs, goals, relationships), turned into a
  prompt only when it acts and sent straight to `inference.local`. That costs a few
  thousand tokens, not Hermes's ~12k system prompt, per call.
- **The engine drives the loop.** No LLM decides whether a turn happens, what the dice
  say, or what is true.

### 2.2 Components

| Component | Module | Kind | Responsibility |
|---|---|---|---|
| REPL | `repl/app.py`, `repl/render.py` | code | Prompt, session commands, rendering, tab-completion |
| Game / turn pipeline | `engine/game.py` | code | One save, one life; the only writer of state |
| Slash commands | `engine/commands.py` | code | Knowledge-filtered world status; `/debug` |
| Clock | `engine/clock.py` | code | Minutes since 0 AC; 12 moons; BC/AC labels; durations |
| Dice | `engine/rng.py` | code | Counter-based `sha256(seed:stream:n)`; persisted counters |
| Odds | `engine/odds.py` | code | Normal performance model, CDF tiers, 1% floor, modifier vocabulary |
| Warden | `engine/warden.py` | code | Validates every LLM output (ids, liveness, presence, bounds, sovereignty) |
| Effects | `engine/effects.py` | code | Applies validated effects as events |
| Scheduler | `engine/scheduler.py` | code | Tiers, cadences, budget-aware stretching |
| Ravenry | `engine/ravenry.py` | code | Witnessing, letters (raven / rider / by hand), delays, interception |
| Mortality | `engine/mortality.py` | code | Age hazard, wounds that heal or kill |
| Store | `world/store.py`, `schema.sql` | code | Event-sourced SQLite; reducers; rebuild; FTS5 |
| Packs | `world/pack.py`, `world/seed.py` | code | Starting worlds (seed packs, era packs) |
| Fate | `world/fate.py` | code | The only reader of the Director-only `fate` table |
| Context views | `agents/context.py`, `agents/memory.py` | code | Assemble prompt context; memory retrieval |
| Nemo agents | `agents/character.py` | LLM | On-stage, background, batch and exchange calls |
| GM Resolve | `agents/roles/gm.py` | LLM (Hermes) | Intent + adjudication parameters |
| Director | `agents/roles/director.py` | LLM (Hermes) | Hooks, arrivals and departures, pacing, canon horizon |
| Narrator | `agents/roles/narrator.py` | LLM | Second-person prose + 3–5 precomputed options |
| Chronicler | `agents/roles/chronicler.py` | LLM | The death Chronicle (*later*: reflections, journal) |
| Maester | `lore/*` | code + LLM | WOIAF client, cache, parse, time-slice, dossiers, cast, era packs |
| LLM client | `llm/*` | code | Role routing, fallback, repair, breaker, budget, log, mock, replay |
| Hermes plugin | `hermes/plugin/nemothrones` | code | `nt_world`, `nt_scene`, `nt_lore`, `nt_canon` (read-only) |

### 2.3 LLM routing

| Role | Default backend | Fallback | When |
|---|---|---|---|
| `gm` | `hermes` | `inference_local` | every free-text turn (numbered choices skip it) |
| `director` | `hermes` | `inference_local` | scene changes, idle scenes, after waits |
| `lore_deep` | `hermes` | `inference_local` | `/lore deep …` |
| `narrator` | `inference_local` | — | every turn |
| `agent` | `inference_local` | — | each on-stage character, each turn |
| `background` / `batch` / `exchange` | `inference_local` | — | as the scheduler finds agents due |
| `chronicler` | `inference_local` | — | on death |
| `dossier` / `fate` / `slicer` | `inference_local` | — | building an era pack |

Backends: `hermes` (`http://127.0.0.1:8642/v1`, bearer token), `inference_local`
(`https://inference.local/v1`; the model field is rewritten by OpenShell),
`openrouter_direct` (host development only), and `mock` / `replay` (in-process).

Every call follows the same chain:

1. Budget check.
2. Backend call.
3. JSON extraction, which tolerates code fences and surrounding prose.
4. Pydantic validation.
5. **One repair retry**: the validation error is sent back to the model.
6. The next backend in the chain.

A circuit breaker skips a backend for 10 minutes after two consecutive failures, so
a down Hermes costs one timeout, not one per turn. Each call is logged with its
prompt hash, tokens, cost, latency and outcome, which feeds `/cost`, `/debug llm` and
deterministic replay. Prompts put stable rules first and volatile context last, to
benefit from provider prefix caching.

---

## 3. The turn

```
input ─┬─ /command ─────────────► engine (no LLM, no time passes except /wait /sleep)
       ├─ number ───────────────► precomputed option (skip GM)
       └─ free text ─► GM Resolve (Hermes)
                          ▼
                       Warden ──► clarify? ooc? wait? (no dice)
                          ▼
                       odds ──► lethal risk ≥ 5%? ── confirm (y/n) ──┐
                          ▼                                          │
            ┌── one SQLite transaction (the autosave) ───────────────┘
            │  speech / action event
            │  roll (seeded, logged) → outcome → effects → witnesses' beliefs
            │  letters / travel
            │  on-stage reactions: Tier-0 agents in parallel (addressed first, ≤3 speak),
            │     plus one NPC→NPC follow-up
            │  advance clock: mortality → letters due → background tiers → meetings
            │  arrivals seeking the player get greeted
            │  Director if the scene changed or idled
            │  death? → final prose + Chronicle → archive read-only
            │  Narrator → prose + next options (odds shown)
            └─ commit
```

Two sequential LLM steps (GM, Narrator) plus one parallel step (on-stage agents) per
turn. Choosing a numbered option skips the GM, because the Narrator already returned
each option fully interpreted.

### 3.1 Example display (odds mode `exact`; also `fuzzy` or `off`)

```
── 10th day of the 2nd moon, 298 AC, afternoon · the godswood, Winterfell · summer ──

You sit beneath the heart tree beside the black pool… Catelyn comes to you through the
trees… Catelyn Stark says to you: "Jon Arryn is dead."
1. Ask Catelyn what the letter said
2. Ride out to meet the royal party on the kingsroad   [Welcomed 62% · Cool 31% · Slight 7%]
3. Challenge the stranger in the yard   [Win 18% · Yield 34% · Maimed 31% · Death 17%] ⚠
> _
```

---

## 4. Game design

### 4.1 Starting a life

`/new` or `nemothrones new` offers two paths:

- **Featured start.** Some use a hand-authored seed pack and play offline
  (`eddard_298`, `catelyn_298`, `jon_298`, `arya_298`, `theon_298`, `robert_298`,
  `tyrion_298`, `cersei_298`). Others are built from WOIAF (`daenerys_298`,
  `rhaenyra_129`, `daemon_129`, `duncan_209`, `jaime_282`).
- **Any character, any year.** Find them by WOIAF search, then choose a year (AC). The
  Maester checks they are born and alive then (from infobox Born/Died) and builds an
  **era pack** (§6):
  - cast selection (top N, default 100)
  - dossiers time-sliced to the start date
  - initial beliefs
  - the canon timeline extracted from the cast's fates
  - an opening scene

  Dossiers are cached per (page, year), so later starts in the same era are fast.

### 4.2 Time

- **Turn length varies with the action.** A spoken line takes minutes, an act takes
  hours, travel takes days at a lord's party pace.
- **Waiting.** `/wait <duration>` and `/sleep` advance the clock in steps (one step
  up to a day; longer waits in 1–7 day steps). Each step can be **interrupted** by a
  letter, an arrival, or someone seeking you out. A wait of six hours or more ends the
  scene: people disperse into the background and a new scene forms around you.
- **Calendar.** A 365-day year in twelve moons, with astronomical years (1 BC = year 0).
- **Seasons** are world state, not calendar: they change when the Citadel's white
  ravens fly. Changes are driven by the canon timeline and the Director *(later)*.

### 4.3 Nemo agents

Each agent is a persistent record, materialised into a prompt only when it acts.

| Part | Contents |
|---|---|
| Identity | name, house, titles, birth, culture, faith, appearance, aliases |
| Persona | traits, values, fears, voice description, history summary (no verbatim book quotes) |
| Attributes | martial, diplomacy, intrigue, stewardship, learning, prowess (0–20); health; wounds |
| Goals | ambitions → plans (latest five kept) → steps |
| Secrets | known only to their holders |
| Relationships | directed edges: kind, opinion, trust, fear, respect (−100…100), notes |
| Memory | episodic stream with importance; FTS5 retrieval; *(later)* reflections |
| Beliefs | subject, content, source, as-of date, confidence, truth flag |
| Fate | Director-only (separate table, separate module) |

**Retrieval** blends three signals: 0.45 × relevance, 0.35 × recency and 0.20 ×
importance.

- **Relevance** is FTS5 BM25 over memory text and tagged entities, trying an AND-match
  first, then OR.
- **Recency** has a 30-day half-life.

No embeddings are needed (`inference.local` serves one chat model).

**On-stage contract:**

```json
{"thought": "...private...", "speech": "..." , "speak_to": "id",
 "action": {"type": "gesture|move|attack|give|leave|write|scheme|other", "target": "id",
            "detail": "...", "risky": false, "to": "..."},
 "plan_update": "...", "opinion_changes": [{"of": "id", "opinion": 5, "trust": -3, "why": "..."}],
 "remember": "..."}
```

`thought` is private. It is logged for continuity and visible only through
`/debug thoughts`.

### 4.4 The living world: tiered level of detail

| Tier | Who | Cadence | Fidelity |
|---|---|---|---|
| 0 On-stage | in the player's scene | every player turn | individual call; reacts to this turn |
| 1 Active | the ~25 most entangled with the player | daily (game time) | individual call covering the elapsed span |
| 2 Background | the rest of the cast | weekly | **batched** regional calls (≤ 8 per call) |
| 3 Dormant | extras, minor encountered | on demand | rule-based; promoted on encounter |

**Entanglement** sums importance, mutual |opinion|, a non-trivial relationship kind,
and co-location with the player. Tiers are recomputed at every scene change. Next-act
times are jittered so agents do not act in lockstep.

**Background outputs** may move the agent, write letters, update plans, or *seek*
someone.

- If they seek **the player** and are co-located, they come to the player's scene,
  which interrupts a wait.
- If they seek **another off-stage agent nearby**, one *exchange* call simulates the
  meeting from both dossiers. The result goes into both memories and opinions.

**Budget pressure** (spend past the warning line) stretches Tier-1/2 cadence up to 3×.
Past 75% pressure, background roles are skipped and agents keep to their routine. The
player's scene is never degraded.

**Light-cone precompute** *(later, M6)*. Information travels at finite speed, so agents
outside the player's causal cone can be simulated ahead while the player reads. If new
information reaches them sooner than predicted, the engine rolls back: optimistic
parallel discrete-event simulation (Time Warp). Events already carry the time,
location, visibility and cause fields this needs.

### 4.5 Knowledge and fog of war: the Ravenry

Ground truth is the event log. Each event has a time, a location, actors, a
visibility (`public | scene | private | secret | system | director`) and causes.

- **Witnesses** learn at once: beliefs for everyone present at rolls, attacks and
  deaths. Dialogue becomes memories.
- **Letters:**
  - Delivery: by raven (both seats have maesters, about 250 mi/day), by rider (about
    40 mi/day) or by hand (same place).
  - Delay: computed from rough map distance.
  - Interception risk: 2%.
  - Arrival: the recipient gains a memory and a belief; the player gets an interrupt.
- *(later, M3)* **Rumour** diffusion along the location graph, losing fidelity as it
  spreads; proclamations; deliberate lies and false beliefs.

**Slash commands show the player character's beliefs, never the truth.** Each belief
is stamped "as of <date>, via <source>". `/debug` is the explicit out-of-character
exception.

### 4.6 Odds and resolution

The GM (or the Narrator, for options) sets the **stakes**:

- the skill
- a difficulty (4 trivial · 7 easy · 10 moderate · 13 hard · 16 very hard · 19 heroic ·
  22 near-impossible), or an opponent and their skill
- modifiers named from a **bounded vocabulary**, whose values the engine owns; custom
  modifiers are capped at ±3 each and ±5 in total
- 2–5 outcome tiers, best first, each with consequences

The engine sets the **probabilities**:

- performance `X ~ N(μ, σ = 4)`, with `μ = skill + Σmodifiers − difficulty`
- tier cut points evenly spaced (4) and symmetric about 0
- tier probabilities are CDF differences, so they are always normalised and monotone
  in skill
- every tier is floored at **1%**: nothing in Westeros is certain
- wounds add engine-owned penalties: −3 serious, −6 grievous

Further rules:

- **Opposed checks** use the opponent's skill as the difficulty.
- **Chance outcomes** take weights; canon gravity may bias off-stage chance events
  *(later)*.
- **Lethal risk** of 5% or more asks for confirmation, because death is permanent.
- **Social rolls** set *impact*, not outcomes. A roll decides how hard your words land
  (an `impact` from −3 to +3 delivered to the target's agent). **The agent still
  chooses its reply in character.** Dice and free will combine: you roll how compelling
  your plea to Ned was, and Ned decides what to do with it.
- **NPC violence** on-stage resolves through an opposed prowess table (Kills · Grievous
  wound · Wounds · Glancing · Repelled). Others can kill you without your consent;
  this is Westeros.
- `/why` shows the full derivation of the last roll.

### 4.7 Dialogue

Dialogue is the main mode of play.

- **Input forms:**
  - quoted text, or `say …`, is speech
  - plain text is an action
  - mixed input works ("I bow and say 'My lord'")
  - a number picks an option
- **Rolls:** casual talk is never rolled. Persuasion, deception, intimidation and
  seduction are flagged by the GM and rolled.
- **Speaking order:** addressed agents always respond. Up to three people speak in a
  turn, picked by importance, and one NPC→NPC follow-up is allowed.
- **Verbatim speech:** every spoken line reaches the player word for word. The Warden
  appends any line the Narrator dropped.

### 4.8 Game managers

These keep the game on rails and moving forward.

- **GM Resolve** (Hermes): interprets intent and sets the stakes, using the plugin
  tools for facts.
- **Director** (Hermes): runs on scene changes, after waits, or when a scene has idled
  for three turns.
  - Draws hooks from the living world: nearby people's plans, letters on the way,
    weather. It never invents realm-changing events by fiat.
  - Brings arrivals in and moves departures out, and tracks tension.
  - Sees the **canon horizon**: pending canon events in the next 120 days, Director-only.
- **Warden**: deterministic validation of everything (see §7). *(later)* LLM checks
  for anachronism, meta-knowledge and book-vs-show contamination.
- **Chronicler**: writes the death Chronicle. *(later)* reflections and `/journal`
  synthesis.
- **Maester**: lore (§6).

### 4.9 Canon gravity: Butterfly (default 0.6; `/gravity 0..1`)

Canon events come from the fate extraction (§6). Each has an expected date,
participants, a location, structured **preconditions** (`alive(X)`, `at(X, Y)`, …) and
**drivers** (whose goals produce it).

Already built:

- canon events are stored per save
- the Director sees the horizon
- the Chronicle reports a canon ledger

*(later, M4)* At each background tick, for pending events in the look-ahead window:

- **preconditions hold**: seed the event as a *plan option* in its drivers' goals;
  lean the odds of off-stage rolls the player has not touched toward canon, within a
  band scaled by gravity; schedule chance events (a hunting accident) with
  gravity-weighted probability
- **preconditions broken**: mark the event `averted` or `impossible`

Statuses: `pending | as-canon | altered | averted | impossible`. The engine never
forces the player character and never overrides an on-stage roll.

| Gravity | Feel |
|---|---|
| 0.0 Sandbox | canon is only the starting world and personalities |
| 0.6 Butterfly | history leans to canon until its causes break |
| 1.0 Fated | canon happens on schedule unless directly prevented |

### 4.10 Death and the end

Death can come from:

- risky outcomes you chose
- other agents (attacks, and *later* executions, assassinations and battles)
- wounds that will not heal (daily hazard: 0.3% serious, 6% grievous)
- age: a Gompertz-like annual hazard, about 0.5%/yr at 30 and 15%/yr at 70

On the player character's death:

1. Final narration.
2. **The Chronicle of <Name>**: who they were, what they did, how they died, what the
   realm became, and the canon ledger.
3. The save is checkpointed and set **read-only** (ironman). Loading it later shows the
   Chronicle.

### 4.11 Slash commands

| Group | Commands |
|---|---|
| Self | `/me` · `/goals` · `/goal add <text>` · `/goal done <n>` · `/inventory` · `/journal` · `/relations` · `/family` |
| Scene | `/look` · `/who` · `/why` |
| World, as believed | `/know <x>` · `/house [x]` · `/realm` · `/letters` · *(later)* `/map` `/wars` `/rumors` `/travel` |
| Time | `/date` · `/wait <dur>` · `/sleep` |
| Out of character | `/lore <topic>` · `/lore deep <q>` · `/history` · `/cost` · `/settings` · `/odds` · `/verbosity` · `/gravity` · `/help` |
| Session | `/new` · `/load [name]` · `/saves` · `/save` (snapshot) · `/quit` |
| Dev | `/debug truth <x> \| agents \| canon \| llm \| thoughts \| rebuild` |

---

## 5. Data model

There is one SQLite file per save (WAL), plus a shared `lore.db`. See
`src/nemothrones/world/schema.sql`.

| Table | Purpose |
|---|---|
| `meta` | seed, start/now, player, scene, season, settings, UI state, dice counters, spend |
| `events` | **append-only ground truth**: t, turn, kind, location, actors, summary, data, visibility, causes, canon_ref |
| `characters` | identity, liveness, location, tier, next/last act, importance, attrs, persona, status |
| `fate` | Director-only |
| `relationships` | directed (a → b) |
| `houses`, `locations`, `routes` | the realm |
| `beliefs` (+FTS5) | who believes what, since when, from whom |
| `memories` (+FTS5) | episodic memory |
| `goals` | ambitions / plans / steps |
| `deliveries` | letters in flight and delivered |
| `canon_events` | canon timeline and its status |
| `scenes` | the player's scenes |
| `rolls`, `llm_log`, `transcript` | logs (not world state) |

**Event sourcing.** Every world change is an event plus a reducer, applied in the same
transaction. Rows created by an event use the event id as their primary key, so a
replay is bit-identical. `Store.rebuild()` replays the log into a fresh database;
`/debug rebuild` and the tests assert that the replay equals the live world.

---

## 6. The lore pipeline (WOIAF = canon)

| Stage | Module | What it does |
|---|---|---|
| Client | `lore/wiki.py` | MediaWiki API: batched `query` (≤20 titles per request), `list=search`, redirects; ≤1 request/s on one connection; descriptive User-Agent; `maxlag`; backoff |
| Cache | `lore/cache.py` | `lore.db`: pages (title, revid, length, wikitext, fetched_at), redirects, searches, dossiers, era packs; 30-day freshness; works offline once warm |
| Parse | `lore/parse.py` | `mwparserfromhell`: tolerant infobox extraction (any `{{Character…}}`-like template; case-insensitive keys), refs stripped, sections, links |
| Dates | `lore/dates.py` | "In 263 AC", "In or between 282 AC and 283 AC", "c. 250 AC", "Before 280 AC", "2 BC" → year ranges |
| Time-slicer | `lore/slicer.py` | Splits the life into **past** (< D) and **fate** (≥ D); see below |
| Dossier | `lore/dossier.py` | LLM sees **past only** → agent record; infobox facts (birth, name) override the model |
| Fate | `lore/dossier.py` | Separate LLM call: fate text → canon events with preconditions and drivers |
| Cast | `lore/cast.py` | Two-phase top-N selection (below) |
| Era pack | `lore/erapack.py` | Assembles a `WorldPack`: characters, houses, locations, relationships, canon events, opening |
| Service | `lore/service.py` | `/lore`, search, promotion on encounter, `build_pack` |

**How the time-slicer works:**

- Book "Recent Events" sections are dated by `data/books.yaml` (AGOT 298–299, ACOK
  299, ASOS 299–300, AFFC/ADWD 300, TWOW 300+, Dunk & Egg 209–212, the Dance 129–131…).
- History paragraphs are classified by the years they mention: all earlier is past,
  any later is fate, and only the start year itself is ambiguous (it may already be
  true on the year's first day). Undated paragraphs inherit their predecessor's class
  in chronological sections, and are ambiguous in descriptive ones (lead, Appearance
  and Character).
- Ambiguous passages, which mention years on both sides of D, go to an LLM classifier.
  If it is unavailable they are treated as **fate**, so spoilers are never risked.
- Quotes, references, behind-the-scenes and media sections are dropped: no verbatim
  book text is ever stored.
- **Infobox lifetime lists never reach a dossier or `/lore`.** WOIAF's titles,
  allegiances, spouses, issue, siblings and aliases cover the whole life (Jon Snow's
  include "Lord Commander" and "Night's Watch"). Only birth, parents, culture, faith
  and the first-listed house are passed on; everything else comes from the sliced past.
- **In a year of succession, the old monarch reigns at its start.** In 298 AC the
  common knowledge says Robert sits the throne, not Joffrey.

**How cast selection works:**

- **Candidates:** links from the player's page; the great houses' pages; year pages
  D−1…D+2; and curated era anchors (`data/era_anchors.yaml`).
- **Phase A:** metadata only (page length) for every candidate. Keep the top 3N plus the
  player's kin.
- **Phase B:** content for those. Keep pages with a character infobox who are alive at
  D, then score them:
  - `w1·notability`: log page length
  - `+ w2·rank`: crown, Hand, Lord Paramount, house head, …
  - `+ w3·proximity`: kin, links, shared allegiance
  - `+ w4·canon involvement in [D, D+2]`
- No canon character is ever invented. Sparse eras get smaller casts. Unnamed extras
  (guards, smallfolk) are Tier 3, made on encounter.

**Promotion on encounter.** Writing to a canon character outside the cast builds their
dossier on demand (sliced to the current in-game year, with their fate recorded for
the Director) and adds them as a Tier-1 agent at their WOIAF location. Promotion on
meeting someone in person, for example on arrival somewhere, comes with travel in M3.

**Spoiler rules.** `/lore` shows only the past relative to the in-game date (marked
"may be imperfect"). Book canon overrides the shows in every prompt. WOIAF text is
CC BY-SA: it is attributed in `/lore` and the README, and the cache lives only on the
sandbox volume, never in git.

---

## 7. The Warden: what is enforced

| Check | Where |
|---|---|
| Targets exist, are alive, and are present (except letters) | `check_resolve` |
| Opposed checks need a present opponent, else they fall back to a difficulty check | `check_resolve` |
| Skill in the vocabulary; difficulty 0–30; 2–5 tiers | `check_resolve` |
| A player-death effect forces `lethal` (so the confirmation fires) | `check_resolve` |
| Nobody sets the player character's opinions, goals or feelings | `check_effects` |
| Relationship deltas ±30, impact ±3, opinion changes ±20 | effects / agent turns |
| Wounds and deaths only on the actor or people present | `check_effects` |
| Agents can only attack someone present; speech ≤ 700 chars | `check_agent_turn` |
| Background moves only to known locations; letters only to the living | `check_background` |
| Director arrivals only from people nearby; departures only of people present | `check_beat` |
| Every spoken line reaches the player verbatim | `narrator.ensure_lines` |
| Fate never reaches agent, narrator or GM prompts | module boundary + `test_fate_isolation.py` |

---

## 8. Running on the nemo stack

| Concern | How |
|---|---|
| Persistence | Everything lives under `/sandbox/.hermes/workspace/nemothrones/` (saves, `lore.db`, config, venv). It survives `rebuild`; `hermes --backup` copies it |
| Egress | `policy/nemothrones.yaml` (preset name = filename) allows GET on `awoiaf.westeros.org` and PyPI, bound by `binaries:` to the game venv and Hermes's python. Apply with `nemohermes prometheus policy-add --from-file policy/nemothrones.yaml --yes` |
| Inference | `https://inference.local/v1`: no key in the sandbox; the supervisor injects it and rewrites the model. TLS uses the system store (`SSL_CERT_FILE` honoured) plus an optional `ca_bundle` |
| Hermes GM | `http://127.0.0.1:8642/v1`; token from `NEMOTHRONES_HERMES_TOKEN`, then `<home>/hermes_api_token` (written by `start_nemothrones.sh` from `nemohermes … gateway-token`), then `/sandbox/.hermes/.env` |
| Install | `scripts/deploy.sh`: builds a wheel, then an offline wheelhouse for the sandbox's Python, then `docker cp` + `chown`, venv, `pip --no-index`; installs the plugin and skills, enables the plugin, appends to AGENTS.md, applies the policy |
| Launch | `scripts/start_nemothrones.sh`: starts the stack, hands over the token, opens `nemohermes prometheus connect` (or `NEMOTHRONES_ENTRY=docker`) |
| Health | `nemothrones doctor`: runtime, storage persistence, FTS5, plugin, keys, budget, `inference.local` ping, Hermes ping, WOIAF reachability and infobox probe |

### 8.1 Unverified integration points

These could not be exercised from the build environment. `doctor` checks each one:

- **The Hermes plugin `register(ctx)` signature.** It follows the documented
  `ctx.register_tool(name, toolset, schema, handler)` and falls back to positional
  arguments.
- **Whether `127.0.0.1:8642` is reachable from inside the sandbox**, and whether the
  `gateway-token` is the API bearer there.
- **The exact TTY entry** (`nemohermes … connect` vs `exec`).
- **The WOIAF API path (`/api.php`) and infobox template/field names.** The parser is
  tolerant, and `nemothrones lore probe` prints exactly what it found.

---

## 9. Cost and latency (DeepSeek v4.1 Flash: $0.14/M in, $0.42/M out)

| Call | Backend | Frequency | ≈ $ |
|---|---|---|---|
| GM Resolve (incl. ~12k Hermes prompt) | hermes | per free-text turn | 0.003 |
| Narrator | inference.local | per turn | 0.001 |
| On-stage agent | inference.local | 1–3 per turn | 0.0008 each |
| Tier-1 agent | inference.local | daily × ~25 | 0.0007 each |
| Tier-2 batch | inference.local | weekly × ~10 | 0.0025 each |
| Director | hermes | scene changes / idle | 0.0025 |
| Dossier | inference.local | once per character per era (cached) | 0.003 |

- **Per turn:** about $0.006–0.015. **A 100-turn session:** about $1–2.
- **New era:** about $0.30–0.50, once.
- **Caps:** sessions warn at $2 and hard-stop at $5 (configurable). Keep a credit limit
  on the OpenRouter key too.
- **Latency target:** first prose in about 10 s or less.
- **Full fidelity** (every agent every turn) would be about $0.08–0.10 and 15–40 s per
  turn, which is why tiers exist.

---

## 10. Verification

- **Unit tests:**
  - calendar round-trips, BC labels and durations
  - odds: normalised, floored, monotone, Φ-exact for 2 tiers, modifier caps
  - dice determinism and resumption
  - JSON extraction
  - budget caps
  - LLM repair, fallback and breaker
  - Warden rules
  - effects; tiers and cadence
  - **event-log rebuild equals the live world**
  - **fate isolation** (static and dynamic)
  - lore dates, parse, slicer and cast
- **Integration tests** (real CLI, mock LLM):
  - a scripted life of Eddard Stark: commands, a move, an opposed persuasion with
    `/why`, a letter, an interrupted wait, a lethal confirmation, death, the Chronicle,
    read-only archive
  - the same seed gives the same story
  - save/resume
  - **replay from `llm_log` reproduces the session**
  - every featured offline start opens
- **Live, on the host:** `deploy.sh` → `doctor` (all green) → play ~10 turns of
  `eddard_298` → a WOIAF start (`--character "Daenerys Targaryen" --year 298`) →
  `/debug llm` to see which backend served the GM.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Time-slicing errors (a one-handed Jaime in 298 AC) | Book-dated sections, year tagging, conservative fallback (fate), infobox overrides, `nemothrones lore dossier` to inspect; lore QA eval *(M6)* |
| The model leaks future events or HBO canon | Agents see only past + beliefs; "book canon; your memories only" rules; fate isolation; Warden LLM checks *(M4)* |
| Hermes slow, JSON drift, loopback blocked | Schema + repair, breaker, automatic fallback to inference.local, doctor |
| Cost runaway | Budget governor, LOD stretching, caps, an OpenRouter key limit |
| WOIAF etiquette, availability or template drift | Batching, 1 request/s, cache, era packs offline, tolerant parser, probe |
| Volume loss on an unclean reboot | Data only under `/sandbox/.hermes`; `hermes --backup` |
| Mature content or provider refusals | Prompts default to grounded violence, with sex off-page; refusal → bare-facts fallback narration |
| IP | Personal fan project; no book text stored; WOIAF attributed (CC BY-SA) |

---

## 12. Roadmap

| Milestone | Scope | State |
|---|---|---|
| **M0 Foundations** | config, LLM layer (5 backends, repair, fallback, breaker, budget, log, replay), event-sourced store, clock, dice, odds, policy, deploy/start scripts, doctor, this design | **built** |
| **M1 Vertical slice** | Winterfell 298 AC seed pack (17 agents), REPL + commands, full turn pipeline, odds + confirmation, options, Tier 0/1/2 + batching + exchanges, witnesses, letters, memory, waits, Director hooks, Warden, mortality, death → Chronicle → read-only, save/load, Hermes plugin + skills | **built** |
| **M2 WOIAF lore pipeline** | client + cache, parser, dates, time-slicer, dossiers, fate → canon events, cast top-N, era packs, any character / any year, `/lore`, promotion on encounter (by letter), `woiaf-maester` skill | **built**; tested against synthetic WOIAF-format pages only. The live wiki is unverified (§8.1) |
| M3 Living world | rumour diffusion, interception politics, gazetteer + routes + `/travel` with interruptions, `/map` `/wars` `/rumors`, travelling companions, promotion heuristics | later |
| M4 Managers in full | Butterfly gravity mechanics (plan seeding, odds bias, chance scheduling, precondition tracking), Warden LLM checks, Chronicler reflections, `/journal` synthesis | later |
| M5 Depth | armies, wars and battles; holdings and economy (light); disease; seasons, winter and the Others; era-gated magic and dragons; succession and inheritance | later |
| M6 Polish | light-cone precompute, streaming narration, eval suites (lore QA, anachronism probes), balance, Hermes `delegate_task` deep research, optional local-4090 route for background tiers | later |

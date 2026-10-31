# NemoThrones

*A life in the world of A Song of Ice and Fire. Winter is coming.*

Choose any canon character and a start date, then live that life through dialogue and
decisions until the character dies. There is no objective. Uncertain outcomes are rolled
from odds you can see. Every other important character is a persistent **nemo agent**
that keeps living, scheming, writing letters and meeting others in the background,
whether or not you have met them.

- **Canon** is the books, as recorded by [A Wiki of Ice and Fire](https://awoiaf.westeros.org).
- **Interface** is a plain text prompt with slash commands.
- **Runtime** is the [nemo](https://github.com/JackWetherell/nemo) stack: NemoClaw +
  OpenShell + Hermes Agent + DeepSeek v4.1 Flash on OpenRouter. Hermes is the Game
  Master's brain; the characters are lightweight agents on `inference.local`.

The full design is in **[docs/DESIGN.md](docs/DESIGN.md)**.

```
── 10th day of the 2nd moon, 298 AC, afternoon · the godswood, Winterfell · summer ──

You sit on a stone beneath the great heart tree beside the black pool, cleaning Ice
with oilcloth… Catelyn comes to you through the trees; her face tells you she brings
tidings.
1. Ask Catelyn what has happened
2. Rise and take her hands
3. Send for Maester Luwin before she can speak    [Swift 71% · Slow 28% · …]
> "Cat. You've had a raven."
```

---

## Quick start

### 1. Try it offline (no keys, no network)

The mock LLM plays every role deterministically. It is good for seeing the loop, not
for prose.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/nemothrones new --start eddard_298 --llm mock
```

### 2. Play it for real on the nemo stack

From the host, in this repository:

```bash
./scripts/deploy.sh                 # build, copy into the sandbox, venv, plugin, skills, policy
hermes --stop && hermes --no-launch # restart Hermes so it loads the nemothrones plugin
./scripts/start_nemothrones.sh      # starts the stack, hands over the Hermes token, opens a sandbox shell
```

Then, inside the sandbox:

```bash
/sandbox/.hermes/workspace/nemothrones/.venv/bin/nemothrones doctor   # everything green?
/sandbox/.hermes/workspace/nemothrones/.venv/bin/nemothrones          # play
```

- **Persistence.** Everything lives under `/sandbox/.hermes/workspace/nemothrones/`, so
  it survives a sandbox rebuild and `hermes --backup` copies it. As the nemo README
  says: **`hermes --backup && hermes --stop` before every reboot.**
- **The egress preset.** `policy/nemothrones.yaml` allows read-only access to the wiki
  for the game venv and Hermes's python. `deploy.sh` applies it, or apply it by hand:
  `nemohermes prometheus policy-add --from-file policy/nemothrones.yaml --yes`.

### 3. Develop on the host against OpenRouter

```bash
export OPENROUTER_API_KEY=...       # host only; never put keys in the sandbox
.venv/bin/nemothrones new --start eddard_298 --llm openrouter_direct --home ~/.nemothrones-dev
```

---

## Starting a life

```bash
nemothrones new                                   # interactive: featured lives, or any name
nemothrones new --start jon_298                   # a featured start (offline seed pack)
nemothrones new --character "Daenerys Targaryen" --year 298   # anything on WOIAF
nemothrones play                                  # continue the latest save
nemothrones saves                                 # list saves
```

- **Featured offline starts** (from the Winterfell 298 AC seed pack): `eddard_298`,
  `catelyn_298`, `jon_298`, `arya_298`, `theon_298`, `robert_298`, `tyrion_298`,
  `cersei_298`.
- **Featured WOIAF starts:** `daenerys_298`, `rhaenyra_129`, `daemon_129`,
  `duncan_209`, `jaime_282`.

Any other character and year builds an **era pack** from the wiki:

1. the 100 most relevant characters alive then, ranked by notability, rank, proximity
   to you and involvement in upcoming canon
2. a dossier for each, sliced to the date so no one knows their future
3. the canon timeline ahead, kept hidden by the Director

The first start in an era takes a few minutes; dossiers are cached for later starts.

Inspect what the Maester sees, without starting a game:

```bash
nemothrones lore probe "Eddard Stark"          # what the parser found on the live wiki
nemothrones lore cast "Eddard Stark" --year 298 # who would be in the cast, and why
nemothrones lore dossier "Jon Snow" --year 298  # the time-sliced dossier (and the fate, marked as spoilers)
nemothrones lore page "Robb Stark" --year 298   # what /lore would show
```

## Playing

- **Speak** with quotes: `"My lord, a word."`. **Act** in plain words:
  `I ride out to meet the king`. **Pick an option** by typing its number.
- **Odds are shown** before you commit: `[Convinced 62% · Wavering 30% · Unmoved 8%]`.
  Anything with ≥5% risk of death asks for confirmation. `/why` explains the last roll.
- **Social rolls** decide how hard your words land. The other character's agent still
  decides how to answer.
- **Time** passes with every act. `/wait 3 days` and `/sleep` skip ahead, but letters,
  arrivals and people seeking you out interrupt.
- **Death** is permanent. Your save becomes a read-only **Chronicle** of your life and
  the realm you left behind.

| | Commands |
|---|---|
| You | `/me` `/goals` `/goal add <aim>` `/inventory` `/journal` `/relations` `/family` |
| The scene | `/look` `/who` `/why` |
| The world, as you know it | `/know <anyone or anything>` `/house [name]` `/realm` `/letters` |
| Time | `/date` `/wait <duration>` `/sleep` |
| Out of character | `/lore <topic>` `/lore deep <question>` `/history` `/cost` `/settings` `/odds exact\|fuzzy\|off` `/verbosity terse\|standard\|lush` `/gravity 0..1` `/help` |
| Session | `/new` `/load [name]` `/saves` `/save` `/quit` |
| Developer (spoilers) | `/debug truth <x>` `/debug agents` `/debug canon` `/debug llm` `/debug thoughts` `/debug rebuild` |

Everything in the world group is filtered through what **your character** believes,
with "as of" dates and sources. Only `/debug` shows the truth.

## How it works

- **The engine owns the facts; LLMs supply the voice.**
  - A deterministic engine owns the world, the clock, the dice and the information.
  - Hermes (as Game Master and Director), the Narrator and the nemo agents propose.
  - The Warden validates every proposal; the engine computes odds, rolls and commits.
- **Every turn is one SQLite transaction** over an append-only event log. Every turn is
  an autosave, replayable bit-for-bit (`--llm replay:<save.db>`).
- **Tiered level of detail.**
  - Characters in your scene act every turn.
  - The ~25 most entangled with you act daily.
  - The rest act weekly, in batched calls.
  - About $0.01 per turn on DeepSeek v4.1 Flash; session caps are in
    `nemothrones.yaml`.
- **Canon gravity** defaults to *Butterfly* (0.6): history leans toward the books until
  your choices break the causes.

## Configuration

Copy `nemothrones.example.yaml` to `<home>/nemothrones.yaml`. `nemothrones config` prints
the effective settings.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests
.venv/bin/pytest
```

## Licence and attribution

The code is MIT licensed. *A Song of Ice and Fire* is © George R. R. Martin; this is a
personal, non-commercial fan project. Lore is drawn at runtime from
[A Wiki of Ice and Fire](https://awoiaf.westeros.org), whose text is licensed
[CC BY-SA](https://awoiaf.westeros.org/index.php/A_Wiki_of_Ice_and_Fire:Copyrights).
Wiki text is cached only on your machine and is never committed. No book text is
stored.

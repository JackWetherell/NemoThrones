"""The NemoThrones REPL: a plain text prompt with slash commands.

Session-level commands (/new, /load, /saves, /save, /quit) live here; everything
else goes to the Game. Works interactively (prompt_toolkit, with tab-completion of
commands and names) or from a script file (for tests and recorded sessions).
"""

from __future__ import annotations

import random
import shutil
import sys
import time
from pathlib import Path

import yaml

from ..config import Settings
from ..engine.commands import COMMANDS
from ..engine.game import Block, Game
from ..llm import LLM
from ..world.pack import builtin_pack_path, load_pack
from .render import render

SESSION_COMMANDS = ["new", "load", "saves", "save", "quit"]
BANNER = """
  N E M O T H R O N E S
  a life in the world of A Song of Ice and Fire
  canon: A Wiki of Ice and Fire · type /help · Winter is coming.
"""


# ----------------------------------------------------------------------- IO
class ScriptIO:
    """Reads input lines from a file; echoes them so transcripts read naturally."""

    def __init__(self, path: str | Path, out=None):
        self.lines = [ln.rstrip("\n") for ln in Path(path).read_text().splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")]
        self.out = out or sys.stdout

    async def read(self, prompt: str = "> ") -> str | None:
        if not self.lines:
            return None
        line = self.lines.pop(0)
        self.write(f"{prompt}{line}")
        return line

    def write(self, text: str) -> None:
        print(text, file=self.out, flush=True)


class TerminalIO:
    def __init__(self, names: list[str] | None = None):
        self.session = None
        self.names = names or []
        if sys.stdin.isatty():
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.completion import WordCompleter
                from prompt_toolkit.history import InMemoryHistory

                words = [f"/{c}" for c in list(COMMANDS) + SESSION_COMMANDS] + self.names
                self.session = PromptSession(history=InMemoryHistory(),
                                             completer=WordCompleter(words, ignore_case=True, sentence=True))
            except Exception:  # pragma: no cover - terminal quirks
                self.session = None

    async def read(self, prompt: str = "> ") -> str | None:
        try:
            if self.session is not None:
                return await self.session.prompt_async(prompt)
            return input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None

    def write(self, text: str) -> None:
        print(text, flush=True)


# ------------------------------------------------------------------ helpers
def featured_starts() -> list[dict]:
    from importlib import resources

    return yaml.safe_load(resources.files("nemothrones.data").joinpath("featured_starts.yaml").read_text())


def unique_save(settings: Settings, stem: str) -> Path:
    settings.ensure_dirs()
    base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem.lower())
    for i in range(1, 1000):
        p = settings.saves_dir / f"{base}-{i}.db"
        if not p.exists():
            return p
    raise RuntimeError("too many saves")


def list_saves(settings: Settings) -> list[Path]:
    if not settings.saves_dir.is_dir():
        return []
    return sorted(settings.saves_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


def make_llm(settings: Settings, llm_arg: str | None) -> LLM:
    if not llm_arg or llm_arg == "default":
        return LLM(settings)
    if llm_arg == "mock":
        settings.force_backend("mock")
        return LLM(settings)
    if llm_arg.startswith("replay:"):
        from ..llm.backends import ReplayBackend
        from ..world.store import Store

        src = Store(llm_arg.split(":", 1)[1], readonly=True)
        rows = [dict(r) for r in src.conn.execute("SELECT role, prompt_hash, response, ok FROM llm_log ORDER BY id")]
        src.close()
        settings.force_backend("replay")
        return LLM(settings, backends={"replay": ReplayBackend(rows)})
    settings.force_backend(llm_arg)
    return LLM(settings)


def make_lore(settings: Settings, llm: LLM):
    try:
        from ..lore.service import LoreService
    except ImportError:  # pragma: no cover
        return None
    return LoreService(settings, llm)


# ------------------------------------------------------------------ session
class Session:
    def __init__(self, settings: Settings, io, *, llm_arg: str | None = None, seed: int | None = None):
        self.settings = settings
        self.io = io
        self.llm_arg = llm_arg
        self.seed = seed
        self.game: Game | None = None
        self._bannered = False

    def banner(self) -> None:
        if not self._bannered:
            self._bannered = True
            self.io.write(BANNER)

    def show(self, blocks: list[Block]) -> None:
        if blocks:
            self.io.write(render(blocks, color=self.settings.game.color and isinstance(self.io, TerminalIO),
                                 w=self.settings.game.wrap))

    def say(self, kind: str, text: str) -> None:
        self.show([Block(kind, text)])

    def _attach(self, game: Game) -> None:
        self.game = game
        game.lore = make_lore(self.settings, game.llm)
        if game.path and not game.readonly:
            self.settings.home.mkdir(parents=True, exist_ok=True)
            self.settings.active_pointer.write_text(str(game.path))

    async def close_game(self) -> None:
        if self.game is not None:
            await self.game.close()
            self.game = None

    # ------------------------------------------------------------ starting
    async def start_featured(self, fid: str, save: Path | None = None) -> bool:
        self.banner()
        entry = next((f for f in featured_starts() if f["id"] == fid), None)
        if entry is None:
            self.say("error", f"No featured start called {fid!r}.")
            return False
        if entry.get("pack"):
            pack = load_pack(builtin_pack_path(entry["pack"]))
            return await self._create(pack, entry["player"], save)
        return await self.start_custom(entry["character"], entry["year"], None, save)

    async def start_custom(self, character: str, year: int, moon: int | None, save: Path | None = None) -> bool:
        self.banner()
        llm = make_llm(self.settings, self.llm_arg)
        lore = make_lore(self.settings, llm)
        if lore is None:
            self.say("error", "The WOIAF lore pipeline is unavailable.")
            return False
        self.say("system", f"Consulting A Wiki of Ice and Fire for {character} in {year} AC… "
                           "(the first build of an era takes a few minutes; later starts reuse the cache)")
        try:
            pack, player_id = await lore.build_pack(character, year, moon, progress=lambda m: self.say("system", m))
        except Exception as e:
            await lore.aclose()
            await llm.aclose()
            if self.settings.dev:
                raise
            self.say("error", f"Could not build that world: {e}")
            return False
        await lore.aclose()
        return await self._create(pack, player_id, save, llm=llm)

    async def _create(self, pack, player_id: str, save: Path | None, llm: LLM | None = None) -> bool:
        await self.close_game()
        seed = self.seed if self.seed is not None else random.SystemRandom().randrange(1, 2**31)
        path = save or unique_save(self.settings, f"{player_id}-{pack.year}")
        llm = llm or make_llm(self.settings, self.llm_arg)
        game = Game.create(self.settings, path, pack, player_id, seed=seed, llm=llm)
        self._attach(game)
        self.say("system", f"New life begun: {game.pc['name']} · save {path.name} · seed {seed}")
        self.show(await game.begin())
        return True

    async def load(self, path: Path) -> bool:
        self.banner()
        await self.close_game()
        try:
            game = Game.open(self.settings, path, make_llm(self.settings, self.llm_arg))
        except FileNotFoundError:
            self.say("error", f"No such save: {path}")
            return False
        self._attach(game)
        self.say("system", f"Loaded {path.name}: {game.pc['name']}" + (" (finished chronicle)" if game.ended else ""))
        self.show(await game.begin())
        return True

    async def new_interactive(self) -> bool:
        self.banner()
        starts = featured_starts()
        rows = [f"{i:>2}. {f['character']} ({f['year']} AC) — {f['blurb']}" for i, f in enumerate(starts, 1)]
        self.say("sheet", "Featured lives:\n" + "\n".join(rows) +
                 "\n\nChoose a number, or type any character's name to be found on A Wiki of Ice and Fire.")
        choice = await self.io.read("Whom will you be? ")
        if choice is None:
            return False
        choice = choice.strip()
        if choice.isdigit() and 1 <= int(choice) <= len(starts):
            return await self.start_featured(starts[int(choice) - 1]["id"])
        llm = make_llm(self.settings, self.llm_arg)
        lore = make_lore(self.settings, llm)
        titles: list[str] = []
        if lore is not None:
            try:
                titles = await lore.search_characters(choice)
            except Exception as e:
                self.say("error", f"WOIAF is unreachable ({e}). Offline, choose a featured life with a number.")
            await lore.aclose()
        await llm.aclose()
        if not titles:
            self.say("system", f"No one called '{choice}' could be found.")
            return False
        title = titles[0]
        if len(titles) > 1:
            self.say("sheet", "\n".join(f"{i}. {t}" for i, t in enumerate(titles[:8], 1)))
            pick = await self.io.read("Which? ")
            if pick and pick.strip().isdigit() and 1 <= int(pick) <= min(8, len(titles)):
                title = titles[int(pick) - 1]
        year = await self.io.read("In what year (AC)? ")
        try:
            y = int((year or "").strip().upper().removesuffix("AC").strip())
        except ValueError:
            self.say("error", "A year, such as 298.")
            return False
        return await self.start_custom(title, y, None)

    # ------------------------------------------------------------ main loop
    async def run(self) -> None:
        self.banner()
        if self.game is None:
            saves = list_saves(self.settings)
            if saves:
                self.say("system", "Saves: " + ", ".join(p.stem for p in saves[:6]) +
                         "\nType /load <name> to continue one, or /new to begin a new life.")
            elif not await self.new_interactive():
                self.say("system", "Type /new to begin, or /quit.")
        while True:
            text = await self.io.read("> ")
            if text is None:
                break
            t = text.strip()
            low = t.lower()
            if low in ("/quit", "/exit", "/q"):
                break
            if low.startswith("/new"):
                await self.new_interactive()
                continue
            if low == "/saves":
                rows = [f"{p.stem}  ({time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))})"
                        for p in list_saves(self.settings)]
                self.say("sheet", "\n".join(rows) if rows else "No saves yet.")
                continue
            if low.startswith("/load"):
                name = t[5:].strip()
                saves = list_saves(self.settings)
                path = next((p for p in saves if p.stem == name), None) or (saves[0] if not name and saves else None)
                if path is None:
                    self.say("error", f"No save called '{name}'. /saves lists them.")
                else:
                    await self.load(path)
                continue
            if low.startswith("/save"):
                if self.game is None or self.game.path is None:
                    self.say("system", "Nothing to save.")
                    continue
                stamp = time.strftime("%Y%m%d-%H%M%S")
                dest = self.game.path.with_name(f"{self.game.path.stem}-snapshot-{stamp}.db")
                self.game.store.conn.execute("PRAGMA wal_checkpoint(FULL)")
                shutil.copy2(self.game.path, dest)
                self.say("system", f"The game autosaves every turn. Snapshot written: {dest.name}")
                continue
            if self.game is None:
                self.say("system", "No life in progress. /new to begin, /load to continue.")
                continue
            self.show(await self.game.handle(t))
        await self.close_game()
        self.io.write("Farewell. Valar morghulis.")

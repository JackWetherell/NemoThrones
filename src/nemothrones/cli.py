"""Command line: ``nemothrones [play|new|saves|doctor|lore|config]``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_settings


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--home", type=Path, help="game data directory (default: sandbox workspace or ~/.nemothrones)")
    p.add_argument("--config", type=Path, help="config file (default: <home>/nemothrones.yaml)")
    p.add_argument("--llm", help="route every role to one backend: mock | replay:<save.db> | inference_local | ...")
    p.add_argument("--seed", type=int, help="dice seed for a new game")
    p.add_argument("--script", type=Path, help="read input lines from a file (non-interactive)")
    p.add_argument("--plain", action="store_true", help="no ANSI colour")
    p.add_argument("--dev", action="store_true", help="developer mode: raise internal errors")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nemothrones", description="NemoThrones: live a life in A Song of Ice and Fire.")
    ap.add_argument("--version", action="version", version=f"nemothrones {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    play = sub.add_parser("play", help="play (load the latest save, or start a new life)")
    _common(play)
    play.add_argument("--save", help="save name to load (see `nemothrones saves`)")

    new = sub.add_parser("new", help="begin a new life")
    _common(new)
    new.add_argument("--start", help="featured start id (e.g. eddard_298); offline packs need no wiki")
    new.add_argument("--character", help="any WOIAF character title (builds an era pack from the wiki)")
    new.add_argument("--year", type=int, help="start year (AC) for --character")
    new.add_argument("--moon", type=int, help="start moon 1-12 for --character")
    new.add_argument("--save", type=Path, help="explicit save path")

    saves = sub.add_parser("saves", help="list saves")
    _common(saves)

    doc = sub.add_parser("doctor", help="check the stack: inference.local, Hermes, WOIAF, storage")
    _common(doc)
    doc.add_argument("--offline", action="store_true", help="skip network checks")

    lore = sub.add_parser("lore", help="WOIAF tools: search | page | cast | probe")
    _common(lore)
    lore.add_argument("action", choices=["search", "page", "cast", "probe", "dossier"])
    lore.add_argument("query", nargs="?", default="")
    lore.add_argument("--year", type=int, default=298)

    cfg = sub.add_parser("config", help="print the effective configuration")
    _common(cfg)
    return ap


def _settings(args):
    s = load_settings(args.home, args.config)
    if getattr(args, "plain", False):
        s.game.color = False
    s.dev = bool(getattr(args, "dev", False))
    return s


def _io(args):
    from .repl.app import ScriptIO, TerminalIO

    return ScriptIO(args.script) if getattr(args, "script", None) else TerminalIO()


async def _play(args) -> int:
    from .repl.app import Session, list_saves

    settings = _settings(args)
    session = Session(settings, _io(args), llm_arg=args.llm, seed=args.seed)
    if args.save:
        path = next((p for p in list_saves(settings) if p.stem == args.save), None)
        if path is None:
            print(f"No save named {args.save!r}.", file=sys.stderr)
            return 2
        await session.load(path)
    else:
        saves = list_saves(settings)
        if saves:
            await session.load(saves[0])
    await session.run()
    return 0


async def _new(args) -> int:
    from .repl.app import Session

    settings = _settings(args)
    session = Session(settings, _io(args), llm_arg=args.llm, seed=args.seed)
    ok = False
    if args.start:
        ok = await session.start_featured(args.start, args.save)
    elif args.character:
        if args.year is None:
            print("--character needs --year", file=sys.stderr)
            return 2
        ok = await session.start_custom(args.character, args.year, args.moon, args.save)
    else:
        ok = await session.new_interactive()
    if not ok:
        await session.close_game()
        return 1
    await session.run()
    return 0


def _saves(args) -> int:
    from .repl.app import list_saves

    for p in list_saves(_settings(args)):
        print(p.stem)
    return 0


async def _lore(args) -> int:
    from .llm import LLM
    from .lore.service import LoreService

    settings = _settings(args)
    if args.llm:
        settings.force_backend(args.llm)
    llm = LLM(settings)
    svc = LoreService(settings, llm)
    try:
        if args.action == "search":
            for t in await svc.search_characters(args.query):
                print(t)
        elif args.action == "page":
            print(await svc.lore_text(args.query, args.year))
        elif args.action == "probe":
            print(json.dumps(await svc.probe(args.query or "Eddard Stark"), indent=1, ensure_ascii=False))
        elif args.action == "cast":
            for e in await svc.cast(args.query, args.year):
                print(f"{e.score:5.2f}  {e.title:<32} {', '.join(e.reasons)}")
        elif args.action == "dossier":
            d = await svc.dossier(args.query, args.year)
            print(json.dumps(d, indent=1, ensure_ascii=False))
    finally:
        await svc.aclose()
        await llm.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["play", *(argv or sys.argv[1:])])
    if args.cmd == "play":
        return asyncio.run(_play(args))
    if args.cmd == "new":
        return asyncio.run(_new(args))
    if args.cmd == "saves":
        return _saves(args)
    if args.cmd == "doctor":
        from .doctor import run_doctor

        return asyncio.run(run_doctor(_settings(args), offline=args.offline))
    if args.cmd == "lore":
        return asyncio.run(_lore(args))
    if args.cmd == "config":
        print(_settings(args).model_dump_json(indent=1))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

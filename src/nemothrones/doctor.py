"""`nemothrones doctor`: check every link in the chain, from inside the sandbox.

Reports in the same ok/warn/fail style as nemo's setup-host.sh. Network checks make
one tiny request each (a few tokens of inference, one wiki query).
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from . import __version__
from .config import SANDBOX_WORKSPACE, Settings
from .llm.backends import BackendError, HttpChatBackend, resolve_api_key


class Report:
    def __init__(self):
        self.failed = 0

    def ok(self, msg):
        print(f"\033[1;32m[ ok ]\033[0m {msg}")

    def warn(self, msg):
        print(f"\033[1;33m[warn]\033[0m {msg}")

    def bad(self, msg):
        print(f"\033[1;31m[fail]\033[0m {msg}")
        self.failed += 1

    def step(self, msg):
        print(f"\n\033[1m== {msg}\033[0m")


async def _ping(name: str, settings: Settings, r: Report) -> None:
    cfg = settings.llm.backends[name]
    be = HttpChatBackend(name, cfg, settings.home)
    t0 = time.monotonic()
    try:
        comp = await be.complete([{"role": "system", "content": "Reply with the JSON object {\"ok\": true} and nothing else."},
                                  {"role": "user", "content": "ping"}], role="doctor", max_tokens=20, temperature=0)
        r.ok(f"{name}: {cfg.base_url} answered in {time.monotonic() - t0:.1f}s "
             f"(model {comp.model}, {comp.tokens_in}+{comp.tokens_out} tokens): {comp.text.strip()[:60]!r}")
    except BackendError as e:
        r.bad(f"{name}: {e}")
    finally:
        await be.aclose()


async def run_doctor(settings: Settings, offline: bool = False) -> int:
    r = Report()
    r.step("Runtime")
    r.ok(f"nemothrones {__version__} on Python {sys.version.split()[0]} ({sys.executable})")
    in_sandbox = SANDBOX_WORKSPACE.is_dir()
    r.ok("running inside the OpenShell sandbox") if in_sandbox else r.warn(
        "not inside the sandbox (fine for development; play inside it for the nemo security model)")

    r.step("Storage")
    home = settings.home
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        r.ok(f"home {home} is writable")
    except OSError as e:
        r.bad(f"home {home} is not writable: {e}")
    if in_sandbox and not str(home.resolve()).startswith("/sandbox/.hermes"):
        r.bad(f"home {home} is outside /sandbox/.hermes: it will NOT survive a sandbox rebuild")
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        r.ok(f"SQLite {sqlite3.sqlite_version} with FTS5")
    except sqlite3.OperationalError:
        r.bad("SQLite lacks FTS5 (memory retrieval needs it)")
    saves = list(settings.saves_dir.glob("*.db")) if settings.saves_dir.is_dir() else []
    r.ok(f"{len(saves)} save(s) in {settings.saves_dir}")

    r.step("Hermes integration")
    plugin = Path("/sandbox/.hermes/plugins/nemothrones/plugin.yaml")
    if in_sandbox:
        r.ok("plugin installed") if plugin.is_file() else r.warn("plugin not installed (run scripts/deploy.sh)")
        cfg = Path("/sandbox/.hermes/config.yaml")
        if cfg.is_file() and "nemothrones" in cfg.read_text():
            r.ok("plugin enabled in config.yaml")
        else:
            r.warn("plugin not enabled in /sandbox/.hermes/config.yaml (plugins.enabled)")
    key = resolve_api_key(settings.llm.backends["hermes"], home)
    r.ok("Hermes API key found") if key else r.warn(
        "no Hermes API key (NEMOTHRONES_HERMES_TOKEN, <home>/hermes_api_token or /sandbox/.hermes/.env); "
        "the GM will fall back to inference.local")

    r.step("Budget")
    b = settings.budget
    if b.session_hard_usd <= b.session_warn_usd:
        r.warn("budget.session_hard_usd should exceed session_warn_usd")
    r.ok(f"session warn ${b.session_warn_usd:.2f}, hard stop ${b.session_hard_usd:.2f}. "
         "Also keep a hard credit limit on the OpenRouter key.")

    if offline:
        r.step("Network (skipped: --offline)")
    else:
        r.step("Inference")
        await _ping("inference_local", settings, r)
        if key:
            await _ping("hermes", settings, r)
        r.step("A Wiki of Ice and Fire")
        try:
            from .llm import LLM
            from .lore.service import LoreService

            svc = LoreService(settings, LLM(settings))
            info = await svc.probe("Eddard Stark")
            await svc.aclose()
            r.ok(f"{info['site']} reachable; '{info['title']}' rev {info['revid']}: infobox "
                 f"'{info['infobox_template']}' with {len(info['infobox_keys'])} fields "
                 f"(born {info['born']}, died {info['died']})")
            if not info["infobox_keys"]:
                r.warn("no infobox fields parsed: the WOIAF template may have changed; see `nemothrones lore probe`")
        except Exception as e:  # noqa: BLE001 - report whatever broke
            r.bad(f"WOIAF unreachable or unparseable: {type(e).__name__}: {e} "
                  "(is policy/nemothrones.yaml applied, and the game venv in its binaries list?)")

    r.step("Result")
    if r.failed:
        r.warn(f"{r.failed} check(s) failed")
        return 1
    r.ok("ready. Winter is coming.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    from .config import load_settings

    sys.exit(asyncio.run(run_doctor(load_settings(), offline="--offline" in sys.argv)))

"""Fate (what canon holds after the start date) must never reach a character agent,
the narrator or the GM. Checked statically (imports) and dynamically (every prompt)."""

import ast
from pathlib import Path

import nemothrones
from nemothrones.llm import LLM
from nemothrones.llm.backends import MockBackend
from nemothrones.llm.mock import default_responder
from tests.conftest import run

PKG = Path(nemothrones.__file__).parent
ALLOWED = {"world/fate.py", "world/seed.py", "agents/roles/chronicler.py", "engine/commands.py",
           "lore/erapack.py", "lore/service.py"}


def test_only_allowed_modules_import_fate():
    offenders = []
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG).as_posix()
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("fate"):
                if rel not in ALLOWED:
                    offenders.append(rel)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "FROM fate" in node.value:
                if rel not in ALLOWED:
                    offenders.append(rel)
    assert not offenders, offenders


def test_no_prompt_contains_fate_or_canon_horizon(settings, pack, make_game):
    mock = MockBackend(default_responder)
    llm = LLM(settings, backends={"mock": mock})
    g = make_game(llm=llm)
    run(g.begin())
    for text in ['"Catelyn, what is it?"', "1", "go to the yard", '"Jon, a word."', "/wait 3 days",
                 "write to Robert Baratheon: Your Grace, Winterfell awaits you."]:
        run(g.handle(text))
    fates = [c.fate for c in pack.characters if c.fate]
    horizon = [ce.summary for ce in pack.canon_events]
    assert mock.calls, "no LLM calls were made"
    roles_seen = set()
    for role, messages in mock.calls:
        roles_seen.add(role)
        blob = "\n".join(m["content"] for m in messages)
        for f in fates:
            assert f[:60] not in blob, f"fate leaked into a {role} prompt"
        if role != "director":
            for h in horizon:
                assert h[:50] not in blob, f"canon horizon leaked into a {role} prompt"
    assert {"agent", "narrator", "gm", "director"} <= roles_seen

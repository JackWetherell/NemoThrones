"""End-to-end: the real CLI, the real engine, the mock LLM, a scripted life."""

import sqlite3
import stat
from pathlib import Path

from nemothrones.cli import main
from nemothrones.config import Settings
from nemothrones.engine.game import Game
from nemothrones.llm import LLM
from tests.conftest import run

SCRIPT = Path(__file__).parent.parent / "fixtures" / "sessions" / "ned_298.txt"


def play(tmp_path, capsys, *extra):
    home = tmp_path / "home"
    code = main(["new", "--home", str(home), "--start", "eddard_298", "--llm", "mock", "--seed", "42",
                 "--script", str(SCRIPT), "--plain", "--dev", *extra])
    out = capsys.readouterr().out
    return code, out, home


def test_scripted_life_ends_in_a_chronicle(tmp_path, capsys):
    code, out, home = play(tmp_path, capsys)
    assert code == 0
    assert "N E M O T H R O N E S" in out
    assert "the godswood, Winterfell" in out
    assert "Catelyn" in out
    assert "Lord of Winterfell" in out                     # /me
    assert "Hear Me Roar!" in out                          # /house Lannister
    assert "You make your way to the yard" in out          # move
    assert "diplomacy 9 vs Theon Greyjoy diplomacy" in out  # /why after an opposed roll
    assert "on its way" in out                             # /letters
    assert "Event-log replay matches" in out               # /debug rebuild
    assert "This could be the death of you" in out         # lethal confirmation
    assert "Eddard Stark is dead" in out
    assert "The Chronicle of Eddard Stark" in out
    assert "Condition: dead" in out                        # /me after death
    save = next((home / "saves").glob("*.db"))
    assert not save.stat().st_mode & stat.S_IWUSR          # ironman: archived read-only
    db = sqlite3.connect(f"file:{save}?mode=ro", uri=True)
    alive = db.execute("SELECT alive FROM characters WHERE id='eddard_stark'").fetchone()[0]
    assert alive == 0
    assert db.execute("SELECT count(*) FROM rolls").fetchone()[0] >= 2
    assert db.execute("SELECT count(*) FROM llm_log WHERE ok=1").fetchone()[0] > 5


def test_same_seed_same_story(tmp_path, capsys):
    _, out1, _ = play(tmp_path / "a", capsys)
    _, out2, _ = play(tmp_path / "b", capsys)
    strip = lambda s: "\n".join(ln for ln in s.splitlines() if "seed" not in ln and "$" not in ln and "avg" not in ln)  # noqa: E731
    assert strip(out1) == strip(out2)


def test_save_and_resume(tmp_path, settings, pack):
    path = tmp_path / "resume.db"
    g = Game.create(settings, path, pack, "jon_snow", seed=5, llm=LLM(settings))
    run(g.begin())
    run(g.handle('"Robb, will you spar?"'))
    turn, now, options = g.store.turn, g.store.now, [o.label for o in g.options]
    run(g.close())
    g2 = Game.open(settings, path, LLM(settings))
    assert (g2.store.turn, g2.store.now) == (turn, now)
    assert [o.label for o in g2.options] == options
    blocks = run(g2.begin())  # resume shows the last scene, not a new opening
    assert any(b.kind == "prose" for b in blocks)
    run(g2.handle("1"))
    assert g2.store.turn == turn + 1
    run(g2.close())


def test_replay_backend_reproduces_a_session(tmp_path, capsys):
    _, out1, home = play(tmp_path, capsys)
    save = next((home / "saves").glob("*.db"))
    code = main(["new", "--home", str(tmp_path / "replay-home"), "--start", "eddard_298",
                 "--llm", f"replay:{save}", "--seed", "42", "--script", str(SCRIPT), "--plain", "--dev"])
    out2 = capsys.readouterr().out
    assert code == 0
    assert "The Chronicle of Eddard Stark" in out2
    prose1 = [ln for ln in out1.splitlines() if ln.startswith("the godswood") or ln.startswith("the yard")]
    prose2 = [ln for ln in out2.splitlines() if ln.startswith("the godswood") or ln.startswith("the yard")]
    assert prose1 == prose2


def test_other_featured_starts_open(tmp_path, capsys):
    for start in ("jon_298", "robert_298", "catelyn_298"):
        script = tmp_path / f"{start}.txt"
        script.write_text("/who\n/me\n")
        code = main(["new", "--home", str(tmp_path / start), "--start", start, "--llm", "mock", "--seed", "1",
                     "--script", str(script), "--plain", "--dev"])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "New life begun" in out


def test_settings_defaults_are_butterfly_and_tiered():
    s = Settings()
    assert s.game.canon_gravity == 0.6
    assert s.llm.roles["gm"].backend == "hermes" and s.llm.roles["gm"].fallback == ["inference_local"]
    assert s.llm.roles["agent"].backend == "inference_local"

import importlib.util
import json
from pathlib import Path

from tests.conftest import run

PLUGIN = Path(__file__).parents[2] / "hermes" / "plugin" / "nemothrones" / "__init__.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("nemothrones_hermes_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = (toolset, schema, handler)


def test_plugin_registers_and_reads_the_active_save(make_game, settings, monkeypatch):
    g = make_game()
    run(g.begin())
    settings.home.mkdir(parents=True, exist_ok=True)
    (settings.home / "active_save").write_text(str(g.path))
    monkeypatch.setenv("NEMOTHRONES_HOME", str(settings.home))
    mod = load_plugin()
    ctx = FakeCtx()
    mod.register(ctx)
    assert set(ctx.tools) == {"nt_world", "nt_scene", "nt_lore", "nt_canon"}
    assert all(t[0] == "nemothrones" and t[1]["name"] == n for n, t in ctx.tools.items())
    scene = json.loads(mod.nt_scene())
    assert "Eddard Stark" in scene["present_names"]
    ned = json.loads(mod.nt_world({"kind": "character", "id": "Eddard Stark"}))
    assert ned["id"] == "eddard_stark" and ned["relationships"]
    assert "Great Sept of Baelor" not in json.dumps(ned)  # fate stays out of nt_world
    canon = json.loads(mod.nt_canon())
    assert any(c["id"] == "royal_visit" for c in canon)
    events = json.loads(mod.nt_world(kind="events"))
    assert all(e["kind"] != "world_seeded" for e in events)
    assert "error" in json.loads(mod.nt_lore({"title": "Eddard Stark"}))  # no lore cache yet

from nemothrones.agents.schemas import AgentTurn, Effect, Resolve
from nemothrones.engine import scheduler
from nemothrones.engine.warden import Warden
from tests.conftest import run


def test_seed_and_queries(make_game):
    g = make_game()
    s = g.store
    assert s.player_id == "eddard_stark"
    assert s.find_character("Ned")["id"] == "eddard_stark"
    assert s.find_character("Lord Stark")["id"] == "eddard_stark"
    assert s.find_character("Luwin")["id"] == "maester_luwin"
    assert s.find_character("the Imp")["id"] == "tyrion_lannister"
    assert s.find_house("Stark")["words"] == "Winter Is Coming"
    assert s.find_location("the capital")["id"] == "kings_landing"
    assert s.relationship("catelyn_stark", "jon_snow")["opinion"] < 0


def test_fts_and_first(make_game):
    g = make_game()
    hits = g.store.search_beliefs("eddard_stark", "Jon Arryn", limit=3)
    assert "Jon Arryn" in hits[0]["content"]


def test_event_log_rebuilds_world(make_game):
    g = make_game()
    run(g.begin())
    for text in ['"Catelyn, what news?"', "1", "go to the yard", "attack Theon"]:
        run(g.handle(text))
    run(g.handle("/wait 2 days"))
    fresh = g.store.rebuild()
    assert fresh.snapshot() == g.store.snapshot()


def test_warden_resolve(make_game):
    g = make_game()
    run(g.begin())
    w = Warden(g.store)
    present = g.present_npcs()
    r = Resolve.model_validate({
        "kind": "act", "targets": ["Catelyn", "Tyrion Lannister", "nobody"],
        "adjudication": {"kind": "opposed", "skill": "juggling", "opponent": "Tyrion", "difficulty": 99,
                         "outcomes": [
                             {"label": "A", "effects": [
                                 {"type": "relationship", "a": "eddard_stark", "b": "catelyn_stark", "opinion": 90},
                                 {"type": "relationship", "a": "catelyn_stark", "b": "eddard_stark", "opinion": 90},
                                 {"type": "death", "target": "tyrion_lannister"},
                                 {"type": "impact", "target": "catelyn_stark", "value": 10}]},
                             {"label": "B"}]}})
    clean, unknown = w.check_resolve(r, present)
    assert clean.targets == ["catelyn_stark"]  # Tyrion is not here
    assert "nobody" in unknown
    adj = clean.adjudication
    assert adj.skill == "prowess" and adj.kind == "check" and adj.difficulty == 30
    effects = adj.outcomes[0].effects
    kinds = [(e.type, e.a or e.target) for e in effects]
    assert ("relationship", "eddard_stark") not in kinds   # nobody decides the PC's feelings
    rel = next(e for e in effects if e.type == "relationship")
    assert rel.opinion == 30                               # clamped
    assert not any(e.type == "death" for e in effects)    # absent target
    assert next(e for e in effects if e.type == "impact").value == 3


def test_warden_agent_turn(make_game):
    g = make_game()
    run(g.begin())
    w = Warden(g.store)
    t = AgentTurn.model_validate({"speech": '"Hello"', "speak_to": "Ned",
                                  "action": {"type": "attack", "target": "Tyrion"},
                                  "opinion_changes": [{"of": "Ned", "opinion": 99}]})
    clean = w.check_agent_turn(t, "catelyn_stark", g.scene["present"])
    assert clean.speech == "Hello" and clean.speak_to == "eddard_stark"
    assert clean.action is None  # cannot attack someone who is not here
    assert clean.opinion_changes[0].opinion == 20


def test_effects_wound_and_death(make_game):
    from nemothrones.engine import effects as fx

    g = make_game()
    s = g.store
    with s.transaction():
        rep = fx.apply_effects(s, [Effect(type="wound", target="theon_greyjoy", severity=2),
                                   Effect(type="death", target="jory_cassel", cause="test")], actor_id=None)
    assert s.character("theon_greyjoy")["attrs"]["wounds"][0]["severity"] == 2
    assert not s.character("jory_cassel")["alive"]
    assert rep.deaths == ["jory_cassel"]


def test_tiers_and_cadence(make_game, settings):
    g = make_game()
    run(g.begin())
    s = g.store
    tiers = {c["id"]: c["tier"] for c in s.characters()}
    assert tiers["eddard_stark"] == 0
    assert tiers["catelyn_stark"] == 0  # in the opening scene
    assert sum(1 for t in tiers.values() if t == 1) <= settings.game.tier1_size
    assert scheduler.cadence(1, settings.game, 0.0) == settings.game.tier1_cadence_min
    assert scheduler.cadence(1, settings.game, 1.0) == 3 * settings.game.tier1_cadence_min
    t1, t2 = scheduler.due(s, s.now + 30 * 24 * 60, g.scene["present"])
    ids = {c["id"] for c in t1 + t2}
    assert "eddard_stark" not in ids and "catelyn_stark" not in ids
    assert "robert_baratheon" in ids

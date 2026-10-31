"""The lore pipeline against synthetic WOIAF-format fixtures (see tests/fixtures/wiki/README.md)."""

import pytest

from nemothrones.config import Settings
from nemothrones.engine.game import Game
from nemothrones.llm import LLM
from nemothrones.llm.backends import MockBackend
from nemothrones.llm.mock import default_responder
from nemothrones.lore import service as service_mod
from nemothrones.lore.dates import alive_at, parse_year_range, years_in
from nemothrones.lore.parse import parse_wikitext
from nemothrones.lore.service import LoreService
from nemothrones.lore.slicer import resolve_ambiguous, slice_page
from tests.conftest import run
from tests.wikimock import WikiMock, pages


@pytest.fixture
def lore_settings(tmp_path) -> Settings:
    s = Settings(home=tmp_path / "home")
    s.force_backend("mock")
    s.lore.rate_limit_s = 0.0
    s.game.cast_size = 8
    s.dev = True
    return s


@pytest.fixture
def wiki():
    return WikiMock()


@pytest.fixture
def svc(lore_settings, wiki):
    llm = LLM(lore_settings)
    s = LoreService(lore_settings, llm, transport=wiki.transport())
    yield s
    run(s.aclose())


def page(title):
    return parse_wikitext(title, pages()[title])


# ------------------------------------------------------------------ dates
@pytest.mark.parametrize("text,expected", [
    ("In 263 AC Winterfell", (263, 263)),
    ("In or between 282 AC and 283 AC", (282, 283)),
    ("Between 282 and 283 AC", (282, 283)),
    ("c. 250 AC", (250, 250)),
    ("Before 280 AC", (None, 279)),
    ("After 290 AC", (291, None)),
    ("In 2 BC", (-1, -1)),
    ("283–284 AC", (283, 284)),
    ("unknown", None),
])
def test_parse_year_range(text, expected):
    assert parse_year_range(text) == expected


def test_years_and_alive():
    assert years_in("In 282 AC and again in 289 AC; 2 BC") == [282, 289, -1]
    assert alive_at((263, 263), (298, 298), 298) is True    # dies during the year: that is fate
    assert alive_at((230, 230), (282, 282), 298) is False
    assert alive_at((284, 284), None, 283) is False         # not yet born
    assert alive_at(None, None, 298) is None


# ------------------------------------------------------------------ parse
def test_parse_character_page():
    p = page("Eddard Stark")
    assert p.infobox_name == "Character" and p.is_character
    assert p.born == (263, 263) and p.died == (298, 298)
    assert p.allegiances() == ["House Stark"]
    assert "Robb Stark" in p.kin_links() and "Catelyn Tully" not in p.kin_links()  # spouses are not kin links
    assert "Synthetic fixture" not in p.infobox["born"]   # <ref> removed
    paths = [" > ".join(s.path) for s in p.sections]
    assert "Recent Events > A Game of Thrones" in paths
    assert "Lord Eddard Stark" in p.lead
    assert "Robert I Baratheon" in p.links
    assert "House Stark" in p.categories


def test_non_character_pages():
    assert not page("Winterfell").is_character
    assert not page("House Stark").is_character
    assert page("House Stark").infobox["words"] == "Winter is Coming"


# ------------------------------------------------------------------ slicer
def test_slicer_keeps_the_future_out_of_the_past():
    sl = slice_page(page("Robb Stark"), 298)
    assert "trained at arms" in sl.past
    assert "King in the North" not in sl.past
    assert "Red Wedding" not in sl.past
    assert "Red Wedding" in sl.fate
    # the lead is descriptive and spoiler-heavy: never past without a classifier
    assert any(p.where == "lead" and p.label == "ambiguous" for p in sl.passages)


def test_slicer_skips_quotes_and_resolves_conservatively(lore_settings):
    sl = slice_page(page("Eddard Stark"), 298)
    assert "swing the sword" not in sl.past + sl.fate      # quotes are never kept
    assert "Greyjoy Rebellion" in sl.past
    assert "beheaded" not in sl.past
    sl = run(resolve_ambiguous(LLM(lore_settings), "Eddard Stark", sl))
    assert not sl.ambiguous                                # mock classifier says fate; so does the fallback
    assert "beheaded" in sl.fate


def test_slicer_later_start_moves_books_into_the_past():
    sl = slice_page(page("Robb Stark"), 301)
    assert "King in the North" in sl.past


# ------------------------------------------------------------------ dossiers never see the future
def test_dossier_prompt_has_no_future(lore_settings, wiki):
    mock = MockBackend(default_responder)
    llm = LLM(lore_settings, backends={"mock": mock})
    s = LoreService(lore_settings, llm, transport=wiki.transport())
    d, f = run(s.dossier_for(page("Jon Snow"), 298))
    run(s.aclose())
    dossier_prompts = ["\n".join(m["content"] for m in msgs) for role, msgs in mock.calls if role == "dossier"]
    assert dossier_prompts
    for blob in dossier_prompts:
        assert "Lord Commander" not in blob                # lifetime title withheld
        assert "elected" not in blob
    fate_prompts = ["\n".join(m["content"] for m in msgs) for role, msgs in mock.calls if role == "fate"]
    assert any("Night's Watch" in b for b in fate_prompts)  # the future goes to the Director instead


# ------------------------------------------------------------------ cast and era packs
def test_cast_selection(svc):
    entries = run(svc.cast("Eddard Stark", 298))
    titles = [e.title for e in entries]
    assert {"Catelyn Stark", "Robb Stark", "Jon Snow"} <= set(titles)
    assert "Rickard Stark" not in titles                   # dead before 298
    assert "House Stark" not in titles and "Winterfell" not in titles
    assert all(e.score > 0 for e in entries)
    robb = next(e for e in entries if e.title == "Robb Stark")
    assert "kin" in robb.reasons


def test_era_pack_builds_and_plays(svc, lore_settings, wiki, tmp_path):
    pack, pid = run(svc.build_pack("Ned Stark", 298))       # via a redirect
    assert pid == "eddard_stark" and pack.source == "woiaf"
    ids = {c.id for c in pack.characters}
    assert {"catelyn_stark", "robb_stark", "jon_snow"} <= ids
    stark = next(h for h in pack.houses if h.id == "stark")
    assert stark.words == "Winter is Coming" and stark.seat == "winterfell"
    assert any(r.a == "robb_stark" and r.b == "eddard_stark" and r.kind == "father" for r in pack.relationships)
    ned = pack.character("eddard_stark")
    assert ned.location == "winterfell" and ned.fate
    assert "beheaded" not in ned.summary
    assert pack.common_knowledge and "Robert I Baratheon" in pack.common_knowledge[0].content
    requests = wiki.calls.__len__()
    again, _ = run(svc.build_pack("Eddard Stark", 298))   # cached: no new requests
    assert len(wiki.calls) == requests and again.id == pack.id

    game = Game.create(lore_settings, tmp_path / "era.db", pack, pid, seed=3, llm=LLM(lore_settings))
    blocks = run(game.begin())
    assert any("Winterfell" in b.text for b in blocks)
    run(game.handle('"Catelyn, a word."'))
    assert game.store.turn == 1
    run(game.close())


def test_dead_player_is_refused(svc):
    with pytest.raises(ValueError):
        run(svc.build_pack("Rickard Stark", 298))


def test_lore_text_is_spoiler_filtered(svc):
    text = run(svc.lore_text("Robb Stark", 298))
    assert "trained at arms" in text
    assert "Red Wedding" not in text and "King in the North" not in text
    assert "Died" not in text
    assert "CC BY-SA" in text and "awoiaf.westeros.org" in text
    later = run(svc.lore_text("Rickard Stark", 298))
    assert "Died" in later                                  # died before the in-game year: fine to show


def test_probe(svc):
    info = run(svc.probe("Eddard Stark"))
    assert info["infobox_template"] == "Character" and "born" in info["infobox_keys"]


def test_promotion_by_letter_and_lore_command(settings, pack, tmp_path, wiki, monkeypatch):
    monkeypatch.setattr(service_mod, "DEFAULT_TRANSPORT", wiki.transport())
    settings.lore.rate_limit_s = 0.0
    game = Game.create(settings, tmp_path / "promo.db", pack, "eddard_stark", seed=9, llm=LLM(settings))
    game.lore = LoreService(settings, game.llm)
    run(game.begin())
    run(game.handle("write to Daenerys Targaryen: Princess, the North remembers your father's crimes."))
    dany = game.store.find_character("Daenerys Targaryen")
    assert dany is not None and dany["location_id"] == "pentos"
    assert game.store.conn.execute("SELECT count(*) FROM deliveries WHERE recipient_id=?",
                                   (dany["id"],)).fetchone()[0] == 1
    blocks = run(game.handle("/lore Robb Stark"))
    assert "Red Wedding" not in blocks[0].text and "trained at arms" in blocks[0].text
    run(game.close())


def test_cli_new_from_wiki(tmp_path, capsys, wiki, monkeypatch):
    from nemothrones.cli import main

    monkeypatch.setattr(service_mod, "DEFAULT_TRANSPORT", wiki.transport())
    home = tmp_path / "home"
    home.mkdir()
    (home / "nemothrones.yaml").write_text("lore: {rate_limit_s: 0}\ngame: {cast_size: 6}\n")
    script = tmp_path / "s.txt"
    script.write_text("/me\n/who\n/lore Robert I Baratheon\n")
    code = main(["new", "--home", str(home), "--character", "Daenerys Targaryen", "--year", "298",
                 "--llm", "mock", "--seed", "4", "--script", str(script), "--plain", "--dev"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "New life begun: Daenerys Targaryen" in out
    assert "Pentos" in out


def test_succession_years_name_the_old_monarch():
    from nemothrones.lore.cast import anchors_at, monarchs_at_start

    assert monarchs_at_start(298) == ["Robert I Baratheon"]       # Joffrey would be a spoiler
    assert "Joffrey I Baratheon" in anchors_at(298)[0]          # but is still a cast candidate
    assert monarchs_at_start(129) == ["Viserys I Targaryen"]
    assert set(monarchs_at_start(130)) == {"Aegon II Targaryen", "Rhaenyra Targaryen"}


def test_start_year_facts_are_ambiguous_not_fate():
    sl = slice_page(page("Daenerys Targaryen"), 298)
    labels = {p.text: p.label for p in sl.passages}
    assert any("came to Pentos" in t and lab == "past" for t, lab in labels.items())
    assert any("betrothed" in t and lab == "ambiguous" for t, lab in labels.items())
    assert any("pyre" in p.text for p in sl.ambiguous)     # AGOT straddles 298: per paragraph
    assert "pyre" not in sl.past


def test_lifetime_infobox_lists_never_leak(svc):
    text = run(svc.lore_text("Jon Snow", 298))
    assert "Night's Watch" not in text and "Lord Commander" not in text

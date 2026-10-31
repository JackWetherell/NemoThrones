"""Turn a WorldPack into the first events of a save."""

from __future__ import annotations

from ..engine import clock
from .fate import record_fate
from .pack import CharacterSpec, WorldPack
from .store import Store


def character_row(c: CharacterSpec, *, is_player: bool, start_t: int) -> dict:
    born_t = None
    if c.born_year is not None:
        born_t = clock.to_t(c.born_year, c.born_moon or 1, 1)
    return {
        "id": c.id,
        "name": c.name,
        "wiki_title": c.wiki_title,
        "house_id": c.house,
        "born_t": born_t,
        "alive": True,
        "location_id": c.location,
        "tier": 0 if is_player else 2,
        "next_act_t": None,
        "last_act_t": start_t,
        "importance": c.importance,
        "canon": c.canon,
        "is_player": is_player,
        "attrs": {"skills": c.skills, "health": c.health, "wounds": []},
        "persona": {
            "titles": c.titles, "aliases": c.aliases, "gender": c.gender, "culture": c.culture,
            "faith": c.faith, "appearance": c.appearance, "traits": c.traits, "values": c.values,
            "fears": c.fears, "voice": c.voice, "summary": c.summary, "secrets": c.secrets,
            "possessions": c.possessions,
        },
        "status": {"sublocation": c.sublocation, "activity": c.activity},
    }


def seed_world(store: Store, pack: WorldPack, player_id: str, *, seed: int) -> None:
    if not pack.character(player_id):
        raise ValueError(f"{player_id!r} is not a character in pack {pack.id!r}")
    start_t = clock.to_t(pack.year, pack.moon, pack.day, pack.hour)
    ids = {c.id for c in pack.characters}
    data = {
        "pack_id": pack.id,
        "title": pack.title,
        "player_id": player_id,
        "start_t": start_t,
        "season": pack.season,
        "houses": [
            {"id": h.id, "name": h.name, "seat_id": h.seat, "liege_id": h.liege, "head_id": h.head,
             "words": h.words, "sigil": h.sigil, "data": {"region": h.region}}
            for h in pack.houses
        ],
        "locations": [
            {"id": loc.id, "name": loc.name, "region": loc.region, "kind": loc.kind, "x": loc.x, "y": loc.y,
             "parent_id": loc.parent,
             "data": {"aliases": loc.aliases, "sublocations": loc.sublocations,
                      "has_maester": loc.has_maester, "description": loc.description}}
            for loc in pack.locations
        ],
        "routes": [r.model_dump() for r in pack.routes],
        "characters": [character_row(c, is_player=(c.id == player_id), start_t=start_t)
                       for c in pack.characters],
        "relationships": [r.model_dump() for r in pack.relationships if r.a in ids and r.b in ids],
        "canon_events": [
            {"id": ce.id,
             "t_expected": clock.to_t(ce.year, ce.moon or 1, ce.day or 1) if ce.year is not None else None,
             "title": ce.title, "summary": ce.summary, "participants": ce.participants,
             "location_id": ce.location, "preconditions": ce.preconditions, "drivers": ce.drivers,
             "chance": ce.chance, "wiki_ref": ce.wiki_ref}
            for ce in pack.canon_events
        ],
    }
    store.meta_set("seed", seed)
    store.meta_set("pack_id", pack.id)
    store.emit("world_seeded", data, t=start_t, visibility="system", summary=f"The world of {pack.title}")

    for c in pack.characters:
        if c.fate:
            record_fate(store, c.id, c.fate, source=pack.source)
        for k in pack.common_knowledge:
            store.emit("belief_added", {"holder": c.id, "subject": k.subject, "content": k.content,
                                        "source": k.source, "confidence": k.confidence,
                                        "is_true": k.is_true}, visibility="system")
        for k in c.knowledge:
            store.emit("belief_added", {"holder": c.id, "subject": k.subject, "content": k.content,
                                        "source": k.source, "confidence": k.confidence,
                                        "is_true": k.is_true}, visibility="system")
        for a in c.ambitions:
            store.emit("goal_added", {"owner": c.id, "level": "ambition", "text": a}, visibility="system")
        for p in c.plans:
            store.emit("goal_added", {"owner": c.id, "level": "plan", "text": p}, visibility="system")
        if c.summary:
            store.emit("memory_added", {"owner": c.id, "kind": "reflection", "text": c.summary,
                                        "importance": 6, "entities": [c.name]}, visibility="system")

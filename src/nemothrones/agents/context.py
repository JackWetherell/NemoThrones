"""Prompt context views.

Everything an agent, the narrator or the GM sees is assembled here, from world
tables only. This module (and every prompt builder) must never import
``world.fate``: fate is Director-only.
"""

from __future__ import annotations

from ..engine import clock
from ..world.store import Store
from .memory import relevant_beliefs, retrieve


def age_of(store: Store, c: dict) -> int | None:
    return clock.age_years(c.get("born_t"), store.now)


def house_name(store: Store, hid: str | None) -> str | None:
    h = store.house(hid)
    return h["name"] if h else None


def loc_name(store: Store, lid: str | None) -> str:
    loc = store.location(lid)
    return loc["name"] if loc else (lid or "somewhere")


def where(store: Store, location_id: str | None, sublocation: str | None) -> str:
    base = loc_name(store, location_id)
    return f"{sublocation}, {base}" if sublocation else base


def health_line(c: dict) -> str:
    if not c.get("alive", True):
        return f"dead ({c.get('status', {}).get('cause_of_death', 'unknown cause')})"
    attrs = c.get("attrs", {})
    wounds = attrs.get("wounds", [])
    if not wounds:
        return attrs.get("health", "hale")
    worst = max(w.get("severity", 1) for w in wounds)
    label = {1: "lightly wounded", 2: "seriously wounded", 3: "grievously wounded"}.get(worst, "wounded")
    return f"{label} ({'; '.join(w.get('desc', 'a wound') for w in wounds[-3:])})"


def dossier(store: Store, c: dict) -> dict:
    """The agent's view of itself (private, includes its own secrets)."""
    p = c.get("persona", {})
    return {
        "id": c["id"], "name": c["name"], "titles": p.get("titles", []),
        "house": house_name(store, c.get("house_id")), "age": age_of(store, c),
        "gender": p.get("gender", ""), "culture": p.get("culture", ""), "faith": p.get("faith", ""),
        "appearance": p.get("appearance", ""), "traits": p.get("traits", []),
        "values": p.get("values", []), "fears": p.get("fears", []), "voice": p.get("voice", ""),
        "history": p.get("summary", ""), "secrets": p.get("secrets", []),
        "possessions": p.get("possessions", []), "skills": c.get("attrs", {}).get("skills", {}),
        "health": health_line(c),
    }


def public_card(store: Store, c: dict, viewer: str | None) -> dict:
    """How ``viewer`` sees ``c``: public identity plus the viewer's own feelings."""
    p = c.get("persona", {})
    card = {"id": c["id"], "name": c["name"], "titles": p.get("titles", [])[:3],
            "house": house_name(store, c.get("house_id")), "age": age_of(store, c),
            "appearance": p.get("appearance", "")[:240], "condition": health_line(c)}
    if viewer and viewer != c["id"]:
        rel = store.relationship(viewer, c["id"])
        card["your_relation"] = {k: rel[k] for k in ("kind", "opinion", "trust", "fear", "respect")}
    return card


def goals_view(store: Store, owner: str) -> list[str]:
    return [f"[{g['level']}] {g['text']}" for g in store.goals(owner)][:8]


def scene_view(store: Store, scene: dict | None) -> dict:
    now = store.now
    d = clock.from_t(now)
    out = {"date": clock.format_date(now), "time_of_day": clock.time_of_day(d.hour),
           "season": store.meta_get("season", "unknown")}
    if scene:
        loc = store.location(scene.get("location_id")) or {}
        out.update({"place": where(store, scene.get("location_id"), scene.get("sublocation")),
                    "location_id": scene.get("location_id"),
                    "region": loc.get("region"),
                    "place_description": (loc.get("data") or {}).get("description", ""),
                    "situation": scene.get("situation", "")})
    return out


def terms_for(store: Store, ids: list[str], extra: list[str] | None = None) -> list[str]:
    terms = list(extra or [])
    for cid in ids:
        c = store.character(cid)
        if c:
            terms.append(c["name"])
    return terms


def memory_view(store: Store, owner: str, terms: list[str], k: int = 8) -> list[str]:
    return [f"({clock.short_date(m['t'])}) {m['text']}" for m in retrieve(store, owner, terms, k)]


def belief_view(store: Store, holder: str, terms: list[str], k: int = 10) -> list[str]:
    out = []
    for b in relevant_beliefs(store, holder, terms, k):
        conf = "" if b["confidence"] >= 0.9 else f" (uncertain, {b['confidence']:.0%})"
        out.append(f"{b['subject']}: {b['content']}{conf} [as of {clock.short_date(b['learned_t'])}, {b['source']}]")
    return out


def relationships_view(store: Store, owner: str, k: int = 8) -> list[str]:
    out = []
    for r in store.relationships_of(owner)[:k]:
        other = store.character(r["b"])
        if not other:
            continue
        state = "" if other["alive"] else " (believed dead)" if _believes_dead(store, owner, other) else ""
        out.append(f"{other['name']} [{r['b']}]: {r['kind']}; opinion {r['opinion']:+d}, trust {r['trust']:+d}"
                   f"{', fear ' + str(r['fear']) if r['fear'] else ''}{state}"
                   f"{' — ' + r['notes'][-160:] if r['notes'] else ''}")
    return out


def _believes_dead(store: Store, owner: str, other: dict) -> bool:
    hits = store.search_beliefs(owner, [other["name"], "dead", "died", "killed", "slain"], limit=5)
    return any(other["name"].split()[0].lower() in (b["subject"] + b["content"]).lower()
               and any(w in b["content"].lower() for w in ("dead", "died", "killed", "slain")) for b in hits)


def recent_lines(store: Store, scene: dict | None, n: int = 10) -> list[str]:
    if not scene:
        return []
    rows = store.conn.execute(
        "SELECT summary FROM events WHERE id >= ? AND kind IN ('speech','action','player_action','hook','arrival','departure','outcome')"
        " ORDER BY id DESC LIMIT ?", (scene["id"], n)).fetchall()
    return [r[0] for r in reversed(rows)]


def known_locations(store: Store, limit: int = 30) -> list[str]:
    return [f"{loc['id']}: {loc['name']} ({loc.get('region') or '?'})" for loc in store.locations()[:limit]]

"""Area definitions and weekly issue assembly.

Trailhead sends one weekly issue per *area* (a region a subscriber follows).
Each area is a fixed entry in ``AREAS`` (slug, display name, and a representative
lat/lon used for the weather forecast). Areas are intentionally a small code
constant rather than a DB table — there are two of them and they change about
never; the editable per-area bits (the optional AllTrails list URL) live in
config_kv via ``db.area_list_url_*``.

An area issue is just a ``newsletters`` row whose ``kind`` is the area slug, and
its content is plain ``articles`` rows distinguished by ``source``:
  • ``weather`` — the frozen 7-day forecast (refreshed on every rebuild)
  • ``trail``   — one random easy/moderate curated trail (picked once, sticky)
  • ``trip``    — operator-added custom trip ads (manual URL adds)
This reuse means the existing article list / email renderer / preview all work
on area issues with no special-casing.
"""
from secdigest import db, weather, komoot

# Representative coordinates per area for the NWS forecast. Utah → the Wasatch
# Front near Salt Lake City; the Poconos → the Pocono Mountains in NE PA.
AREAS = [
    {"slug": "utah",    "name": "Utah",         "lat": 40.7608, "lon": -111.8910},
    {"slug": "poconos", "name": "The Poconos",  "lat": 41.1220, "lon": -75.3646},
]

_BY_SLUG = {a["slug"]: a for a in AREAS}


def area_slugs() -> list[str]:
    return [a["slug"] for a in AREAS]


def area_by_slug(slug: str) -> dict | None:
    return _BY_SLUG.get(slug)


def area_name(slug: str) -> str:
    a = _BY_SLUG.get(slug)
    return a["name"] if a else slug


def is_area(kind: str) -> bool:
    """True when a newsletter ``kind`` is an area slug (vs daily/weekly/monthly)."""
    return kind in _BY_SLUG


def refresh_weather(newsletter_id: int, area: dict):
    """Drop the previous weather card and insert a freshly fetched one at the
    top of the issue. Safe to call repeatedly (that's the whole point of a
    rebuild)."""
    db.article_delete_by_source(newsletter_id, "weather")
    summary = weather.forecast_7day(area)
    aid = db.article_insert(
        newsletter_id=newsletter_id, hn_id=None,
        title=f"7-day forecast — {area['name']}",
        url="", hn_score=0, hn_comments=0,
        relevance_score=10.0, relevance_reason="weather",
        position=0, included=1, source="weather", source_name="Weather",
    )
    db.article_update(aid, summary=summary)


def refresh_area_trails(area_slug: str, max_distance_m: int = 20000) -> int:
    """Pull fresh easy/moderate hikes for the area from komoot and insert any
    not already in the trails table. Returns the number of new trails added.

    Best-effort: a komoot failure returns 0 and leaves the existing (curated +
    previously imported) rows untouched, so the weekly build always has a pool
    to pick from. This is what keeps the weekly pick fresh/unique over time."""
    area = area_by_slug(area_slug)
    if not area:
        return 0
    added = 0
    for row in komoot.discover_hikes(area, max_distance_m=max_distance_m):
        if db.trail_exists(area_slug, row["alltrails_url"]):
            continue
        if db.trail_create(area_slug, row["name"], row["alltrails_url"],
                            row["difficulty"], row["length_mi"], row["blurb"]):
            added += 1
    return added


def pick_trail(newsletter_id: int, area: dict) -> bool:
    """Insert one random easy/moderate trail card for the area, avoiding last
    week's pick when possible. Returns False when the area has no eligible
    trails (neither curated nor imported)."""
    slug = area["slug"]
    last_raw = db.cfg_get(f"last_trail_{slug}")
    last_id = int(last_raw) if last_raw.isdigit() else None
    t = db.trail_random(slug, exclude_id=last_id)
    if not t:
        return False
    db.cfg_set(f"last_trail_{slug}", str(t["id"]))
    stats = []
    if t.get("difficulty"):
        stats.append(t["difficulty"])
    if t.get("length_mi"):
        stats.append(f"{t['length_mi']:g} mi")
    suffix = f" ({', '.join(stats)})" if stats else ""
    summary = f"{t.get('blurb', '').strip()}{suffix}".strip()
    aid = db.article_insert(
        newsletter_id=newsletter_id, hn_id=None,
        title=f"Trail of the week: {t['name']}",
        url=t.get("alltrails_url") or "",
        hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="trail pick",
        position=1, included=1, source="trail", source_name="Trail",
    )
    db.article_update(aid, summary=summary)
    return True


def reroll_trail(newsletter_id: int, area: dict) -> bool:
    """Replace the current trail card with a fresh random pick."""
    db.article_delete_by_source(newsletter_id, "trail")
    return pick_trail(newsletter_id, area)


def build_area_issue(area_slug: str, week_start: str, week_end: str) -> dict | None:
    """Create (or fetch) the area's issue for the given week, refresh its
    weather card, and ensure it has a trail card. Trip ads are added separately
    by the operator. Returns the newsletter row, or None for an unknown area."""
    area = area_by_slug(area_slug)
    if not area:
        return None
    nl = db.newsletter_get_or_create(
        week_start, kind=area_slug, period_start=week_start, period_end=week_end
    )
    refresh_weather(nl["id"], area)
    if not db.article_has_source(nl["id"], "trail"):
        pick_trail(nl["id"], area)
    return nl

"""Discover hikes near an area via komoot's public discover API.

Why komoot and not AllTrails: AllTrails hard-blocks server-side requests
(DataDome CAPTCHA). Komoot does not — its discover endpoint answers plain JSON
to an anonymous client, which lets us refresh each area's trail pool weekly.

Endpoint (reverse-engineered from the public web discover page — it's the same
call komoot.com fires; it is *undocumented* and could change without notice):

    GET https://api.komoot.de/v007/discover_tours/
        ?sport=hike&lat={lat}&lng={lng}&max_distance={meters}&limit={n}&page={p}

Returns ``_embedded.items[]`` where each item has: ``name``, ``distance`` (m),
``elevation_up`` (m), ``duration`` (s), ``difficulty.grade`` (easy/moderate/
difficult), ``rating_score``/``rating_count``, and ``share_url``
(https://www.komoot.com/smarttour/{id}/...). We normalise those into the shape
``db.trail_create`` expects and let the caller filter/dedupe/insert.

Network hygiene mirrors ``rss.py``/``weather.py``: SSRF-safe URL check, short
timeout, explicit User-Agent. Failures degrade to an empty list so a komoot
outage never blocks the weekly build (the curated rows remain as fallback).
"""
import httpx

from secdigest.web.security import is_safe_external_url

_API = "https://api.komoot.de/v007/discover_tours/"
_HEADERS = {
    "User-Agent": "Trailhead/1.0 (hiking newsletter; contact via website)",
    "Accept": "application/hal+json",
}
# komoot grades → our two buckets. We only want approachable trails for the
# weekly "trail of the week" pick, so 'difficult' is dropped.
_GRADE_MAP = {"easy": "easy", "moderate": "moderate"}
_METERS_PER_MILE = 1609.344


def _normalise(item: dict) -> dict | None:
    """Map one komoot discover item to a db.trail row dict, or None if it isn't
    an easy/moderate hike with a usable link."""
    grade = (((item.get("difficulty") or {}).get("grade")) or "").lower()
    difficulty = _GRADE_MAP.get(grade)
    if not difficulty:
        return None
    name = (item.get("name") or "").strip()
    url = (item.get("share_url") or "").strip()
    if not name or not url:
        return None
    miles = round((item.get("distance") or 0) / _METERS_PER_MILE, 1)
    elev_up = item.get("elevation_up")
    rating = item.get("rating_score")
    rating_n = item.get("rating_count") or 0
    # Synthesise a short stats blurb — komoot's discover payload has no prose
    # description, so we describe the route from its numbers.
    bits = []
    if elev_up:
        bits.append(f"{round(elev_up)} m gain")
    if rating and rating_n:
        bits.append(f"{rating:.1f}★ ({rating_n})")
    blurb = " · ".join(bits) + " · via komoot" if bits else "via komoot"
    return {
        "area": "",  # filled in by caller
        "name": name,
        "alltrails_url": url,  # generic "trail URL" column — holds the komoot link
        "difficulty": difficulty,
        "length_mi": miles,
        "blurb": blurb,
    }


def discover_hikes(area: dict, max_distance_m: int = 20000,
                   limit: int = 24, max_pages: int = 3) -> list[dict]:
    """Return normalised easy/moderate hikes near ``area`` (dict with lat/lon).
    Empty list on any error. ``area['slug']`` is stamped onto each row."""
    lat, lon = area.get("lat"), area.get("lon")
    slug = area.get("slug", "")
    if lat is None or lon is None:
        return []
    out: list[dict] = []
    seen_urls: set[str] = set()
    try:
        with httpx.Client(follow_redirects=True, timeout=15, headers=_HEADERS) as client:
            if not is_safe_external_url(_API):
                return []
            for page in range(max_pages):
                resp = client.get(_API, params={
                    "sport": "hike",
                    "lat": float(lat),
                    "lng": float(lon),
                    "max_distance": int(max_distance_m),
                    "limit": int(limit),
                    "page": page,
                })
                resp.raise_for_status()
                body = resp.json()
                items = (body.get("_embedded") or {}).get("items", []) or []
                if not items:
                    break
                for it in items:
                    row = _normalise(it)
                    if row and row["alltrails_url"] not in seen_urls:
                        row["area"] = slug
                        seen_urls.add(row["alltrails_url"])
                        out.append(row)
                page_info = body.get("page") or {}
                if page + 1 >= (page_info.get("totalPages") or 1):
                    break
    except Exception as e:
        print(f"[komoot] discover failed for {slug}: {e}")
        return []
    return out

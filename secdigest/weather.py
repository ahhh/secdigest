"""7-day weather forecast for an area, via the US National Weather Service.

Why NWS (``api.weather.gov``): it's free, needs no API key, and covers the
whole US — both Trailhead areas (Utah, the Poconos) are domestic, so it's a
clean fit. The flow is two hops:

    GET /points/{lat},{lon}      → properties.forecast (a per-grid URL)
    GET {that forecast URL}      → properties.periods  (day/night periods)

We fetch this at *issue-build time* and freeze the result into the weather
"article" so a weather outage at send time can never block the email.

Network hygiene mirrors ``rss.py``: SSRF-safe URL checks, a short timeout, and
an explicit User-Agent (NWS rejects requests without one). The summary is
returned as a single compact line because the email renderer HTML-escapes
article summaries — multi-line text wouldn't keep its line breaks.
"""
import httpx

from secdigest.web.security import is_safe_external_url

# NWS requires a descriptive User-Agent (ideally with contact info) or it
# returns 403. This mirrors the polite UA the RSS fetcher already uses.
_HEADERS = {
    "User-Agent": "Trailhead/1.0 (hiking newsletter; contact via website)",
    "Accept": "application/geo+json",
}
_POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"


def _fallback(area: dict) -> str:
    return (f"Forecast for {area.get('name', 'this area')} is temporarily "
            f"unavailable — check your local conditions before heading out.")


def forecast_7day(area: dict) -> str:
    """Return a one-line 7-day forecast summary for ``area`` (a dict with
    ``name``/``lat``/``lon``). Falls back to a friendly placeholder string on
    any network/parse error, so callers never have to handle exceptions."""
    lat, lon = area.get("lat"), area.get("lon")
    if lat is None or lon is None:
        return _fallback(area)
    points_url = _POINTS_URL.format(lat=float(lat), lon=float(lon))
    try:
        # follow_redirects=True: the points endpoint occasionally 301s to a
        # canonical host. The host is NWS (not user-supplied), and we still
        # validate the forecast URL pulled from the response below.
        with httpx.Client(follow_redirects=True, timeout=12, headers=_HEADERS) as client:
            if not is_safe_external_url(points_url):
                return _fallback(area)
            meta = client.get(points_url)
            meta.raise_for_status()
            forecast_url = (meta.json().get("properties", {}) or {}).get("forecast", "")
            if not forecast_url or not is_safe_external_url(forecast_url):
                return _fallback(area)
            fc = client.get(forecast_url)
            fc.raise_for_status()
            periods = (fc.json().get("properties", {}) or {}).get("periods", []) or []
    except Exception as e:
        print(f"[weather] forecast fetch failed for {area.get('slug')}: {e}")
        return _fallback(area)

    # Use the daytime periods (one per day) for a compact 7-day readout.
    parts = []
    for p in periods:
        if not p.get("isDaytime"):
            continue
        name = (p.get("name") or "").strip()
        temp = p.get("temperature")
        unit = p.get("temperatureUnit", "F")
        short = (p.get("shortForecast") or "").strip()
        if not name:
            continue
        bit = name
        if temp is not None:
            bit += f" {temp}°{unit}"
        if short:
            bit += f", {short.lower()}"
        parts.append(bit)
        if len(parts) >= 7:
            break

    if not parts:
        return _fallback(area)
    return "  ·  ".join(parts)

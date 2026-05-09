"""Defence-in-depth tests for the JS-context XSS via the date_str path param.

Two layers protect against CVE-style XSS where a crafted /day/<...> URL would
break out of the JavaScript string literal in day.html / digest.html:

  • Templates use Jinja's `|tojson` filter when emitting date_str into a JS
    context — this alone makes the value safe regardless of content.
  • Routes call _validate_date(date_str) which 404s on anything that isn't a
    real ISO YYYY-MM-DD.

These tests assert both layers are present and behave correctly.
"""
import re

import pytest


# ── Route-level validator ───────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "2026-13-40",            # impossible date
    "not-a-date",
    "2026/05/04",            # wrong separator
    "20260504",              # missing dashes
    "'+alert(1)+'",          # the actual XSS payload
    "<script>x</script>",
    "../../etc/passwd",
])
async def test_day_view_rejects_malformed_date(admin_client, bad):
    """Route boundary should 404 anything that isn't ISO YYYY-MM-DD."""
    from urllib.parse import quote
    r = await admin_client.get(f"/day/{quote(bad, safe='')}")
    assert r.status_code == 404, \
        f"malformed date {bad!r} returned {r.status_code} instead of 404"


@pytest.mark.parametrize("bad", [
    "2026-13-40",
    "'+alert(1)+'",
    "<script>",
])
async def test_week_and_month_view_reject_malformed_date(admin_client, bad):
    from urllib.parse import quote
    encoded = quote(bad, safe="")
    r = await admin_client.get(f"/week/{encoded}")
    assert r.status_code == 404
    r = await admin_client.get(f"/month/{encoded}")
    assert r.status_code == 404


async def test_day_view_accepts_well_formed_date(admin_client):
    """Sanity: real dates must still render — 200, not 404."""
    r = await admin_client.get("/day/2026-05-04")
    assert r.status_code == 200


# ── Template-level escape (defence in depth) ─────────────────────────────────

def test_day_template_uses_tojson_for_date_str():
    """If a future edit reverts the |tojson filter, JS-context XSS is back —
    catch the regression at template-parse time."""
    from pathlib import Path
    day_html = (Path(__file__).resolve().parents[1]
                / "secdigest" / "web" / "templates" / "day.html").read_text()
    # Old vulnerable form: const dateStr = '{{ date_str }}';
    assert "const dateStr = '{{ date_str }}'" not in day_html, \
        "day.html dropped date_str into a JS string literal without |tojson"
    assert re.search(r"const dateStr = \{\{\s*date_str\s*\|\s*tojson\s*\}\}", day_html), \
        "day.html no longer uses |tojson on date_str"


def test_digest_template_uses_tojson_for_date_str():
    from pathlib import Path
    digest_html = (Path(__file__).resolve().parents[1]
                   / "secdigest" / "web" / "templates" / "digest.html").read_text()
    assert "const dateStr = '{{ date_str }}'" not in digest_html
    assert re.search(r"const dateStr = \{\{\s*date_str\s*\|\s*tojson\s*\}\}", digest_html)

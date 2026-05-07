"""Tests for the public landing/subscribe/confirm/unsubscribe site.

Uses the shared `stub_smtp` and `reset_rate_limits` fixtures from conftest.

Covers:
  • Landing page renders the form with all three cadences and the cyber chrome
  • subscribe → creates a pending row + sends a confirmation email
  • Honeypot silently blocks bot signups
  • Invalid email rejected (400)
  • Confirm token activates and is single-use
  • Re-subscribe with already-confirmed email reports already-subscribed
  • Unsubscribe link flips active=0
  • Per-IP rate limits trigger on subscribe (5/hr) and unsubscribe (10/hr)
  • Admin-side subscriber_create marks confirmed=1
"""
import re
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from secdigest import db


@pytest.fixture
def public_base_url(monkeypatch):
    """Point confirm/unsubscribe links at a stable URL the tests can match."""
    from secdigest import config
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://public.example")


async def _get(app, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path)


async def _post(app, path: str, data: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(path, data=data)


# ── Templates parse ──────────────────────────────────────────────────────────

def test_all_public_templates_parse():
    """Catch syntax errors in landing / thanks / confirmed / unsubscribed before
    they blow up at request time."""
    from jinja2 import Environment, FileSystemLoader
    template_dir = (Path(__file__).resolve().parents[1]
                    / "secdigest" / "public" / "templates")
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    for t in ("landing.html", "thanks.html", "confirmed.html", "unsubscribed.html"):
        env.get_template(t)


# ── Landing page ─────────────────────────────────────────────────────────────

async def test_landing_renders_form(tmp_db, stub_smtp, reset_rate_limits):
    from secdigest.public.app import app
    r = await _get(app, "/")
    assert r.status_code == 200
    body = r.text
    assert "subscribe" in body.lower()
    assert 'name="cadence"' in body
    for c in ("daily", "weekly", "monthly"):
        assert f'value="{c}"' in body, f"cadence option {c!r} missing"


async def test_landing_carries_cyber_chrome(tmp_db, stub_smtp, reset_rate_limits):
    """The cyber-noir styling depends on a few specific markup hooks (scanlines
    layer, grid overlay, terminal panel). If someone strips them out, the page
    goes from Blade Runner to Bootstrap — test that the hooks stay put."""
    from secdigest.public.app import app
    r = await _get(app, "/")
    body = r.text
    for hook in ('class="scanlines"', 'class="grid-bg"',
                 'class="terminal"', 'class="terminal-bar"'):
        assert hook in body, f"missing styling hook: {hook}"


# ── Subscribe → pending + confirmation email ────────────────────────────────

async def test_subscribe_creates_pending_row_and_sends_email(
        tmp_db, stub_smtp, public_base_url, reset_rate_limits):
    from secdigest.public.app import app
    r = await _post(app, "/subscribe",
                    {"email": "alice@test.example", "cadence": "weekly", "website": ""})
    assert r.status_code == 200
    # The cyberpunk theme uses lowercase copy throughout; match case-insensitively
    # so a future style swap doesn't quietly break the test.
    assert "check your inbox" in r.text.lower()

    sub = db.subscriber_get_by_email("alice@test.example")
    assert sub is not None
    assert sub["confirmed"] == 0
    assert sub["active"] == 0
    assert sub["cadence"] == "weekly"
    assert sub["confirm_token"]

    assert len(stub_smtp) == 1
    msg = stub_smtp[0]
    assert msg["to"] == "alice@test.example"
    assert re.search(r"http://public\.example/confirm/[\w-]+", msg["body"])


async def test_honeypot_blocks_bot_signup(tmp_db, stub_smtp, reset_rate_limits):
    from secdigest.public.app import app
    r = await _post(app, "/subscribe",
                    {"email": "bot@test.example", "cadence": "daily",
                     "website": "http://buy-pills.example"})
    # Bots get a 200 indistinguishable from a real success
    assert r.status_code == 200
    assert db.subscriber_get_by_email("bot@test.example") is None
    assert stub_smtp == []


async def test_subscribe_rejects_invalid_email(tmp_db, stub_smtp, reset_rate_limits):
    from secdigest.public.app import app
    r = await _post(app, "/subscribe",
                    {"email": "not-an-email", "cadence": "daily", "website": ""})
    assert r.status_code == 400
    assert stub_smtp == []


async def test_subscribe_clamps_unknown_cadence_to_daily(
        tmp_db, stub_smtp, public_base_url, reset_rate_limits):
    from secdigest.public.app import app
    r = await _post(app, "/subscribe",
                    {"email": "carol@test.example", "cadence": "yearly", "website": ""})
    assert r.status_code == 200
    sub = db.subscriber_get_by_email("carol@test.example")
    assert sub["cadence"] == "daily"


# ── Confirm flow ─────────────────────────────────────────────────────────────

async def test_confirm_activates_row_and_is_single_use(
        tmp_db, stub_smtp, public_base_url, reset_rate_limits):
    from secdigest.public.app import app
    await _post(app, "/subscribe",
                {"email": "dave@test.example", "cadence": "monthly", "website": ""})
    confirm_url = re.search(r"http://public\.example/confirm/[\w-]+",
                             stub_smtp[0]["body"]).group(0)
    token = confirm_url.rsplit("/", 1)[-1]

    r = await _get(app, f"/confirm/{token}")
    assert r.status_code == 200
    assert "you're in" in r.text.lower()
    sub = db.subscriber_get_by_email("dave@test.example")
    assert sub["confirmed"] == 1
    assert sub["active"] == 1
    assert sub["confirm_token"] is None

    r = await _get(app, f"/confirm/{token}")
    assert r.status_code == 200
    assert "dead" in r.text.lower() or "expired" in r.text.lower()


async def test_resubscribe_already_confirmed_says_already_subscribed(
        tmp_db, stub_smtp, public_base_url, reset_rate_limits):
    from secdigest.public.app import app
    await _post(app, "/subscribe",
                {"email": "eve@test.example", "cadence": "daily", "website": ""})
    token = re.search(r"/confirm/([\w-]+)", stub_smtp[0]["body"]).group(1)
    await _get(app, f"/confirm/{token}")

    stub_smtp.clear()
    r = await _post(app, "/subscribe",
                    {"email": "eve@test.example", "cadence": "weekly", "website": ""})
    assert r.status_code == 200
    assert "already subscribed" in r.text
    assert stub_smtp == []


# ── Unsubscribe ──────────────────────────────────────────────────────────────

async def test_unsubscribe_via_token_flips_active_zero(
        tmp_db, stub_smtp, public_base_url, reset_rate_limits):
    from secdigest.public.app import app
    await _post(app, "/subscribe",
                {"email": "frank@test.example", "cadence": "daily", "website": ""})
    token = re.search(r"/confirm/([\w-]+)", stub_smtp[0]["body"]).group(1)
    await _get(app, f"/confirm/{token}")
    sub = db.subscriber_get_by_email("frank@test.example")

    r = await _get(app, f"/unsubscribe/{sub['unsubscribe_token']}")
    assert r.status_code == 200
    assert "off the wire" in r.text.lower() or "unsubscribed" in r.text.lower()
    assert db.subscriber_get_by_email("frank@test.example")["active"] == 0


async def test_unsubscribe_unknown_token_is_safe(tmp_db, reset_rate_limits):
    from secdigest.public.app import app
    r = await _get(app, "/unsubscribe/totally-fake-uuid")
    assert r.status_code == 200
    assert "invalid" in r.text.lower() or "expired" in r.text.lower()


# ── Rate limiting ────────────────────────────────────────────────────────────

async def test_subscribe_rate_limit_429_after_5_attempts(
        tmp_db, stub_smtp, public_base_url, reset_rate_limits):
    from secdigest.public.app import app
    for i in range(5):
        r = await _post(app, "/subscribe",
                        {"email": f"rate{i}@test.example", "cadence": "daily", "website": ""})
        assert r.status_code == 200, f"attempt {i+1} status {r.status_code}"
    r = await _post(app, "/subscribe",
                    {"email": "rate-final@test.example", "cadence": "daily", "website": ""})
    assert r.status_code == 429


async def test_unsubscribe_rate_limit_429_after_10_attempts(tmp_db, reset_rate_limits):
    from secdigest.public.app import app
    for i in range(10):
        r = await _get(app, f"/unsubscribe/dummy-{i}")
        assert r.status_code == 200, f"attempt {i+1} status {r.status_code}"
    r = await _get(app, "/unsubscribe/dummy-final")
    assert r.status_code == 429


# ── Admin-side parity ────────────────────────────────────────────────────────

def test_admin_subscriber_create_marks_confirmed(tmp_db):
    """Admin trusts itself — adding a subscriber via the admin form should not
    leave them stuck in pending state."""
    sub = db.subscriber_create("admin-add@test.example", "")
    assert sub is not None
    assert sub["confirmed"] == 1
    assert sub["active"] == 1

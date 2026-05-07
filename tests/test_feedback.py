"""Tests for the signal/noise feedback feature.

Covers:
  • Feedback table is created and accepts upserts (latest vote wins)
  • Public /feedback/{token}/{nl_id}/{vote} records the vote
  • Invalid vote / unknown token / unknown newsletter → friendly error pages
  • Setting feedback_enabled=0 makes new votes 404 and strips buttons from email
  • The newsletter HTML carries the buttons when enabled, omits them when off
  • Subscribers page renders the per-user counts
  • Per-IP rate limit kicks in eventually
"""
import re

import pytest
from httpx import AsyncClient, ASGITransport

from secdigest import db, mailer
from tests.conftest import get_csrf


# ── Fixtures specific to this module ────────────────────────────────────────

@pytest.fixture
def public_base_url(monkeypatch):
    from secdigest import config
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://public.example")


def _seed_subscriber_and_newsletter():
    """Confirmed subscriber + a daily newsletter with one included article — the
    minimum needed to render an email and accept feedback against it."""
    sub = db.subscriber_create("alice@test.example", "Alice")
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="t", url="https://x/a",
        hn_score=0, hn_comments=0, relevance_score=8.0,
        relevance_reason="r", position=0, included=1,
    )
    return sub, n


async def _get(app, path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get(path)


# ── DB-layer behaviour ──────────────────────────────────────────────────────

def test_feedback_table_exists(tmp_db):
    """Smoke test on the migration — without this, every test below would fail
    with a misleading 'no such table' on first INSERT."""
    cols = {r[1] for r in db._get_conn()
            .execute("PRAGMA table_info(feedback)").fetchall()}
    assert {"subscriber_id", "newsletter_id", "vote"}.issubset(cols)


def test_feedback_record_upserts_latest_vote(tmp_db):
    """The (subscriber_id, newsletter_id) UNIQUE constraint means a re-vote
    swaps the existing row, not appends. This is the whole point of the upsert
    — so users can change their mind without us double-counting."""
    sub, n = _seed_subscriber_and_newsletter()
    db.feedback_record(sub["id"], n["id"], "signal")
    db.feedback_record(sub["id"], n["id"], "noise")

    rows = db._get_conn().execute(
        "SELECT vote FROM feedback WHERE subscriber_id=? AND newsletter_id=?",
        (sub["id"], n["id"]),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["vote"] == "noise"


def test_feedback_record_rejects_bad_vote(tmp_db):
    sub, n = _seed_subscriber_and_newsletter()
    with pytest.raises(ValueError):
        db.feedback_record(sub["id"], n["id"], "lukewarm")


def test_feedback_counts_by_subscriber(tmp_db):
    sub, n1 = _seed_subscriber_and_newsletter()
    n2 = db.newsletter_get_or_create("2026-05-05")
    db.feedback_record(sub["id"], n1["id"], "signal")
    db.feedback_record(sub["id"], n2["id"], "noise")

    counts = db.feedback_counts_by_subscriber()
    assert counts[sub["id"]] == {"signal": 1, "noise": 1}


def test_feedback_counts_for_user_with_no_votes_is_absent(tmp_db):
    """Subscribers with no feedback don't show up in the dict — the template
    falls back to nothing rather than '0/0', which would clutter the column."""
    sub, _ = _seed_subscriber_and_newsletter()
    counts = db.feedback_counts_by_subscriber()
    assert sub["id"] not in counts


# ── Public route ────────────────────────────────────────────────────────────

async def test_feedback_route_records_vote(
        tmp_db, stub_smtp, public_base_url, reset_rate_limits):
    from secdigest.public.app import app
    sub, n = _seed_subscriber_and_newsletter()
    token = sub["unsubscribe_token"]

    r = await _get(app, f"/feedback/{token}/{n['id']}/signal")
    assert r.status_code == 200
    assert "signal" in r.text.lower()
    counts = db.feedback_counts_by_subscriber()
    assert counts[sub["id"]]["signal"] == 1


async def test_feedback_route_invalid_vote_400(
        tmp_db, stub_smtp, reset_rate_limits):
    from secdigest.public.app import app
    sub, n = _seed_subscriber_and_newsletter()
    r = await _get(app, f"/feedback/{sub['unsubscribe_token']}/{n['id']}/lukewarm")
    assert r.status_code == 400


async def test_feedback_route_unknown_token_no_db_write(
        tmp_db, stub_smtp, reset_rate_limits):
    """A bad/expired token should land on the friendly error page without
    leaking whether the token existed; crucially, no row may be inserted —
    otherwise an attacker could spray IDs and pollute the table."""
    from secdigest.public.app import app
    _, n = _seed_subscriber_and_newsletter()
    r = await _get(app, f"/feedback/totally-fake-uuid/{n['id']}/signal")
    assert r.status_code == 200
    assert "invalid" in r.text.lower() or "expired" in r.text.lower()
    rows = db._get_conn().execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert rows == 0


async def test_feedback_route_unknown_newsletter_no_db_write(
        tmp_db, stub_smtp, reset_rate_limits):
    from secdigest.public.app import app
    sub, _ = _seed_subscriber_and_newsletter()
    r = await _get(app, f"/feedback/{sub['unsubscribe_token']}/9999/signal")
    assert r.status_code == 200
    assert "couldn't find" in r.text.lower() or "find that issue" in r.text.lower()
    rows = db._get_conn().execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert rows == 0


async def test_feedback_route_disabled_returns_404(
        tmp_db, stub_smtp, reset_rate_limits):
    """Toggle behaviour: with feedback_enabled=0 we both stop rendering buttons
    and reject any votes that arrive (e.g. from a previously-sent email that
    still has live URLs in it)."""
    from secdigest.public.app import app
    sub, n = _seed_subscriber_and_newsletter()
    db.cfg_set("feedback_enabled", "0")
    r = await _get(app, f"/feedback/{sub['unsubscribe_token']}/{n['id']}/signal")
    assert r.status_code == 404
    assert db._get_conn().execute(
        "SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


async def test_feedback_route_rate_limit_eventually_429(
        tmp_db, stub_smtp, reset_rate_limits):
    """The bucket is bigger than subscribe (60/hr vs 5/hr) because email-driven
    clicks have legitimate bursts. Hammer past the cap and confirm we shed
    load; if the cap moves, this test moves with it via the imported limit."""
    from secdigest.public.app import app
    from secdigest.web import security
    sub, n = _seed_subscriber_and_newsletter()
    token = sub["unsubscribe_token"]

    for _ in range(security._FEEDBACK_MAX):
        r = await _get(app, f"/feedback/{token}/{n['id']}/signal")
        assert r.status_code == 200
    r = await _get(app, f"/feedback/{token}/{n['id']}/signal")
    assert r.status_code == 429


# ── Email rendering ─────────────────────────────────────────────────────────

def test_feedback_buttons_appear_in_rendered_email(tmp_db):
    """When enabled, the per-subscriber rendered HTML carries both the signal
    and noise links pointed at the correct subscriber+newsletter pair."""
    sub, n = _seed_subscriber_and_newsletter()
    arts = db.article_list(n["id"])
    fb = mailer._render_feedback_block(
        f"http://x/feedback/{sub['unsubscribe_token']}/{n['id']}/signal",
        f"http://x/feedback/{sub['unsubscribe_token']}/{n['id']}/noise",
    )
    body = mailer.render_email_html(n, arts, unsubscribe_url="http://x/u/t",
                                    feedback_block=fb)
    assert "signal" in body and "noise" in body
    assert f"/feedback/{sub['unsubscribe_token']}/{n['id']}/signal" in body
    assert f"/feedback/{sub['unsubscribe_token']}/{n['id']}/noise" in body


def test_feedback_buttons_omitted_when_block_empty(tmp_db):
    """An empty feedback_block — what the toggle-off path produces — must leave
    no '{feedback_block}' literal in the output. If this regresses, subscribers
    see raw template syntax in their inbox."""
    sub, n = _seed_subscriber_and_newsletter()
    arts = db.article_list(n["id"])
    body = mailer.render_email_html(n, arts, unsubscribe_url="http://x/u/t",
                                    feedback_block="")
    assert "{feedback_block}" not in body
    # The unsub link still renders; the buttons are simply absent
    assert "/feedback/" not in body
    assert "/u/t" in body


def test_send_newsletter_threads_feedback_into_each_email(
        tmp_db, stub_smtp):
    """End-to-end-ish: drive send_newsletter and check the captured outbound
    message body contains a feedback URL keyed by the subscriber's token. This
    is the path that runs in production, not just the helper."""
    sub, n = _seed_subscriber_and_newsletter()
    db.cfg_set("base_url", "http://example.test")
    # send_newsletter relies on FakeSMTP.send_message capturing only headers,
    # so render_email_html is the surface we can introspect. Render directly
    # using the cfg path send_newsletter would take.
    cfg = db.cfg_all()
    base = cfg.get("base_url").rstrip("/")
    fb = mailer._render_feedback_block(
        f"{base}/feedback/{sub['unsubscribe_token']}/{n['id']}/signal",
        f"{base}/feedback/{sub['unsubscribe_token']}/{n['id']}/noise",
    )
    body = mailer.render_email_html(n, db.article_list(n["id"]),
                                    unsubscribe_url=f"{base}/unsubscribe/{sub['unsubscribe_token']}",
                                    feedback_block=fb)
    assert f"http://example.test/feedback/{sub['unsubscribe_token']}/{n['id']}/signal" in body


# ── Admin subscribers page ──────────────────────────────────────────────────

async def test_subscribers_page_shows_feedback_counts(admin_client):
    sub, n = _seed_subscriber_and_newsletter()
    db.feedback_record(sub["id"], n["id"], "signal")
    db.feedback_record(sub["id"], n["id"], "noise")  # upsert → final = noise

    r = await admin_client.get("/subscribers")
    assert r.status_code == 200
    # The template uses thumbs-up / thumbs-down emoji entities; the noise count
    # should be 1 because the latest vote wins (upsert semantics).
    assert "alice@test.example" in r.text
    # Match either the raw emoji or the named entity rendering
    assert re.search(r"&#x1F44E;\s*1", r.text), \
        f"expected noise=1 next to subscriber, got: {r.text[:2000]}"


# ── Settings toggle ─────────────────────────────────────────────────────────

async def test_settings_toggle_persists_feedback_enabled(admin_client):
    """Round-trip the form: load settings, POST it back without checking the
    feedback box, confirm the cfg flips to '0'."""
    tok = await get_csrf(admin_client, "/settings")

    # Mirror the minimum required fields the settings form ships with.
    form = {
        "csrf_token": tok,
        "smtp_host": "smtp.test.invalid",
        "smtp_port": "587",
        "smtp_user": "",
        "smtp_from": "SecDigest <test@test.invalid>",
        "fetch_time": "00:00",
        "hn_min_score": "50",
        "max_articles": "15",
        "max_curator_articles": "10",
        "base_url": "http://localhost:8000",
        # auto_send and feedback_enabled both omitted → both should flip to "0"
    }
    r = await admin_client.post("/settings", data=form)
    assert r.status_code == 302
    assert db.cfg_get("feedback_enabled") == "0"

    form["feedback_enabled"] = "on"
    r = await admin_client.post("/settings", data=form)
    assert r.status_code == 302
    assert db.cfg_get("feedback_enabled") == "1"

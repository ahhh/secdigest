"""Mailer smoke tests.

Focus areas:
  • render_email_html escapes hostile inputs in titles/summaries
  • Restricts URLs to http(s):// — javascript:, data: are filtered to empty href
  • kind-aware send paths (daily uses article_list, weekly/monthly use the join)
  • subscriber cadence filter — only matching subscribers receive a given send
  • The "SMTP not configured" guard fires when smtp_host is blank
"""
import pytest

from secdigest import db, mailer


def _seed_daily(date_str: str = "2026-05-04",
                title: str = "test article",
                summary: str = "test summary",
                url: str = "https://example.invalid/a") -> tuple[dict, int]:
    n = db.newsletter_get_or_create(date_str)
    aid = db.article_insert(
        newsletter_id=n["id"], hn_id=None, title=title,
        url=url, hn_score=0, hn_comments=0,
        relevance_score=8.0, relevance_reason="r", position=0, included=1,
    )
    db.article_update(aid, summary=summary)
    return n, aid


# ── render_email_html: escaping & url safety ────────────────────────────────

def test_render_escapes_html_in_title(tmp_db):
    n, _ = _seed_daily(title="<script>alert(1)</script>")
    articles = db.article_list(n["id"])
    html = mailer.render_email_html(n, articles)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_escapes_html_in_summary(tmp_db):
    n, _ = _seed_daily(summary='" onerror="alert(1)"')
    articles = db.article_list(n["id"])
    html = mailer.render_email_html(n, articles)
    # Quotes must be entity-encoded so they can't break out of the surrounding attr
    assert ' onerror="alert(1)"' not in html


@pytest.mark.parametrize("hostile_url", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox",
    "  ",
])
def test_render_strips_non_http_urls(tmp_db, hostile_url):
    n, _ = _seed_daily(url=hostile_url)
    articles = db.article_list(n["id"])
    html = mailer.render_email_html(n, articles)
    # No href to the hostile url should appear (we render an empty href instead)
    assert hostile_url not in html


def test_render_keeps_http_and_https_urls(tmp_db):
    n, _ = _seed_daily(url="https://example.invalid/safe")
    articles = db.article_list(n["id"])
    html = mailer.render_email_html(n, articles)
    assert "https://example.invalid/safe" in html


def test_render_unsubscribe_url_substituted(tmp_db):
    n, _ = _seed_daily()
    articles = db.article_list(n["id"])
    html = mailer.render_email_html(n, articles, unsubscribe_url="https://x/unsub/abc")
    assert "https://x/unsub/abc" in html
    assert "{unsubscribe_url}" not in html


# ── kind-aware send routing ──────────────────────────────────────────────────

def test_send_newsletter_daily_uses_article_list(tmp_db, stub_smtp):
    n, _ = _seed_daily()
    db.subscriber_create("a@test.invalid")
    ok, msg = mailer.send_newsletter("2026-05-04", kind="daily")
    assert ok, msg
    assert any("a@test.invalid" in m["to"] for m in stub_smtp)


def test_send_newsletter_weekly_uses_digest_join(tmp_db, stub_smtp):
    """Daily articles tied to a date inside the week should be reachable through
    the weekly digest's join — and the weekly send must NOT pull from articles
    directly bound to a daily newsletter."""
    n_daily, aid = _seed_daily("2026-05-04", title="daily-article")

    # Build a weekly digest manually and add the daily article via the join
    weekly = db.newsletter_get_or_create(
        "2026-05-04", kind="weekly",
        period_start="2026-05-04", period_end="2026-05-10",
    )
    db.digest_article_add(weekly["id"], aid, position=0, included=1)

    db.subscriber_create("weekly-only@test.invalid")
    sub = next(s for s in db.subscriber_list() if s["email"] == "weekly-only@test.invalid")
    db.subscriber_update(sub["id"], cadence="weekly")

    stub_smtp.clear()
    ok, msg = mailer.send_newsletter("2026-05-04", kind="weekly")
    assert ok, msg
    assert any("weekly-only@test.invalid" in m["to"] for m in stub_smtp)


def test_send_newsletter_no_matching_cadence_fails_gracefully(tmp_db, stub_smtp):
    """If no subscribers match the kind, the send should fail with a clear
    message rather than silently sending zero emails or crashing."""
    _seed_daily()
    db.subscriber_create("only-monthly@test.invalid")
    sub = next(s for s in db.subscriber_list() if s["email"] == "only-monthly@test.invalid")
    db.subscriber_update(sub["id"], cadence="monthly")

    ok, msg = mailer.send_newsletter("2026-05-04", kind="daily")
    assert ok is False
    assert "daily" in msg.lower() or "no active" in msg.lower()
    # And no SMTP traffic at all
    assert stub_smtp == []


def test_send_newsletter_filters_by_cadence_strictly(tmp_db, stub_smtp):
    """Three subscribers, one per cadence. A daily send must reach exactly the
    daily one — not "all active" minus the others."""
    _seed_daily()
    for email, cadence in [
        ("d@test.invalid", "daily"),
        ("w@test.invalid", "weekly"),
        ("m@test.invalid", "monthly"),
    ]:
        db.subscriber_create(email)
        sub = next(s for s in db.subscriber_list() if s["email"] == email)
        if cadence != "daily":
            db.subscriber_update(sub["id"], cadence=cadence)

    stub_smtp.clear()
    ok, _ = mailer.send_newsletter("2026-05-04", kind="daily")
    assert ok
    recipients = [m["to"] for m in stub_smtp]
    assert any("d@test.invalid" in r for r in recipients)
    assert not any("w@test.invalid" in r for r in recipients)
    assert not any("m@test.invalid" in r for r in recipients)


# ── Configuration guards ─────────────────────────────────────────────────────

def test_send_fails_when_smtp_host_blank(tmp_db, stub_smtp):
    db.cfg_set("smtp_host", "")  # override the seed
    _seed_daily()
    db.subscriber_create("a@test.invalid")
    ok, msg = mailer.send_newsletter("2026-05-04", kind="daily")
    assert ok is False
    assert "SMTP" in msg
    assert stub_smtp == []


def test_send_fails_when_from_is_example_dot_com(tmp_db, stub_smtp):
    """The placeholder From address must be replaced before sending — guards
    against accidentally shipping with the default config."""
    db.cfg_set("smtp_from", "SecDigest <noreply@example.com>")
    _seed_daily()
    db.subscriber_create("a@test.invalid")
    ok, msg = mailer.send_newsletter("2026-05-04", kind="daily")
    assert ok is False
    assert "example.com" in msg.lower() or "from" in msg.lower()


# ── Confirmation email (transactional path) ──────────────────────────────────

def test_send_confirmation_email_records_link(tmp_db, stub_smtp):
    ok, _ = mailer.send_confirmation_email(
        "newuser@test.invalid", "https://public.test/confirm/abc-123")
    assert ok
    msg = stub_smtp[0]
    assert msg["to"] == "newuser@test.invalid"
    assert msg["kind"] == "transactional"
    assert "https://public.test/confirm/abc-123" in msg["body"]


def test_send_confirmation_email_handles_smtp_failure_gracefully(monkeypatch, tmp_db):
    """If the underlying _smtp_send returns False, the helper must propagate that
    rather than swallowing the error — the caller may need to retry or surface
    the failure to the user."""
    monkeypatch.setattr(mailer, "_smtp_send",
                         lambda *a, **k: (False, "simulated failure"))
    ok, msg = mailer.send_confirmation_email("x@test.invalid", "https://x/c/1")
    assert ok is False
    assert "simulated failure" in msg

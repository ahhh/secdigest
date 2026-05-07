"""Tests for the per-template header_html feature.

Covers:
  • Schema: header_html column lands on email_templates
  • CRUD: email_template_create/update round-trip header_html
  • Toggle: newsletter_set_header / newsletter_get_header persist
  • Render order: header → voice → toc → articles in the rendered body
  • Header is wrapped in <tr><td> when admin writes a bare <h2>… snippet,
    but a power-user-supplied <tr> passes through unchanged
  • Toggle off → no header in output
  • Day preview route honours include_header=1 query param
  • Templates page form accepts header_html on create + save
"""
import pytest
from httpx import AsyncClient, ASGITransport

from secdigest import db, mailer
from tests.conftest import get_csrf


HEADER_SNIPPET = '<h2 style="color:#39ff14">Editor\'s intro</h2><p>This week we\'re focused on supply-chain attacks.</p>'


def _seed():
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="CVE in libfoo",
        url="https://x/a", hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="r", position=0, included=1,
    )
    return n


# ── Schema + CRUD ──────────────────────────────────────────────────────────

def test_header_html_column_exists(tmp_db):
    cols = {r[1] for r in db._get_conn()
            .execute("PRAGMA table_info(email_templates)").fetchall()}
    assert "header_html" in cols


def test_email_template_create_persists_header_html(tmp_db):
    t = db.email_template_create(
        name="custom", description="", subject="S — {date}",
        html="<html>{articles}{unsubscribe_url}</html>",
        article_html="<tr><td>{title}</td></tr>",
        header_html=HEADER_SNIPPET,
    )
    fetched = db.email_template_get(t["id"])
    assert fetched["header_html"] == HEADER_SNIPPET


def test_email_template_update_persists_header_html(tmp_db):
    t = db.email_template_create(
        name="custom", description="", subject="S",
        html="{articles}", article_html="{title}",
    )
    db.email_template_update(t["id"], header_html=HEADER_SNIPPET)
    assert db.email_template_get(t["id"])["header_html"] == HEADER_SNIPPET


def test_email_template_update_rejects_unknown_column(tmp_db):
    """Allowlist guard regression: a typo in a column name should raise, not
    silently no-op or punch through to the SQL layer."""
    t = db.email_template_create(name="x", description="", subject="s",
                                  html="{articles}", article_html="{title}")
    with pytest.raises(ValueError):
        db.email_template_update(t["id"], header_htmll="oops")  # noqa: typo


# ── Per-newsletter toggle ──────────────────────────────────────────────────

def test_newsletter_header_toggle_round_trips(tmp_db):
    n = _seed()
    assert db.newsletter_get_header(n["id"]) is False
    db.newsletter_set_header(n["id"], True)
    assert db.newsletter_get_header(n["id"]) is True
    db.newsletter_set_header(n["id"], False)
    assert db.newsletter_get_header(n["id"]) is False


# ── Header wrapping ────────────────────────────────────────────────────────

def test_wrap_header_html_wraps_bare_snippet():
    out = mailer._wrap_header_html("<h2>Hi</h2>")
    assert out.startswith("<tr><td")
    assert "<h2>Hi</h2>" in out
    assert out.rstrip().endswith("</td></tr>")


def test_wrap_header_html_passes_through_existing_tr():
    """Power users writing custom multi-cell banners pass <tr>-rooted HTML;
    the wrapper would corrupt their layout if it forced an extra <tr>."""
    raw = "<tr><td>left</td><td>right</td></tr>"
    assert mailer._wrap_header_html(raw) == raw


def test_wrap_header_html_empty_returns_empty():
    assert mailer._wrap_header_html("") == ""
    assert mailer._wrap_header_html("   \n  ") == ""


# ── Render order ───────────────────────────────────────────────────────────

def test_render_order_header_voice_toc_articles(tmp_db):
    """The fixed contract: header sits at the top of the content area, then
    voice, then TOC, then the article rows. If this drifts, the email
    layout silently changes for every subscriber on the next send."""
    n = _seed()
    arts = db.article_list(n["id"])
    body = mailer.render_email_html(
        n, arts,
        unsubscribe_url="http://x/u",
        include_toc=True,
        voice_block=mailer._render_voice_block("https://fake/audio.mp3", duration_sec=42),
        header_block=mailer._wrap_header_html(HEADER_SNIPPET),
    )
    hdr_idx = body.find("Editor's intro")
    voice_idx = body.find("Listen to this issue")
    toc_idx = body.find("Contents")
    art_idx = body.find("CVE in libfoo")
    for label, idx in (("header", hdr_idx), ("voice", voice_idx),
                       ("toc", toc_idx), ("article", art_idx)):
        assert idx >= 0, f"missing {label} block in rendered body"
    assert hdr_idx < voice_idx < toc_idx < art_idx, \
        f"order broken: hdr={hdr_idx} voice={voice_idx} toc={toc_idx} art={art_idx}"


def test_render_omits_header_when_block_empty(tmp_db):
    n = _seed()
    body = mailer.render_email_html(n, db.article_list(n["id"]),
                                    unsubscribe_url="http://x/u")
    assert "Editor's intro" not in body
    assert "{header_block}" not in body


# ── _header_block_for guards ───────────────────────────────────────────────

def test_header_block_for_empty_when_toggle_off(tmp_db):
    n = _seed()
    # Active template has a header, but the per-newsletter toggle is off
    tid = db.email_template_default()["id"]
    db.email_template_update(tid, header_html=HEADER_SNIPPET)
    db.newsletter_set_template_id(n["id"], tid)
    assert mailer._header_block_for(n["id"]) == ""


def test_header_block_for_renders_when_toggle_on_and_template_has_header(tmp_db):
    n = _seed()
    tid = db.email_template_default()["id"]
    db.email_template_update(tid, header_html=HEADER_SNIPPET)
    db.newsletter_set_template_id(n["id"], tid)
    db.newsletter_set_header(n["id"], True)
    block = mailer._header_block_for(n["id"])
    assert "Editor's intro" in block
    assert block.startswith("<tr>")


def test_header_block_for_empty_when_template_header_blank(tmp_db):
    """Toggle on, template has no header_html → empty output. Otherwise an
    admin who flipped the switch on a built-in would get an empty <tr><td>
    appearing in the email, which looks broken."""
    n = _seed()
    db.newsletter_set_header(n["id"], True)
    # Default template has header_html='' from seed
    assert mailer._header_block_for(n["id"]) == ""


# ── Preview route ──────────────────────────────────────────────────────────

async def test_day_preview_includes_header_when_query_set(admin_client):
    """Live builder behaviour: the include_header query param drives the
    iframe, not the persisted DB toggle, so admins can preview before saving."""
    n = _seed()
    tid = db.email_template_default()["id"]
    db.email_template_update(tid, header_html=HEADER_SNIPPET)
    db.newsletter_set_template_id(n["id"], tid)

    r_off = await admin_client.get(f"/day/2026-05-04/preview?template_id={tid}&include_header=0")
    r_on  = await admin_client.get(f"/day/2026-05-04/preview?template_id={tid}&include_header=1")
    assert r_off.status_code == 200 and r_on.status_code == 200
    assert "Editor's intro" not in r_off.text
    assert "Editor's intro" in r_on.text


# ── Templates page CRUD via HTTP ───────────────────────────────────────────

async def test_create_template_via_form_persists_header(admin_client):
    tok = await get_csrf(admin_client, "/email-templates")
    r = await admin_client.post("/email-templates/new", data={
        "csrf_token": tok,
        "name": "with-header",
        "description": "",
        "subject": "S — {date}",
        "html": "<html>{articles}{unsubscribe_url}</html>",
        "article_html": "<tr><td>{title}</td></tr>",
        "header_html": HEADER_SNIPPET,
    })
    assert r.status_code == 302
    rows = [t for t in db.email_template_list() if t["name"] == "with-header"]
    assert rows and rows[0]["header_html"] == HEADER_SNIPPET


async def test_save_template_via_form_persists_header(admin_client):
    t = db.email_template_create(name="x", description="", subject="s",
                                  html="{articles}", article_html="{title}")
    tok = await get_csrf(admin_client, "/email-templates")
    r = await admin_client.post(f"/email-templates/{t['id']}/save", data={
        "csrf_token": tok,
        "name": "x",
        "description": "",
        "subject": "s",
        "html": "{articles}",
        "article_html": "{title}",
        "header_html": HEADER_SNIPPET,
    })
    assert r.status_code == 302
    assert db.email_template_get(t["id"])["header_html"] == HEADER_SNIPPET


# ── Set-template route persists the toggle ─────────────────────────────────

async def test_day_set_template_persists_header_toggle(admin_client):
    _seed()
    tid = db.email_template_default()["id"]
    tok = await get_csrf(admin_client, "/day/2026-05-04")
    r = await admin_client.post("/day/2026-05-04/set-template", data={
        "csrf_token": tok,
        "template_id": str(tid),
        "subject": "S",
        "include_header": "1",
        # include_toc deliberately omitted — should flip to off
    })
    assert r.status_code == 302
    n = db.newsletter_get("2026-05-04")
    assert db.newsletter_get_header(n["id"]) is True
    assert db.newsletter_get_toc(n["id"]) is False

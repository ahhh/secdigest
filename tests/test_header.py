"""Tests for the global newsletter header.

Architecture: the header is a single global value stored in config_kv. The
per-newsletter "Include header" toggle decides whether to render it for a
given issue; the content itself never forks across templates.

Covers:
  • Schema: vestigial header_html column on email_templates is harmless
  • Migration: existing per-template header_html lifts into config_kv
  • CRUD: email_template_update REJECTS header_html (it's no longer a
    template field — admin UIs that try to write it would get silently
    cross-wired without this guard)
  • Toggle: newsletter_set_header / newsletter_get_header persist
  • Render order: header → voice → toc → articles
  • Wrapping: bare snippet wrapped, <tr>-rooted passes through, empty empty
  • Toggle off → no header in output regardless of cfg value
  • Day preview honours include_header=1, reading the GLOBAL value
  • Templates page POST /email-templates/header round-trips global value
  • set-template POST persists per-newsletter toggle
"""
import sqlite3

import pytest

from secdigest import db, mailer
from tests.conftest import get_csrf


HEADER_SNIPPET = (
    '<h2 style="color:#39ff14">Editor\'s intro</h2>'
    '<p>This week we\'re focused on supply-chain attacks.</p>'
)


def _seed():
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="CVE in libfoo",
        url="https://x/a", hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="r", position=0, included=1,
    )
    return n


# ── Schema + CRUD ──────────────────────────────────────────────────────────

def test_header_html_column_still_exists_but_is_dead(tmp_db):
    """The column is kept in the schema for backwards compatibility with old
    DBs but the new code never reads or writes it. This test pins the
    contract: it's there, but it's harmless."""
    cols = {r[1] for r in db._get_conn()
            .execute("PRAGMA table_info(email_templates)").fetchall()}
    assert "header_html" in cols


def test_email_template_update_rejects_header_html(tmp_db):
    """If header_html sneaks back into the template update allowlist, two
    admins on different templates would see different header content for
    the same issue — the whole point of the global lift was to prevent that."""
    t = db.email_template_create(name="x", description="", subject="s",
                                  html="{articles}", article_html="{title}")
    with pytest.raises(ValueError) as exc:
        db.email_template_update(t["id"], header_html="<h2>nope</h2>")
    assert "header_html" in str(exc.value)


def test_email_template_create_no_longer_accepts_header_html(tmp_db):
    """Belt-and-braces: the create signature was tightened to a positional
    arg list that doesn't include header_html, so a stale call site
    passing header_html would fail loudly at TypeError instead of
    silently writing to a dead column."""
    with pytest.raises(TypeError):
        db.email_template_create(
            name="x", description="", subject="s",
            html="{articles}", article_html="{title}",
            header_html=HEADER_SNIPPET,  # type: ignore[call-arg]
        )


# ── Global config ──────────────────────────────────────────────────────────

def test_global_header_round_trips_via_cfg(tmp_db):
    assert db.cfg_get("header_html") == ""
    db.cfg_set("header_html", HEADER_SNIPPET)
    assert db.cfg_get("header_html") == HEADER_SNIPPET


# ── Migration: per-template → global ───────────────────────────────────────

def test_migration_lifts_existing_template_header_into_global(tmp_path, monkeypatch):
    """Simulate an upgrade from the brief per-template-header build: a DB
    where header_html lives on a template row but the global cfg slot is
    empty. After init_db, the global value should hold the template's
    content and the template column should be cleared."""
    db_path = str(tmp_path / "legacy.db")
    monkeypatch.setattr("secdigest.config.DB_PATH", db_path)
    db._conn = None

    # Build the legacy state with raw sqlite3 — bypass our helpers so the
    # data shape exactly mirrors a real upgrade
    raw = sqlite3.connect(db_path)
    raw.executescript("""
        CREATE TABLE config_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE email_templates (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
            subject TEXT NOT NULL DEFAULT 'S', html TEXT NOT NULL,
            article_html TEXT NOT NULL, header_html TEXT DEFAULT '',
            is_builtin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    raw.execute(
        "INSERT INTO email_templates(name, html, article_html, header_html) VALUES (?,?,?,?)",
        ("legacy", "{articles}", "{title}", "<h2>legacy header</h2>"),
    )
    raw.commit()
    raw.close()

    # Run init_db (which runs every migration in order)
    db.init_db()

    assert db.cfg_get("header_html") == "<h2>legacy header</h2>"
    # Template column nulled out so it can't be rendered as a stale shadow
    row = db._get_conn().execute(
        "SELECT header_html FROM email_templates WHERE name='legacy'"
    ).fetchone()
    assert (row["header_html"] or "") == ""

    db._conn.close()
    db._conn = None


def test_migration_no_op_when_global_already_set(tmp_path, monkeypatch):
    """A second run must NOT clobber a global header the admin has since
    edited — only the first migration pass should touch it."""
    db_path = str(tmp_path / "second.db")
    monkeypatch.setattr("secdigest.config.DB_PATH", db_path)
    db._conn = None
    db.init_db()
    db.cfg_set("header_html", "<h2>admin choice</h2>")

    # Pretend a template still has stale header_html (shouldn't matter)
    db._get_conn().execute(
        "UPDATE email_templates SET header_html='<h2>OLD</h2>' WHERE id=1"
    )
    db._get_conn().commit()

    # Re-run migrations (init_db is idempotent)
    db.init_db()
    assert db.cfg_get("header_html") == "<h2>admin choice</h2>", \
        "second migration pass overwrote admin's edit"

    db._conn.close()
    db._conn = None


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
    db.cfg_set("header_html", HEADER_SNIPPET)
    # Toggle defaults to off — the global header is set, but this issue
    # opted out
    assert mailer._header_block_for(n["id"]) == ""


def test_header_block_for_renders_when_toggle_on_and_global_set(tmp_db):
    n = _seed()
    db.cfg_set("header_html", HEADER_SNIPPET)
    db.newsletter_set_header(n["id"], True)
    block = mailer._header_block_for(n["id"])
    assert "Editor's intro" in block
    assert block.startswith("<tr>")


def test_header_block_for_empty_when_global_blank(tmp_db):
    """Toggle on, global header empty → empty output. Otherwise an admin
    flipping the switch with no global header configured would emit an
    empty <tr><td> in the email, which looks broken."""
    n = _seed()
    db.newsletter_set_header(n["id"], True)
    assert db.cfg_get("header_html") == ""
    assert mailer._header_block_for(n["id"]) == ""


def test_header_block_for_does_not_depend_on_active_template(tmp_db):
    """The whole point of the global lift: switching templates must NOT
    change the rendered header content. This test would have caught a
    regression where the lookup re-forked per-template."""
    n = _seed()
    db.cfg_set("header_html", HEADER_SNIPPET)
    db.newsletter_set_header(n["id"], True)

    # Two different templates, neither carrying any header data
    t1 = db.email_template_create(name="A", description="", subject="s",
                                   html="{articles}", article_html="{title}")
    t2 = db.email_template_create(name="B", description="", subject="s",
                                   html="{articles}", article_html="{title}")

    db.newsletter_set_template_id(n["id"], t1["id"])
    block_a = mailer._header_block_for(n["id"])

    db.newsletter_set_template_id(n["id"], t2["id"])
    block_b = mailer._header_block_for(n["id"])

    assert block_a == block_b, "switching template changed the header — global lift regressed"
    assert "Editor's intro" in block_a


# ── Preview route ──────────────────────────────────────────────────────────

async def test_day_preview_includes_global_header_when_query_set(admin_client):
    """Live builder behaviour: include_header=1 pulls from global cfg, not
    from the template ID supplied in the query string."""
    _seed()
    db.cfg_set("header_html", HEADER_SNIPPET)
    tid = db.email_template_default()["id"]

    r_off = await admin_client.get(f"/day/2026-05-04/preview?template_id={tid}&include_header=0")
    r_on = await admin_client.get(f"/day/2026-05-04/preview?template_id={tid}&include_header=1")
    assert r_off.status_code == 200 and r_on.status_code == 200
    assert "Editor's intro" not in r_off.text
    assert "Editor's intro" in r_on.text


# ── Templates page: global header save endpoint ────────────────────────────

async def test_post_email_templates_header_saves_global(admin_client):
    tok = await get_csrf(admin_client, "/email-templates")
    r = await admin_client.post("/email-templates/header", data={
        "csrf_token": tok,
        "header_html": HEADER_SNIPPET,
    })
    assert r.status_code == 302
    assert db.cfg_get("header_html") == HEADER_SNIPPET


async def test_post_email_templates_header_can_clear(admin_client):
    """Submitting an empty textarea is the documented way to disable the
    header globally — the route must accept it instead of validating non-empty."""
    db.cfg_set("header_html", HEADER_SNIPPET)
    tok = await get_csrf(admin_client, "/email-templates")
    r = await admin_client.post("/email-templates/header", data={
        "csrf_token": tok,
        "header_html": "",
    })
    assert r.status_code == 302
    assert db.cfg_get("header_html") == ""


async def test_email_templates_page_renders_current_global_header(admin_client):
    from markupsafe import escape as _jinja_escape
    db.cfg_set("header_html", HEADER_SNIPPET)
    r = await admin_client.get("/email-templates")
    assert r.status_code == 200
    # Jinja2 autoescape uses markupsafe encoding (&#34; / &#39;) inside textarea
    assert str(_jinja_escape(HEADER_SNIPPET)) in r.text


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


# ── Template CRUD via HTTP no longer accepts header_html ───────────────────

async def test_templates_new_form_ignores_header_html_field(admin_client):
    """Any stale form posting header_html should be silently ignored — not
    raise — because we removed the Form param. The header was lifted to a
    separate endpoint; rejecting the whole submission would be hostile to
    admins on a stale browser tab."""
    tok = await get_csrf(admin_client, "/email-templates")
    r = await admin_client.post("/email-templates/new", data={
        "csrf_token": tok,
        "name": "stale-tab",
        "description": "",
        "subject": "S — {date}",
        "html": "<html>{articles}{unsubscribe_url}</html>",
        "article_html": "<tr><td>{title}</td></tr>",
        "header_html": HEADER_SNIPPET,  # stale field — should be ignored, not crash
    })
    assert r.status_code == 302
    rows = [t for t in db.email_template_list() if t["name"] == "stale-tab"]
    assert rows, "template wasn't created — handler probably rejected the stray field"

"""Schema migration smoke tests.

Two scenarios:

  • Fresh install — init_db on an empty file produces the current schema with
    every expected column and seed data.
  • Legacy upgrade — write a pre-migration schema (the one shipped before
    weekly/monthly digests + DOI), run init_db, verify the rebuild preserves
    rows, FKs, and adds the new columns.

Plus: init_db is idempotent (safe to run repeatedly without side effects).
"""
import sqlite3

import pytest

import secdigest.db as db_module
from secdigest import config


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ── Fresh-install schema sanity ──────────────────────────────────────────────

def test_fresh_install_has_all_columns(tmp_db):
    """A fresh DB after init_db must have every column the application reads."""
    conn = db_module._get_conn()

    nl_cols = _cols(conn, "newsletters")
    for required in ("kind", "date", "period_start", "period_end", "status"):
        assert required in nl_cols, f"newsletters missing {required}"

    art_cols = _cols(conn, "articles")
    for required in ("source", "source_name", "pin_weekly", "pin_monthly", "included"):
        assert required in art_cols, f"articles missing {required}"

    sub_cols = _cols(conn, "subscribers")
    for required in ("cadence", "confirmed", "confirm_token", "unsubscribe_token"):
        assert required in sub_cols, f"subscribers missing {required}"

    # digest_articles should exist as a join table
    digest_cols = _cols(conn, "digest_articles")
    assert digest_cols >= {"digest_id", "article_id", "position", "included"}


def test_fresh_install_seeds_email_templates(tmp_db):
    templates = db_module.email_template_list()
    names = {t["name"] for t in templates}
    # The six built-ins should all be present
    expected = {"Dark Terminal", "Clean Light", "Minimal", "2-Column Grid",
                "Mobile Dark", "Mobile Light"}
    assert expected.issubset(names), f"missing built-in templates: {expected - names}"


def test_fresh_install_seeds_default_prompts(tmp_db):
    prompts = db_module.prompt_list()
    types = {p["type"] for p in prompts}
    assert {"curation", "summary"}.issubset(types)


def test_init_db_is_idempotent(tmp_db):
    """Running init_db twice must not duplicate rows or alter schema."""
    n_templates_before = len(db_module.email_template_list())
    n_prompts_before = len(db_module.prompt_list())
    cols_before = _cols(db_module._get_conn(), "newsletters")

    db_module.init_db()  # second call

    assert len(db_module.email_template_list()) == n_templates_before
    assert len(db_module.prompt_list()) == n_prompts_before
    assert _cols(db_module._get_conn(), "newsletters") == cols_before


# ── Legacy DB upgrade ────────────────────────────────────────────────────────

LEGACY_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE newsletters (
    id          INTEGER PRIMARY KEY,
    date        TEXT    UNIQUE NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'draft',
    sent_at     TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    newsletter_id INTEGER NOT NULL REFERENCES newsletters(id),
    hn_id INTEGER, title TEXT, url TEXT, hn_url TEXT,
    hn_score INTEGER, hn_comments INTEGER,
    relevance_score REAL, relevance_reason TEXT, summary TEXT,
    position INTEGER, included INTEGER DEFAULT 1,
    source TEXT DEFAULT 'hn',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE subscribers (
    id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT,
    active INTEGER DEFAULT 1, unsubscribe_token TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE config_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO newsletters(id, date, status) VALUES
    (1, '2026-04-15', 'sent'),
    (2, '2026-04-16', 'draft');
INSERT INTO articles(id, newsletter_id, title, url, relevance_score, position) VALUES
    (10, 1, 'old-A', 'https://x/a', 8.0, 0),
    (11, 2, 'old-B', 'https://x/b', 6.0, 0);
INSERT INTO subscribers(id, email, unsubscribe_token) VALUES (1, 'legacy@x.com', 'tok-legacy');
"""


@pytest.fixture
def legacy_db(monkeypatch, tmp_path):
    """Build a SQLite file with the pre-migration schema and seed it with rows
    that should survive the upgrade. Yields the path; init_db() is NOT called yet.
    """
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", db_path)
    db_module._conn = None
    yield db_path
    if db_module._conn:
        db_module._conn.close()
        db_module._conn = None


def test_legacy_db_upgrades_cleanly(legacy_db):
    """init_db on a pre-migration DB must add new columns and rebuild newsletters
    without dropping rows or breaking FKs."""
    db_module.init_db()
    conn = db_module._get_conn()

    # New columns landed
    assert {"kind", "period_start", "period_end"} <= _cols(conn, "newsletters")
    assert {"pin_weekly", "pin_monthly", "source_name"} <= _cols(conn, "articles")
    assert {"cadence", "confirmed", "confirm_token"} <= _cols(conn, "subscribers")

    # Existing newsletter rows preserved with kind=daily, period anchored to date
    rows = conn.execute(
        "SELECT id, kind, date, period_start, period_end FROM newsletters ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "daily", "2026-04-15", "2026-04-15", "2026-04-15"),
        (2, "daily", "2026-04-16", "2026-04-16", "2026-04-16"),
    ]

    # FK from articles.newsletter_id still resolves (table rebuild doesn't break joins)
    arts = conn.execute(
        "SELECT a.id, n.date FROM articles a JOIN newsletters n ON n.id = a.newsletter_id ORDER BY a.id"
    ).fetchall()
    assert [tuple(r) for r in arts] == [(10, "2026-04-15"), (11, "2026-04-16")]

    # Pre-DOI subscribers backfilled to confirmed=1 (admin trusted them when created)
    sub = conn.execute("SELECT email, confirmed, cadence FROM subscribers").fetchone()
    assert sub["email"] == "legacy@x.com"
    assert sub["confirmed"] == 1
    assert sub["cadence"] == "daily"


def test_legacy_db_idempotent_after_upgrade(legacy_db):
    """Running init_db a second time on an already-migrated DB must be a no-op."""
    db_module.init_db()
    cols_after_first = {
        "newsletters": _cols(db_module._get_conn(), "newsletters"),
        "articles": _cols(db_module._get_conn(), "articles"),
        "subscribers": _cols(db_module._get_conn(), "subscribers"),
    }
    n_articles_after_first = db_module._get_conn().execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]

    db_module.init_db()  # second pass

    cols_after_second = {
        "newsletters": _cols(db_module._get_conn(), "newsletters"),
        "articles": _cols(db_module._get_conn(), "articles"),
        "subscribers": _cols(db_module._get_conn(), "subscribers"),
    }
    assert cols_after_first == cols_after_second
    assert db_module._get_conn().execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0] == n_articles_after_first

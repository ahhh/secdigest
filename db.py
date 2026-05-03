import sqlite3
import threading
import json
from datetime import date as dt_date
from pathlib import Path
import config

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS newsletters (
    id          INTEGER PRIMARY KEY,
    date        TEXT    UNIQUE NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'draft',
    sent_at     TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY,
    newsletter_id   INTEGER NOT NULL REFERENCES newsletters(id),
    hn_id           INTEGER,
    title           TEXT    NOT NULL,
    url             TEXT,
    hn_url          TEXT,
    hn_score        INTEGER DEFAULT 0,
    hn_comments     INTEGER DEFAULT 0,
    relevance_score REAL    DEFAULT 0,
    relevance_reason TEXT,
    summary         TEXT,
    position        INTEGER DEFAULT 0,
    included        INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    active      INTEGER DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscribers (
    id          INTEGER PRIMARY KEY,
    email       TEXT    UNIQUE NOT NULL,
    name        TEXT    DEFAULT '',
    active      INTEGER DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS llm_audit_log (
    id              INTEGER PRIMARY KEY,
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    operation       TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cached_tokens   INTEGER DEFAULT 0,
    article_id      INTEGER,
    result_snippet  TEXT
);

CREATE TABLE IF NOT EXISTS config_kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""

DEFAULT_PROMPTS = [
    {
        "name": "Security Relevance Filter",
        "type": "curation",
        "content": (
            "Score each article for relevance to security professionals on a scale of 0-10.\n"
            "HIGH relevance (7-10): CVEs, exploits, malware, threat intel, security tools, "
            "vulnerabilities, incident reports, privacy breaches, cryptography research, "
            "pentesting, red team techniques, supply chain attacks, zero-days.\n"
            "MEDIUM relevance (4-6): Privacy policy changes, government/legal actions on tech companies, "
            "general infosec news, interesting but non-critical security research.\n"
            "LOW relevance (0-3): General tech news, business news, non-security programming, "
            "AI hype without security angle, sports/politics/entertainment."
        ),
    },
    {
        "name": "Technical Summary Style",
        "type": "summary",
        "content": (
            "Write a concise 2-3 sentence summary for a security professional audience. "
            "Focus on: what the vulnerability/tool/threat is, who or what it affects, "
            "severity/impact, and any CVE IDs, affected versions, or mitigations if known. "
            "Be factual and direct. No fluff, no marketing language."
        ),
    },
]


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db():
    conn = _get_conn()
    with _lock:
        conn.executescript(SCHEMA)
        conn.commit()
        _seed_config(conn)
        _seed_prompts(conn)


def _seed_config(conn):
    for key, val in config.DB_CONFIG_DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO config_kv(key, value) VALUES (?, ?)",
            (key, val)
        )
    conn.commit()


def _seed_prompts(conn):
    count = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
    if count == 0:
        for p in DEFAULT_PROMPTS:
            conn.execute(
                "INSERT INTO prompts(name, type, content) VALUES (?,?,?)",
                (p["name"], p["type"], p["content"])
            )
        conn.commit()


# --- Config ---

def cfg_get(key: str) -> str:
    row = _get_conn().execute("SELECT value FROM config_kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else config.DB_CONFIG_DEFAULTS.get(key, "")


def cfg_set(key: str, value: str):
    with _lock:
        _get_conn().execute(
            "INSERT INTO config_kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        _get_conn().commit()


def cfg_all() -> dict:
    rows = _get_conn().execute("SELECT key, value FROM config_kv").fetchall()
    return {r[0]: r[1] for r in rows}


# --- Newsletters ---

def newsletter_get_or_create(date_str: str) -> dict:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM newsletters WHERE date=?", (date_str,)).fetchone()
    if row:
        return dict(row)
    with _lock:
        conn.execute("INSERT OR IGNORE INTO newsletters(date) VALUES(?)", (date_str,))
        conn.commit()
    row = conn.execute("SELECT * FROM newsletters WHERE date=?", (date_str,)).fetchone()
    return dict(row)


def newsletter_get(date_str: str) -> dict | None:
    row = _get_conn().execute("SELECT * FROM newsletters WHERE date=?", (date_str,)).fetchone()
    return dict(row) if row else None


def newsletter_update(id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [id]
    with _lock:
        _get_conn().execute(f"UPDATE newsletters SET {fields} WHERE id=?", vals)
        _get_conn().commit()


def newsletter_list(limit: int = 60) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM newsletters ORDER BY date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# --- Articles ---

def article_insert(newsletter_id: int, hn_id: int, title: str, url: str,
                   hn_score: int, hn_comments: int, relevance_score: float,
                   relevance_reason: str, position: int) -> int:
    with _lock:
        cur = _get_conn().execute(
            """INSERT OR IGNORE INTO articles
               (newsletter_id, hn_id, title, url, hn_url, hn_score, hn_comments,
                relevance_score, relevance_reason, position)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (newsletter_id, hn_id, title, url,
             f"https://news.ycombinator.com/item?id={hn_id}",
             hn_score, hn_comments, relevance_score, relevance_reason, position)
        )
        _get_conn().commit()
        return cur.lastrowid


def article_list(newsletter_id: int) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM articles WHERE newsletter_id=? ORDER BY position ASC, relevance_score DESC",
        (newsletter_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def article_update(id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [id]
    with _lock:
        _get_conn().execute(f"UPDATE articles SET {fields} WHERE id=?", vals)
        _get_conn().commit()


def article_reorder(newsletter_id: int, ordered_ids: list[int]):
    with _lock:
        for pos, aid in enumerate(ordered_ids):
            _get_conn().execute(
                "UPDATE articles SET position=? WHERE id=? AND newsletter_id=?",
                (pos, aid, newsletter_id)
            )
        _get_conn().commit()


def article_hn_ids(newsletter_id: int) -> set[int]:
    rows = _get_conn().execute(
        "SELECT hn_id FROM articles WHERE newsletter_id=?", (newsletter_id,)
    ).fetchall()
    return {r[0] for r in rows}


# --- Prompts ---

def prompt_list(type_filter: str | None = None) -> list[dict]:
    if type_filter:
        rows = _get_conn().execute(
            "SELECT * FROM prompts WHERE type=? ORDER BY id", (type_filter,)
        ).fetchall()
    else:
        rows = _get_conn().execute("SELECT * FROM prompts ORDER BY type, id").fetchall()
    return [dict(r) for r in rows]


def prompt_create(name: str, type_: str, content: str) -> dict:
    with _lock:
        cur = _get_conn().execute(
            "INSERT INTO prompts(name, type, content) VALUES(?,?,?)", (name, type_, content)
        )
        _get_conn().commit()
    return dict(_get_conn().execute("SELECT * FROM prompts WHERE id=?", (cur.lastrowid,)).fetchone())


def prompt_update(id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [id]
    with _lock:
        _get_conn().execute(f"UPDATE prompts SET {fields} WHERE id=?", vals)
        _get_conn().commit()


def prompt_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM prompts WHERE id=?", (id,))
        _get_conn().commit()


# --- Subscribers ---

def subscriber_list() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM subscribers ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def subscriber_create(email: str, name: str = "") -> dict | None:
    try:
        with _lock:
            cur = _get_conn().execute(
                "INSERT INTO subscribers(email, name) VALUES(?,?)", (email, name)
            )
            _get_conn().commit()
        return dict(_get_conn().execute("SELECT * FROM subscribers WHERE id=?", (cur.lastrowid,)).fetchone())
    except sqlite3.IntegrityError:
        return None


def subscriber_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM subscribers WHERE id=?", (id,))
        _get_conn().commit()


def subscriber_active() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM subscribers WHERE active=1"
    ).fetchall()
    return [dict(r) for r in rows]


# --- LLM Audit ---

def audit_log(operation: str, model: str, input_tokens: int, output_tokens: int,
              cached_tokens: int, article_id: int | None, result_snippet: str):
    with _lock:
        _get_conn().execute(
            """INSERT INTO llm_audit_log
               (operation, model, input_tokens, output_tokens, cached_tokens, article_id, result_snippet)
               VALUES (?,?,?,?,?,?,?)""",
            (operation, model, input_tokens, output_tokens, cached_tokens, article_id,
             result_snippet[:500] if result_snippet else "")
        )
        _get_conn().commit()


def audit_recent(limit: int = 50) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM llm_audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]

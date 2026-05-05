"""All SQLite operations. Single module — import this everywhere you need data access."""
import sqlite3
import threading
from secdigest import config

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

CREATE TABLE IF NOT EXISTS email_templates (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    description  TEXT    DEFAULT '',
    subject      TEXT    NOT NULL DEFAULT 'SecDigest — {date}',
    html         TEXT    NOT NULL,
    article_html TEXT    NOT NULL,
    is_builtin   INTEGER DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

_TMPL_DARK_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 16px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;">
<tr><td style="padding-bottom:24px;border-bottom:2px solid #39ff14;">
<span style="font-family:monospace;font-size:1.6em;font-weight:700;color:#39ff14;">SecDigest</span>
<span style="color:#6e7681;margin-left:12px;font-size:.9em;">{date}</span>
</td></tr>
{articles}
<tr><td style="padding-top:24px;font-size:.75em;color:#6e7681;border-top:1px solid #21262d;">
You're receiving this because you subscribed to SecDigest.
</td></tr>
</table></td></tr></table></body></html>"""

_TMPL_DARK_ARTICLE = """\
<tr><td style="padding:16px 0;border-bottom:1px solid #21262d;">
<div style="font-size:.75em;color:#6e7681;margin-bottom:4px;">#{number} &nbsp;&middot;&nbsp; HN {hn_score} pts &nbsp;&middot;&nbsp; {hn_comments} comments</div>
<a href="{url}" style="color:#58a6ff;font-size:1.05em;font-weight:600;text-decoration:none;">{title}</a>
<p style="color:#c9d1d9;margin:8px 0 4px;font-size:.9em;line-height:1.5;">{summary}</p>
<a href="{hn_url}" style="color:#6e7681;font-size:.8em;">HN discussion &#8594;</a>
</td></tr>"""

_TMPL_LIGHT_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f8fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
<tr><td style="padding:28px 32px 20px;border-bottom:3px solid #0969da;">
<span style="font-size:1.4em;font-weight:700;color:#0969da;letter-spacing:-0.5px;">SecDigest</span>
<span style="color:#8c959f;margin-left:10px;font-size:.875em;">{date}</span>
</td></tr>
<tr><td style="padding:0 32px;">
<table width="100%" cellpadding="0" cellspacing="0">{articles}</table>
</td></tr>
<tr><td style="padding:20px 32px 28px;font-size:.75em;color:#8c959f;border-top:1px solid #e1e4e8;">
You're receiving this because you subscribed to SecDigest.
</td></tr>
</table></td></tr></table></body></html>"""

_TMPL_LIGHT_ARTICLE = """\
<tr><td style="padding:20px 0;border-bottom:1px solid #e1e4e8;">
<div style="font-size:.75em;color:#8c959f;margin-bottom:6px;">#{number} &middot; HN {hn_score} pts &middot; {hn_comments} comments</div>
<a href="{url}" style="color:#0969da;font-size:1em;font-weight:600;text-decoration:none;line-height:1.4;">{title}</a>
<p style="color:#24292f;margin:8px 0 6px;font-size:.875em;line-height:1.6;">{summary}</p>
<a href="{hn_url}" style="color:#8c959f;font-size:.8em;">HN discussion &#8594;</a>
</td></tr>"""

_TMPL_MINIMAL_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#ffffff;font-family:Georgia,'Times New Roman',serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:40px 20px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;">
<tr><td style="padding-bottom:24px;border-bottom:1px solid #cccccc;">
<strong style="font-size:1.2em;color:#111111;">SecDigest</strong>
<span style="color:#888888;margin-left:10px;font-size:.9em;">{date}</span>
</td></tr>
{articles}
<tr><td style="padding-top:32px;font-size:.75em;color:#aaaaaa;border-top:1px solid #eeeeee;">
You're receiving this because you subscribed to SecDigest.
</td></tr>
</table></td></tr></table></body></html>"""

_TMPL_MINIMAL_ARTICLE = """\
<tr><td style="padding:24px 0;border-bottom:1px solid #eeeeee;">
<div style="font-size:.8em;color:#aaaaaa;margin-bottom:6px;">#{number}</div>
<a href="{url}" style="color:#111111;font-size:1em;font-weight:bold;text-decoration:none;">{title}</a>
<p style="color:#444444;margin:10px 0 8px;font-size:.875em;line-height:1.7;">{summary}</p>
<a href="{hn_url}" style="color:#aaaaaa;font-size:.8em;text-decoration:none;">Discussion on HN &#8594;</a>
</td></tr>"""

DEFAULT_EMAIL_TEMPLATES = [
    {
        "name": "Dark Terminal",
        "description": "Dark background with monospace font and green accent. Matches the SecDigest app aesthetic.",
        "subject": "SecDigest — {date}",
        "html": _TMPL_DARK_HTML,
        "article_html": _TMPL_DARK_ARTICLE,
        "is_builtin": 1,
    },
    {
        "name": "Clean Light",
        "description": "White background, blue header, professional sans-serif style.",
        "subject": "SecDigest — {date}",
        "html": _TMPL_LIGHT_HTML,
        "article_html": _TMPL_LIGHT_ARTICLE,
        "is_builtin": 1,
    },
    {
        "name": "Minimal",
        "description": "Plain white with serif font. No heavy styling — lets the content speak.",
        "subject": "SecDigest — {date}",
        "html": _TMPL_MINIMAL_HTML,
        "article_html": _TMPL_MINIMAL_ARTICLE,
        "is_builtin": 1,
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
        _seed_email_templates(conn)


def _seed_config(conn):
    for key, val in config.DB_CONFIG_DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO config_kv(key, value) VALUES (?, ?)", (key, val))
    conn.commit()


def _seed_prompts(conn):
    if conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0:
        for p in DEFAULT_PROMPTS:
            conn.execute(
                "INSERT INTO prompts(name, type, content) VALUES (?,?,?)",
                (p["name"], p["type"], p["content"])
            )
        conn.commit()


def _seed_email_templates(conn):
    if conn.execute("SELECT COUNT(*) FROM email_templates").fetchone()[0] == 0:
        for t in DEFAULT_EMAIL_TEMPLATES:
            conn.execute(
                "INSERT INTO email_templates(name, description, subject, html, article_html, is_builtin) "
                "VALUES (?,?,?,?,?,?)",
                (t["name"], t["description"], t["subject"], t["html"], t["article_html"], t["is_builtin"])
            )
        conn.commit()


# ── Config ───────────────────────────────────────────────────────────────────

def cfg_get(key: str) -> str:
    row = _get_conn().execute("SELECT value FROM config_kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else config.DB_CONFIG_DEFAULTS.get(key, "")


def cfg_set(key: str, value: str):
    with _lock:
        _get_conn().execute(
            "INSERT INTO config_kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        _get_conn().commit()


def cfg_all() -> dict:
    return {r[0]: r[1] for r in _get_conn().execute("SELECT key, value FROM config_kv").fetchall()}


# ── Newsletters ───────────────────────────────────────────────────────────────

def newsletter_get_or_create(date_str: str) -> dict:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM newsletters WHERE date=?", (date_str,)).fetchone()
    if row:
        return dict(row)
    with _lock:
        conn.execute("INSERT OR IGNORE INTO newsletters(date) VALUES(?)", (date_str,))
        conn.commit()
    return dict(conn.execute("SELECT * FROM newsletters WHERE date=?", (date_str,)).fetchone())


def newsletter_get(date_str: str) -> dict | None:
    row = _get_conn().execute("SELECT * FROM newsletters WHERE date=?", (date_str,)).fetchone()
    return dict(row) if row else None


def newsletter_update(id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    with _lock:
        _get_conn().execute(f"UPDATE newsletters SET {fields} WHERE id=?", [*kwargs.values(), id])
        _get_conn().commit()


def newsletter_list(limit: int = 60) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM newsletters ORDER BY date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Articles ──────────────────────────────────────────────────────────────────

def article_get(id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM articles WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def article_insert(newsletter_id: int, hn_id: int | None, title: str, url: str,
                   hn_score: int, hn_comments: int, relevance_score: float,
                   relevance_reason: str, position: int) -> int:
    hn_url = f"https://news.ycombinator.com/item?id={hn_id}" if hn_id else None
    with _lock:
        cur = _get_conn().execute(
            """INSERT OR IGNORE INTO articles
               (newsletter_id, hn_id, title, url, hn_url, hn_score, hn_comments,
                relevance_score, relevance_reason, position)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (newsletter_id, hn_id, title, url,
             hn_url,
             hn_score, hn_comments, relevance_score, relevance_reason, position),
        )
        _get_conn().commit()
        return cur.lastrowid


def article_list(newsletter_id: int) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM articles WHERE newsletter_id=? ORDER BY position ASC, relevance_score DESC",
        (newsletter_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def article_update(id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    with _lock:
        _get_conn().execute(f"UPDATE articles SET {fields} WHERE id=?", [*kwargs.values(), id])
        _get_conn().commit()


def article_reorder(newsletter_id: int, ordered_ids: list[int]):
    with _lock:
        for pos, aid in enumerate(ordered_ids):
            _get_conn().execute(
                "UPDATE articles SET position=? WHERE id=? AND newsletter_id=?",
                (pos, aid, newsletter_id),
            )
        _get_conn().commit()


def article_hn_ids(newsletter_id: int) -> set[int]:
    rows = _get_conn().execute(
        "SELECT hn_id FROM articles WHERE newsletter_id=?", (newsletter_id,)
    ).fetchall()
    return {r[0] for r in rows}


# ── Prompts ───────────────────────────────────────────────────────────────────

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
    with _lock:
        _get_conn().execute(f"UPDATE prompts SET {fields} WHERE id=?", [*kwargs.values(), id])
        _get_conn().commit()


def prompt_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM prompts WHERE id=?", (id,))
        _get_conn().commit()


# ── Subscribers ───────────────────────────────────────────────────────────────

def subscriber_list() -> list[dict]:
    return [dict(r) for r in _get_conn().execute("SELECT * FROM subscribers ORDER BY id").fetchall()]


def subscriber_create(email: str, name: str = "") -> dict | None:
    try:
        with _lock:
            cur = _get_conn().execute(
                "INSERT INTO subscribers(email, name) VALUES(?,?)", (email, name)
            )
            _get_conn().commit()
        return dict(_get_conn().execute("SELECT * FROM subscribers WHERE id=?", (cur.lastrowid,)).fetchone())
    except Exception:
        return None


def subscriber_update(id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    with _lock:
        _get_conn().execute(f"UPDATE subscribers SET {fields} WHERE id=?", [*kwargs.values(), id])
        _get_conn().commit()


def subscriber_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM subscribers WHERE id=?", (id,))
        _get_conn().commit()


def subscriber_active() -> list[dict]:
    return [dict(r) for r in _get_conn().execute(
        "SELECT * FROM subscribers WHERE active=1"
    ).fetchall()]


# ── LLM Audit Log ─────────────────────────────────────────────────────────────

def audit_log(operation: str, model: str, input_tokens: int, output_tokens: int,
              cached_tokens: int, article_id: int | None, result_snippet: str):
    with _lock:
        _get_conn().execute(
            """INSERT INTO llm_audit_log
               (operation, model, input_tokens, output_tokens, cached_tokens, article_id, result_snippet)
               VALUES (?,?,?,?,?,?,?)""",
            (operation, model, input_tokens, output_tokens, cached_tokens,
             article_id, result_snippet[:500] if result_snippet else ""),
        )
        _get_conn().commit()


def audit_recent(limit: int = 50) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM llm_audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Email Templates ───────────────────────────────────────────────────────────

def email_template_list() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM email_templates ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def email_template_get(id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM email_templates WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def email_template_default() -> dict | None:
    row = _get_conn().execute("SELECT * FROM email_templates ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def email_template_create(name: str, description: str, subject: str, html: str, article_html: str) -> dict:
    with _lock:
        cur = _get_conn().execute(
            "INSERT INTO email_templates(name, description, subject, html, article_html) VALUES (?,?,?,?,?)",
            (name, description, subject, html, article_html),
        )
        _get_conn().commit()
    return dict(_get_conn().execute("SELECT * FROM email_templates WHERE id=?", (cur.lastrowid,)).fetchone())


def email_template_update(id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k}=?" for k in kwargs)
    with _lock:
        _get_conn().execute(f"UPDATE email_templates SET {fields} WHERE id=?", [*kwargs.values(), id])
        _get_conn().commit()


def email_template_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM email_templates WHERE id=? AND is_builtin=0", (id,))
        _get_conn().commit()


def newsletter_get_template_id(newsletter_id: int) -> int | None:
    row = _get_conn().execute(
        "SELECT value FROM config_kv WHERE key=?", (f"tmpl_{newsletter_id}",)
    ).fetchone()
    return int(row[0]) if row else None


def newsletter_set_template_id(newsletter_id: int, template_id: int):
    cfg_set(f"tmpl_{newsletter_id}", str(template_id))


def newsletter_get_subject(newsletter_id: int) -> str | None:
    row = _get_conn().execute(
        "SELECT value FROM config_kv WHERE key=?", (f"subject_{newsletter_id}",)
    ).fetchone()
    return row[0] if row else None


def newsletter_set_subject(newsletter_id: int, subject: str):
    cfg_set(f"subject_{newsletter_id}", subject)

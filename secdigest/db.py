"""All SQLite operations. Single module — import this everywhere you need data access.

Architecture in one paragraph: we keep a single long-lived
``sqlite3.Connection`` per process (``_conn``) shared across threads.
SQLite handles this fine when ``check_same_thread=False`` is set, but
we still funnel writes through a module-level ``threading.Lock`` to
avoid two threads stepping on each other's BEGIN/COMMIT pairs. WAL mode
is enabled so reads don't block writes and vice versa.

What lives here:
- **SCHEMA**: the canonical CREATE TABLE statements, applied
  idempotently on startup via ``CREATE TABLE IF NOT EXISTS``.
- **Migrations**: forward-only ``_migrate_*`` helpers each guarded by
  "is the column/table/data already in the new shape?" checks. They
  run unconditionally on every startup; idempotency is enforced by the
  guards, not by tracking applied migrations.
- **Seeders**: ``_seed_*`` populate config defaults, prompts, and email
  templates on first run.
- **CRUD**: thin wrappers around SQL grouped by table — newsletters,
  articles, subscribers, prompts, etc. Each function returns plain
  ``dict``/``list[dict]`` (we set ``row_factory = sqlite3.Row``).

Why one big module? Every table's helpers want access to ``_get_conn``
and ``_lock``, and the routes layer just wants to import a cohesive
"db" namespace rather than a half-dozen sub-modules. At ~1500 lines
it's still scrollable.
"""
import sqlite3
import threading
import uuid
from secdigest import config

# Process-wide singleton connection. ``None`` until ``init_db()`` opens it.
_conn: sqlite3.Connection | None = None
# Serialises writes across threads. Reads are safe to run concurrently
# in WAL mode without holding this lock.
_lock = threading.Lock()

# Canonical schema applied on every startup. Each table uses
# ``CREATE TABLE IF NOT EXISTS`` so this script is safe to run repeatedly;
# changes to existing tables go through the ``_migrate_*`` helpers below
# rather than being edited here (or old DBs would silently lose columns).
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- One row per "issue". ``kind`` separates daily/weekly/monthly streams;
-- ``period_start``/``period_end`` define the covered window (a single
-- day for daily, Mon–Sun for weekly, etc.). The UNIQUE(kind, period_start)
-- guarantees one issue per stream-period regardless of when we created it.
CREATE TABLE IF NOT EXISTS newsletters (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL DEFAULT 'daily',
    date         TEXT    NOT NULL,
    period_start TEXT    NOT NULL,
    period_end   TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'draft',
    sent_at      TIMESTAMP,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind, period_start)
);

-- Articles are owned by their *daily* newsletter (``newsletter_id``); they
-- appear in weekly/monthly digests via the ``digest_articles`` join below.
-- ``included`` is the curator toggle (1 = goes out in the email, 0 = pool only);
-- ``position`` controls render order within an issue.
-- ``pin_weekly``/``pin_monthly`` are sticky flags the curator sets to mark
-- a daily article for inclusion in the upcoming weekly/monthly digest seed.
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
    source          TEXT    DEFAULT 'hn',
    source_name     TEXT,
    pin_weekly      INTEGER DEFAULT 0,
    pin_monthly     INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Many-to-many join: a single article (which lives in some daily issue)
-- can appear in weekly and/or monthly digests, possibly with a different
-- ordering/inclusion than it had in its source-day. ON DELETE CASCADE on
-- both sides keeps the join clean if a daily or digest issue is deleted.
CREATE TABLE IF NOT EXISTS digest_articles (
    digest_id  INTEGER NOT NULL REFERENCES newsletters(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id)    ON DELETE CASCADE,
    position   INTEGER DEFAULT 0,
    included   INTEGER DEFAULT 1,
    PRIMARY KEY (digest_id, article_id)
);

CREATE TABLE IF NOT EXISTS rss_feeds (
    id           INTEGER PRIMARY KEY,
    url          TEXT    UNIQUE NOT NULL,
    name         TEXT    NOT NULL DEFAULT '',
    active       INTEGER DEFAULT 1,
    max_articles INTEGER DEFAULT 5,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    active      INTEGER DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Subscribers go through a double-opt-in flow on the public site:
-- a row is inserted with ``confirmed=0`` and a single-use ``confirm_token``,
-- and is only flipped to ``confirmed=1`` when the link in the confirmation
-- email is clicked. ``unsubscribe_token`` is a stable per-subscriber UUID
-- used for unsubscribe links and feedback votes.
CREATE TABLE IF NOT EXISTS subscribers (
    id                INTEGER PRIMARY KEY,
    email             TEXT    UNIQUE NOT NULL,
    name              TEXT    DEFAULT '',
    active            INTEGER DEFAULT 1,
    cadence           TEXT    NOT NULL DEFAULT 'daily',
    confirmed         INTEGER DEFAULT 0,
    confirm_token     TEXT,
    unsubscribe_token TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One row per Claude API call (curation scoring + summarisation). Used to
-- show input/output/cache token counts on the audit page so operators can
-- track API spend and verify prompt caching is hitting.
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
    header_html  TEXT    DEFAULT '',
    is_builtin   INTEGER DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 👍/👎 votes from the buttons embedded in each issue. UNIQUE(subscriber_id,
-- newsletter_id) means re-clicking just updates the vote rather than
-- piling up rows; the route uses INSERT ... ON CONFLICT to flip.
CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    newsletter_id INTEGER NOT NULL REFERENCES newsletters(id) ON DELETE CASCADE,
    vote          TEXT    NOT NULL CHECK (vote IN ('signal','noise')),
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subscriber_id, newsletter_id)
);

-- One row per newsletter that has (or had) a voice summary. The CHECK
-- constraint enforces a small state machine: idle → queued → generating →
-- ready (or → failed). The renderer in mailer.py only embeds an audio
-- block when status='ready' and an s3_key exists.
CREATE TABLE IF NOT EXISTS voice_audio (
    newsletter_id INTEGER PRIMARY KEY REFERENCES newsletters(id) ON DELETE CASCADE,
    status        TEXT    NOT NULL DEFAULT 'idle'
                          CHECK (status IN ('idle','queued','generating','ready','failed')),
    s3_key        TEXT,
    duration_sec  INTEGER,
    voice_text    TEXT,
    error         TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            "Always produce a summary regardless of article type — never refuse. "
            "For vulnerabilities: include CVE IDs, affected versions, severity, and mitigations. "
            "For opinion or discussion pieces: capture the core argument and its security relevance. "
            "For tools or research: describe what it does and why it matters. "
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
{feedback_block}You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
<a href="{unsubscribe_url}" style="color:#6e7681;">Unsubscribe</a>
</td></tr>
</table></td></tr></table></body></html>"""

_TMPL_DARK_ARTICLE = """\
<tr><td style="padding:16px 0;border-bottom:1px solid #21262d;">
<div style="font-size:.75em;color:#6e7681;margin-bottom:4px;">#{number}</div>
<a href="{url}" style="color:#58a6ff;font-size:1.05em;font-weight:600;text-decoration:none;">{title}</a>
<p style="color:#c9d1d9;margin:8px 0 4px;font-size:.9em;line-height:1.5;">{summary}</p>
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
{feedback_block}You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
<a href="{unsubscribe_url}" style="color:#8c959f;">Unsubscribe</a>
</td></tr>
</table></td></tr></table></body></html>"""

_TMPL_LIGHT_ARTICLE = """\
<tr><td style="padding:20px 0;border-bottom:1px solid #e1e4e8;">
<div style="font-size:.75em;color:#8c959f;margin-bottom:6px;">#{number}</div>
<a href="{url}" style="color:#0969da;font-size:1em;font-weight:600;text-decoration:none;line-height:1.4;">{title}</a>
<p style="color:#24292f;margin:8px 0 6px;font-size:.875em;line-height:1.6;">{summary}</p>
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
{feedback_block}You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
<a href="{unsubscribe_url}" style="color:#aaaaaa;">Unsubscribe</a>
</td></tr>
</table></td></tr></table></body></html>"""

_TMPL_MINIMAL_ARTICLE = """\
<tr><td style="padding:24px 0;border-bottom:1px solid #eeeeee;">
<div style="font-size:.8em;color:#aaaaaa;margin-bottom:6px;">#{number}</div>
<a href="{url}" style="color:#111111;font-size:1em;font-weight:bold;text-decoration:none;">{title}</a>
<p style="color:#444444;margin:10px 0 8px;font-size:.875em;line-height:1.7;">{summary}</p>
</td></tr>"""

_TMPL_GRID_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 16px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:700px;">
<tr><td style="padding-bottom:20px;border-bottom:2px solid #39ff14;">
<span style="font-family:monospace;font-size:1.6em;font-weight:700;color:#39ff14;">SecDigest</span>
<span style="color:#6e7681;margin-left:12px;font-size:.9em;">{date}</span>
</td></tr>
<tr><td style="padding-top:14px;">
<table width="100%" cellpadding="0" cellspacing="0">
{articles_2col}
</table>
</td></tr>
<tr><td style="padding-top:20px;font-size:.75em;color:#6e7681;border-top:1px solid #21262d;">
{feedback_block}You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
<a href="{unsubscribe_url}" style="color:#6e7681;">Unsubscribe</a>
</td></tr>
</table></td></tr></table></body></html>"""

_TMPL_GRID_ARTICLE = """\
<td style="width:50%;vertical-align:top;padding:6px;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#161b22;border:1px solid #30363d;border-radius:6px;">
<tr><td style="padding:14px;vertical-align:top;">
<div style="font-size:.7em;color:#6e7681;font-family:monospace;margin-bottom:8px;">#{number}</div>
<a href="{url}" style="color:#58a6ff;font-size:.9em;font-weight:600;text-decoration:none;display:block;line-height:1.4;margin-bottom:10px;">{title}</a>
<p style="color:#c9d1d9;margin:0;font-size:.82em;line-height:1.55;">{summary}</p>
</td></tr>
</table>
</td>"""

# ── Mobile-optimised templates (Gmail iOS) ────────────────────────────────────
# Notes on the mobile templates below:
#   - All styles inlined; Gmail iOS strips <style> reliably for non-Google addrs
#   - <meta name="format-detection"> stops iOS auto-linking dates/numbers
#   - <meta name="x-apple-disable-message-reformatting"> stops iOS Mail rescaling
#   - <meta name="color-scheme"> opts the message into a fixed scheme so Gmail's
#     auto dark-mode invert does not recolour the dark template
#   - Hidden preheader <div> controls the inbox preview snippet
#   - Title links use display:block + ~12px vertical padding so tap targets clear
#     iOS's 44px minimum
#   - System font stack (-apple-system) renders SF on iOS, Segoe on Win mail clients

_TMPL_MOBILE_DARK_HTML = """\
<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>SecDigest — {date}</title>
</head>
<body style="margin:0;padding:0;background:#0d1117;-webkit-text-size-adjust:100%;" bgcolor="#0d1117">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;color:#0d1117;">
SecDigest daily &mdash; {date} &middot; top security stories, summarised.
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#0d1117" style="background:#0d1117;">
<tr><td align="center" style="padding:20px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">
<tr><td style="padding:6px 4px 18px;border-bottom:2px solid #39ff14;">
<div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:22px;font-weight:700;color:#39ff14;letter-spacing:-.5px;line-height:1.2;">SecDigest</div>
<div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:13px;color:#6e7681;margin-top:6px;"><span style="color:#6e7681;">{date}</span></div>
</td></tr>
{articles}
<tr><td style="padding:24px 4px 12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:13px;line-height:1.6;color:#6e7681;border-top:1px solid #21262d;">
{feedback_block}You're receiving this because you subscribed to SecDigest.<br>
<a href="{unsubscribe_url}" style="color:#58a6ff;text-decoration:underline;">Unsubscribe</a>
</td></tr>
</table>
</td></tr></table>
</body></html>"""

_TMPL_MOBILE_DARK_ARTICLE = """\
<tr><td style="padding:18px 4px;border-bottom:1px solid #21262d;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<div style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:12px;color:#6e7681;margin-bottom:4px;letter-spacing:.04em;">#{number}</div>
<a href="{url}" style="display:block;color:#58a6ff;font-size:17px;font-weight:600;text-decoration:none;line-height:1.35;padding:10px 0;">{title}</a>
<div style="color:#c9d1d9;font-size:15px;line-height:1.6;margin-top:4px;">{summary}</div>
</td></tr>"""

_TMPL_MOBILE_LIGHT_HTML = """\
<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>SecDigest — {date}</title>
</head>
<body style="margin:0;padding:0;background:#f6f8fa;-webkit-text-size-adjust:100%;" bgcolor="#f6f8fa">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;color:#f6f8fa;">
SecDigest daily &mdash; {date} &middot; top security stories, summarised.
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f6f8fa" style="background:#f6f8fa;">
<tr><td align="center" style="padding:20px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;">
<tr><td style="padding:24px 20px 18px;border-bottom:3px solid #0969da;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<div style="font-size:22px;font-weight:700;color:#0969da;letter-spacing:-.5px;line-height:1.2;">SecDigest</div>
<div style="font-size:13px;color:#6e7781;margin-top:6px;">{date}</div>
</td></tr>
<tr><td style="padding:0 20px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
{articles}
</table>
</td></tr>
<tr><td style="padding:18px 20px 22px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:13px;line-height:1.6;color:#6e7781;border-top:1px solid #e1e4e8;background:#fafbfc;" bgcolor="#fafbfc">
{feedback_block}You're receiving this because you subscribed to SecDigest.<br>
<a href="{unsubscribe_url}" style="color:#0969da;text-decoration:underline;">Unsubscribe</a>
</td></tr>
</table>
</td></tr></table>
</body></html>"""

_TMPL_MOBILE_LIGHT_ARTICLE = """\
<tr><td style="padding:18px 0;border-bottom:1px solid #e1e4e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<div style="font-size:12px;color:#6e7781;margin-bottom:4px;letter-spacing:.02em;">#{number}</div>
<a href="{url}" style="display:block;color:#0969da;font-size:17px;font-weight:600;text-decoration:none;line-height:1.35;padding:10px 0;">{title}</a>
<div style="color:#1f2328;font-size:15px;line-height:1.6;margin-top:4px;">{summary}</div>
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
    {
        "name": "2-Column Grid",
        "description": "Dark theme with articles in a 2-column card grid. Best for shorter summaries.",
        "subject": "SecDigest — {date}",
        "html": _TMPL_GRID_HTML,
        "article_html": _TMPL_GRID_ARTICLE,
        "is_builtin": 1,
    },
    {
        "name": "Mobile Dark",
        "description": "Mobile-first dark layout tuned for Gmail iOS — fluid width, large tap targets, preheader text.",
        "subject": "SecDigest — {date}",
        "html": _TMPL_MOBILE_DARK_HTML,
        "article_html": _TMPL_MOBILE_DARK_ARTICLE,
        "is_builtin": 1,
    },
    {
        "name": "Mobile Light",
        "description": "Mobile-first light layout tuned for Gmail iOS — fluid width, large tap targets, preheader text.",
        "subject": "SecDigest — {date}",
        "html": _TMPL_MOBILE_LIGHT_HTML,
        "article_html": _TMPL_MOBILE_LIGHT_ARTICLE,
        "is_builtin": 1,
    },
]


def _get_conn() -> sqlite3.Connection:
    """Return the process-wide connection, opening it on first call.
    ``check_same_thread=False`` lets us share it across the request handler
    threads / scheduler thread / voice-gen thread; writes still serialise
    through ``_lock``. ``row_factory = sqlite3.Row`` gives dict-like access
    so callers can do ``row['title']`` instead of ``row[2]``."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db():
    """One-shot startup hook. Applies the schema, then runs every seeder
    and migration in order. Migrations are intentionally listed
    chronologically (oldest first) — when adding a new one, append to
    the bottom rather than reordering."""
    conn = _get_conn()
    with _lock:
        conn.executescript(SCHEMA)
        conn.commit()
        # Seeders fill empty tables on first run; safe to call always
        # because each guards against existing rows.
        _seed_config(conn)
        _seed_prompts(conn)
        _seed_email_templates(conn)
        # Forward-only migrations. Each is internally idempotent —
        # the order matters only for migrations that depend on prior ones.
        _migrate_subscriber_tokens(conn)
        _migrate_article_source(conn)
        _migrate_builtin_template_unsubscribe(conn)
        _migrate_summary_prompt(conn)
        _migrate_builtin_remove_hn_links(conn)
        _migrate_add_grid_template(conn)
        _migrate_add_mobile_templates(conn)
        _migrate_builtin_remove_hn_points(conn)
        _migrate_newsletters_kind(conn)
        _migrate_article_pins(conn)
        _migrate_article_source_name(conn)
        _migrate_subscriber_cadence(conn)
        _migrate_subscriber_confirmation(conn)
        _migrate_builtin_template_feedback(conn)
        _migrate_email_template_header(conn)
        _migrate_header_to_global(conn)


def _seed_config(conn):
    """Insert the default config_kv rows. ``INSERT OR IGNORE`` makes this a
    no-op for keys that already exist, so admin-edited values are never
    clobbered on restart."""
    for key, val in config.DB_CONFIG_DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO config_kv(key, value) VALUES (?, ?)", (key, val))
    conn.commit()


def _seed_prompts(conn):
    """Insert the built-in curation/summary prompts on a fresh DB. Skipped
    once any prompts exist so admin-edited copies aren't overwritten."""
    if conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0:
        for p in DEFAULT_PROMPTS:
            conn.execute(
                "INSERT INTO prompts(name, type, content) VALUES (?,?,?)",
                (p["name"], p["type"], p["content"])
            )
        conn.commit()


def _migrate_subscriber_tokens(conn):
    """Add unsubscribe_token column to subscribers and backfill any NULLs."""
    # Bare ``except`` because SQLite raises ``OperationalError`` when the
    # column already exists; that's the success path on already-migrated DBs.
    try:
        conn.execute("ALTER TABLE subscribers ADD COLUMN unsubscribe_token TEXT")
        conn.commit()
    except Exception:
        pass
    # Backfill any rows that ended up with NULL tokens (either because they
    # were inserted before the column existed, or via an admin path that
    # forgot to set one). One UUID per subscriber, persisted forever.
    rows = conn.execute("SELECT id FROM subscribers WHERE unsubscribe_token IS NULL").fetchall()
    for row in rows:
        conn.execute("UPDATE subscribers SET unsubscribe_token=? WHERE id=?",
                     (str(uuid.uuid4()), row[0]))
    if rows:
        conn.commit()


_OLD_SUMMARY_PROMPT = (
    "Write a concise 2-3 sentence summary for a security professional audience. "
    "Focus on: what the vulnerability/tool/threat is, who or what it affects, "
    "severity/impact, and any CVE IDs, affected versions, or mitigations if known. "
    "Be factual and direct. No fluff, no marketing language."
)
_NEW_SUMMARY_PROMPT = DEFAULT_PROMPTS[1]["content"]


def _migrate_summary_prompt(conn):
    """Update the default summary prompt if it hasn't been customised."""
    row = conn.execute(
        "SELECT id, content FROM prompts WHERE type='summary' AND name='Technical Summary Style'"
    ).fetchone()
    if row and row[1].strip() == _OLD_SUMMARY_PROMPT.strip():
        conn.execute("UPDATE prompts SET content=? WHERE id=?", (_NEW_SUMMARY_PROMPT, row[0]))
        conn.commit()


def _migrate_article_source(conn):
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN source TEXT DEFAULT 'hn'")
        conn.commit()
    except Exception:
        pass
    conn.execute("UPDATE articles SET source='manual' WHERE hn_id IS NULL AND source='hn'")
    conn.commit()


def _migrate_builtin_remove_hn_links(conn):
    """Strip <a href="{hn_url}"> discussion links from existing built-in article templates."""
    import re
    rows = conn.execute(
        "SELECT id, article_html FROM email_templates WHERE is_builtin=1"
    ).fetchall()
    changed = False
    for row in rows:
        if "{hn_url}" in row[1]:
            new_html = re.sub(r'\s*<a href="\{hn_url\}"[^>]*>[^<]*</a>', "", row[1])
            conn.execute("UPDATE email_templates SET article_html=? WHERE id=?", (new_html, row[0]))
            changed = True
    if changed:
        conn.commit()


def _migrate_newsletters_kind(conn):
    """Rebuild legacy newsletters table to add kind/period columns and drop UNIQUE(date).

    SQLite can't ALTER a UNIQUE constraint, so the standard "table rebuild"
    pattern is used: create a new table with the desired schema, copy rows
    (mapping the old single ``date`` column into kind='daily' + period_start
    /period_end of the same day), drop the original, and rename.

    Foreign keys are turned off during the rebuild because the rename would
    otherwise dangle FKs from articles/digest_articles momentarily; SQLite
    re-validates them when foreign_keys is flipped back on.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(newsletters)").fetchall()}
    if "kind" in cols:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript("""
            CREATE TABLE newsletters_new (
                id           INTEGER PRIMARY KEY,
                kind         TEXT    NOT NULL DEFAULT 'daily',
                date         TEXT    NOT NULL,
                period_start TEXT    NOT NULL,
                period_end   TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'draft',
                sent_at      TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(kind, period_start)
            );
            INSERT INTO newsletters_new(id, kind, date, period_start, period_end, status, sent_at, created_at)
                SELECT id, 'daily', date, date, date, status, sent_at, created_at FROM newsletters;
            DROP TABLE newsletters;
            ALTER TABLE newsletters_new RENAME TO newsletters;
        """)
        conn.commit()
    finally:
        # Always restore the FK pragma, even if the rebuild raised — leaving
        # it OFF would silently break referential integrity for the rest of
        # the process's lifetime.
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_article_pins(conn):
    """Add pin_weekly/pin_monthly columns to articles for older DBs."""
    for col in ("pin_weekly", "pin_monthly"):
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _migrate_article_source_name(conn):
    """Add source_name column to articles for older DBs."""
    try:
        conn.execute("ALTER TABLE articles ADD COLUMN source_name TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_subscriber_cadence(conn):
    """Add cadence column to subscribers for older DBs."""
    try:
        conn.execute("ALTER TABLE subscribers ADD COLUMN cadence TEXT NOT NULL DEFAULT 'daily'")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_subscriber_confirmation(conn):
    """Add confirmed/confirm_token columns. Existing rows are admin-added so we trust them
    and backfill confirmed=1 — only public-site signups need the double-opt-in dance."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subscribers)").fetchall()}
    added = False
    if "confirmed" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN confirmed INTEGER DEFAULT 0")
        conn.execute("UPDATE subscribers SET confirmed=1")
        added = True
    if "confirm_token" not in cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN confirm_token TEXT")
        added = True
    if added:
        conn.commit()


def _migrate_builtin_remove_hn_points(conn):
    """Strip the 'HN {hn_score} pts [· {hn_comments} comments]' meta from built-in article templates."""
    import re
    rows = conn.execute(
        "SELECT id, article_html FROM email_templates WHERE is_builtin=1"
    ).fetchall()
    # Matches the optional separator before, the points text, and the optional
    # ' · {hn_comments} comments' suffix that follows in the older templates.
    pattern = re.compile(
        r'\s*(?:&nbsp;)?&middot;(?:&nbsp;)?\s*HN\s*\{hn_score\}\s*pts'
        r'(?:\s*(?:&nbsp;)?&middot;(?:&nbsp;)?\s*\{hn_comments\}\s*comments)?'
    )
    changed = False
    for row in rows:
        if "{hn_score}" not in row[1]:
            continue
        new_html = pattern.sub("", row[1])
        if new_html != row[1]:
            conn.execute("UPDATE email_templates SET article_html=? WHERE id=?", (new_html, row[0]))
            changed = True
    if changed:
        conn.commit()


def _migrate_add_grid_template(conn):
    """Insert the 2-Column Grid template if it doesn't exist yet."""
    if conn.execute(
        "SELECT COUNT(*) FROM email_templates WHERE name='2-Column Grid'"
    ).fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO email_templates(name, description, subject, html, article_html, is_builtin) "
            "VALUES (?,?,?,?,?,?)",
            ("2-Column Grid",
             "Dark theme with articles in a 2-column card grid. Best for shorter summaries.",
             "SecDigest — {date}",
             _TMPL_GRID_HTML, _TMPL_GRID_ARTICLE, 1),
        )
        conn.commit()


def _migrate_add_mobile_templates(conn):
    """Insert the Mobile Dark and Mobile Light templates if they don't exist yet."""
    specs = [
        ("Mobile Dark",
         "Mobile-first dark layout tuned for Gmail iOS — fluid width, large tap targets, preheader text.",
         _TMPL_MOBILE_DARK_HTML, _TMPL_MOBILE_DARK_ARTICLE),
        ("Mobile Light",
         "Mobile-first light layout tuned for Gmail iOS — fluid width, large tap targets, preheader text.",
         _TMPL_MOBILE_LIGHT_HTML, _TMPL_MOBILE_LIGHT_ARTICLE),
    ]
    changed = False
    for name, desc, body, article in specs:
        existing = conn.execute(
            "SELECT COUNT(*) FROM email_templates WHERE name=?", (name,)
        ).fetchone()[0]
        if not existing:
            conn.execute(
                "INSERT INTO email_templates(name, description, subject, html, article_html, is_builtin) "
                "VALUES (?,?,?,?,?,1)",
                (name, desc, "SecDigest — {date}", body, article),
            )
            changed = True
    if changed:
        conn.commit()


def _migrate_email_template_header(conn):
    """Add header_html column to email_templates for older DBs.

    History: this column is a vestige of the original per-template header
    design. The header is now a single global value stored in config_kv (see
    `_migrate_header_to_global`), but the column stays in the schema so we
    don't have to do a destructive table-rebuild migration. New code never
    reads or writes it; the column is dead but harmless."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(email_templates)").fetchall()}
    if "header_html" in cols:
        return
    conn.execute("ALTER TABLE email_templates ADD COLUMN header_html TEXT DEFAULT ''")
    conn.commit()


def _migrate_header_to_global(conn):
    """One-shot lift of per-template header_html → global config_kv.header_html.

    The header was briefly modelled as a per-template field; we moved it to a
    single global value to keep behaviour consistent regardless of which
    template an admin picks for a given issue. If a previous build wrote a
    non-empty header_html on any template, copy the first one we find into
    the global slot so the admin doesn't lose their work, then null out the
    template column so the dead field can't shadow the real one in any UI
    that still happens to surface it."""
    # Only do work if the global value isn't already set.
    existing = conn.execute(
        "SELECT value FROM config_kv WHERE key='header_html'"
    ).fetchone()
    if existing and (existing[0] or ""):
        return
    row = conn.execute(
        "SELECT id, header_html FROM email_templates "
        "WHERE header_html IS NOT NULL AND header_html != '' "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        return
    conn.execute(
        "INSERT INTO config_kv(key,value) VALUES('header_html', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (row[1],),
    )
    conn.execute("UPDATE email_templates SET header_html='' WHERE header_html IS NOT NULL")
    conn.commit()


def _migrate_builtin_template_feedback(conn):
    """Inject the {feedback_block} placeholder above the unsubscribe footer in
    built-in templates that pre-date the feedback feature. The renderer always
    substitutes the placeholder (with empty string when feedback is disabled),
    so a missing placeholder just means the buttons never appear."""
    rows = conn.execute(
        "SELECT id, html FROM email_templates WHERE is_builtin=1"
    ).fetchall()
    for row in rows:
        if "{feedback_block}" in row[1]:
            continue
        # The unsubscribe boilerplate has been a stable sentinel across every
        # built-in template since the unsubscribe migration landed; placing the
        # placeholder right before it keeps feedback buttons visually adjacent
        # to the unsubscribe link in the footer.
        if "You're receiving this because you subscribed to SecDigest" in row[1]:
            new_html = row[1].replace(
                "You're receiving this because you subscribed to SecDigest",
                "{feedback_block}You're receiving this because you subscribed to SecDigest",
                1,
            )
            conn.execute("UPDATE email_templates SET html=? WHERE id=?", (new_html, row[0]))
    conn.commit()


def _migrate_builtin_template_unsubscribe(conn):
    """Add {unsubscribe_url} footer link to built-in templates that don't have it yet."""
    rows = conn.execute(
        "SELECT id, html FROM email_templates WHERE is_builtin=1"
    ).fetchall()
    for row in rows:
        if "{unsubscribe_url}" not in row[1]:
            new_html = row[1].replace(
                "You're receiving this because you subscribed to SecDigest.",
                "You're receiving this because you subscribed to SecDigest."
                " &nbsp;&middot;&nbsp; "
                '<a href="{unsubscribe_url}" style="color:inherit;opacity:0.7;">Unsubscribe</a>',
            )
            conn.execute("UPDATE email_templates SET html=? WHERE id=?", (new_html, row[0]))
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
    """Read a single config value. Falls back to the in-code default
    (``DB_CONFIG_DEFAULTS``) if the row is missing — keeps callers from
    needing a separate ``or ""`` everywhere."""
    row = _get_conn().execute("SELECT value FROM config_kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else config.DB_CONFIG_DEFAULTS.get(key, "")


def cfg_set(key: str, value: str):
    """Upsert a single config row. Uses ON CONFLICT to avoid the
    select-then-insert race that an explicit existence check would have."""
    with _lock:
        _get_conn().execute(
            "INSERT INTO config_kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        _get_conn().commit()


def cfg_all() -> dict:
    """Bulk fetch — used by views that need many keys at once (settings page,
    mailer pre-flight). Cheaper than N round-trips to ``cfg_get``."""
    return {r[0]: r[1] for r in _get_conn().execute("SELECT key, value FROM config_kv").fetchall()}


# ── Newsletters ───────────────────────────────────────────────────────────────

def newsletter_get_or_create(date_str: str, kind: str = "daily",
                              period_start: str | None = None,
                              period_end: str | None = None) -> dict:
    """Get or create a newsletter row. For digests, period_start/end define the window;
    for daily, all three default to date_str."""
    conn = _get_conn()
    period_start = period_start or date_str
    period_end = period_end or date_str
    # Try to read first to avoid grabbing the write lock on the common
    # "already exists" path.
    row = conn.execute(
        "SELECT * FROM newsletters WHERE kind=? AND period_start=?", (kind, period_start)
    ).fetchone()
    if row:
        return dict(row)
    # ``INSERT OR IGNORE`` covers the race where two requests both fall
    # through the read above and try to create the same (kind, period)
    # — the UNIQUE constraint makes the second insert a no-op.
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO newsletters(kind, date, period_start, period_end) VALUES(?,?,?,?)",
            (kind, date_str, period_start, period_end),
        )
        conn.commit()
    # Re-read after insert so we always return the canonical row, including
    # auto-assigned id and DEFAULTs (status='draft', created_at, etc.).
    return dict(conn.execute(
        "SELECT * FROM newsletters WHERE kind=? AND period_start=?", (kind, period_start)
    ).fetchone())


def newsletter_get_by_id(id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM newsletters WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def newsletter_get(date_str: str, kind: str = "daily") -> dict | None:
    """Look up a newsletter by its period_start (which equals date for daily)."""
    row = _get_conn().execute(
        "SELECT * FROM newsletters WHERE kind=? AND period_start=?", (kind, date_str)
    ).fetchone()
    return dict(row) if row else None


def newsletter_update(id: int, **kwargs):
    """Partial update of a newsletter row. Column names are validated
    against an allow-list to keep the dynamic-SQL build below from
    accepting arbitrary attacker-controlled identifiers — only the
    *values* are bound as params; the *column names* are interpolated."""
    if not kwargs:
        return
    allowed = {"status", "sent_at"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"newsletter_update: disallowed columns {bad}")
    fields = ", ".join(f"{k}=?" for k in kwargs)  # nosec B608 — keys pre-validated against allowlist
    with _lock:
        _get_conn().execute(f"UPDATE newsletters SET {fields} WHERE id=?", [*kwargs.values(), id])  # nosec B608
        _get_conn().commit()


def newsletter_list(limit: int = 60, kind: str | None = "daily") -> list[dict]:
    """List newsletters of the given kind, newest first. Pass kind=None for all kinds."""
    if kind is None:
        rows = _get_conn().execute(
            "SELECT * FROM newsletters ORDER BY period_start DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT * FROM newsletters WHERE kind=? ORDER BY period_start DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Articles ──────────────────────────────────────────────────────────────────

def article_get(id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM articles WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def article_insert(newsletter_id: int, hn_id: int | None, title: str, url: str,
                   hn_score: int, hn_comments: int, relevance_score: float,
                   relevance_reason: str, position: int,
                   included: int = 1, source: str = 'hn',
                   source_name: str | None = None) -> int:
    """Insert a single article row. ``INSERT OR IGNORE`` makes this safe to
    re-run after a partial fetch — the upstream dedup means duplicate URLs
    won't even reach this function, but the OR IGNORE is cheap insurance."""
    # Synthesise the HN comments URL only when we have an hn_id; RSS-only
    # rows leave it NULL so we can tell the source apart from the URL field alone.
    hn_url = f"https://news.ycombinator.com/item?id={hn_id}" if hn_id else None
    with _lock:
        cur = _get_conn().execute(
            """INSERT OR IGNORE INTO articles
               (newsletter_id, hn_id, title, url, hn_url, hn_score, hn_comments,
                relevance_score, relevance_reason, position, included, source, source_name)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (newsletter_id, hn_id, title, url,
             hn_url,
             hn_score, hn_comments, relevance_score, relevance_reason, position,
             included, source, source_name),
        )
        _get_conn().commit()
        return cur.lastrowid


def article_list(newsletter_id: int) -> list[dict]:
    """Articles for one newsletter, ordered by curator position with
    relevance as the tiebreaker. Position is operator-set via drag-reorder
    in the UI; relevance is the LLM/keyword score from the fetcher."""
    rows = _get_conn().execute(
        "SELECT * FROM articles WHERE newsletter_id=? ORDER BY position ASC, relevance_score DESC",
        (newsletter_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def article_update(id: int, **kwargs):
    """Partial update with column allow-list — same pattern as
    ``newsletter_update``. Any unknown column name raises rather than
    silently being ignored, so a typo in caller code surfaces immediately."""
    if not kwargs:
        return
    allowed = {"summary", "included", "title", "url", "relevance_score", "relevance_reason", "position"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"article_update: disallowed columns {bad}")
    fields = ", ".join(f"{k}=?" for k in kwargs)  # nosec B608 — keys pre-validated against allowlist
    with _lock:
        _get_conn().execute(f"UPDATE articles SET {fields} WHERE id=?", [*kwargs.values(), id])  # nosec B608
        _get_conn().commit()


def article_reorder(newsletter_id: int, ordered_ids: list[int]):
    """Apply a drag-reorder by writing a new position to each row.
    The ``newsletter_id`` filter prevents an attacker from rewriting
    positions on articles they don't own (the route doesn't check this
    itself, so we enforce it in the SQL)."""
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


def article_count(newsletter_id: int) -> int:
    row = _get_conn().execute(
        "SELECT COUNT(*) FROM articles WHERE newsletter_id=?", (newsletter_id,)
    ).fetchone()
    return row[0] if row else 0


def article_auto_select(newsletter_id: int, top_n: int):
    """Mark top_n articles by relevance as included=1, rest as included=0."""
    articles = article_list(newsletter_id)
    sorted_arts = sorted(articles, key=lambda a: a.get("relevance_score", 0), reverse=True)
    with _lock:
        for i, a in enumerate(sorted_arts):
            _get_conn().execute(
                "UPDATE articles SET included=? WHERE id=?",
                (1 if i < top_n else 0, a["id"])
            )
        _get_conn().commit()


def article_all_hn_ids() -> set[int]:
    rows = _get_conn().execute(
        "SELECT hn_id FROM articles WHERE hn_id IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows}


def article_all_urls() -> set[str]:
    """Every URL we've ever stored, used by the fetcher's global dedup.
    Returning a set lets callers do O(1) ``url in seen`` checks."""
    rows = _get_conn().execute(
        "SELECT url FROM articles WHERE url IS NOT NULL AND url != ''"
    ).fetchall()
    return {r[0] for r in rows}


def article_set_pin(article_id: int, period: str, pinned: bool):
    """period is 'weekly' or 'monthly'."""
    # Strict allow-list before any string interpolation. Without this guard,
    # ``period`` would be a SQL-injection vector (it's used to build a
    # column name, which can't be parameterised in standard SQL).
    if period not in ("weekly", "monthly"):
        raise ValueError(f"article_set_pin: bad period {period!r}")
    col = f"pin_{period}"  # nosec B608 — period validated to "weekly"/"monthly" above
    with _lock:
        _get_conn().execute(f"UPDATE articles SET {col}=? WHERE id=?", (1 if pinned else 0, article_id))  # nosec B608
        _get_conn().commit()


def articles_in_period(period_start: str, period_end: str) -> list[dict]:
    """All articles from daily newsletters whose date falls within [period_start, period_end]."""
    rows = _get_conn().execute(
        """SELECT a.* FROM articles a
           JOIN newsletters n ON n.id = a.newsletter_id
           WHERE n.kind='daily' AND n.date >= ? AND n.date <= ?
           ORDER BY n.date DESC, a.relevance_score DESC""",
        (period_start, period_end),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Digest articles (weekly / monthly join) ───────────────────────────────────

def digest_article_list(digest_id: int) -> list[dict]:
    """Articles in a digest, in display order. Each row carries the article fields plus
    digest-specific position/included from the join table."""
    rows = _get_conn().execute(
        """SELECT a.*, da.position AS d_position, da.included AS d_included,
                  n.date AS source_date
           FROM digest_articles da
           JOIN articles a ON a.id = da.article_id
           JOIN newsletters n ON n.id = a.newsletter_id
           WHERE da.digest_id = ?
           ORDER BY da.position ASC""",
        (digest_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Overlay the digest's position/included onto the article view used by the renderer
        d["position"] = d.pop("d_position")
        d["included"] = d.pop("d_included")
        out.append(d)
    return out


def digest_article_add(digest_id: int, article_id: int, position: int = 0, included: int = 1):
    with _lock:
        _get_conn().execute(
            """INSERT OR IGNORE INTO digest_articles(digest_id, article_id, position, included)
               VALUES (?,?,?,?)""",
            (digest_id, article_id, position, included),
        )
        _get_conn().commit()


def digest_article_remove(digest_id: int, article_id: int):
    with _lock:
        _get_conn().execute(
            "DELETE FROM digest_articles WHERE digest_id=? AND article_id=?",
            (digest_id, article_id),
        )
        _get_conn().commit()


def digest_article_toggle(digest_id: int, article_id: int):
    """Flip included 0↔1 for one article in a digest."""
    row = _get_conn().execute(
        "SELECT included FROM digest_articles WHERE digest_id=? AND article_id=?",
        (digest_id, article_id),
    ).fetchone()
    if not row:
        return
    with _lock:
        _get_conn().execute(
            "UPDATE digest_articles SET included=? WHERE digest_id=? AND article_id=?",
            (0 if row[0] else 1, digest_id, article_id),
        )
        _get_conn().commit()


def digest_article_reorder(digest_id: int, ordered_ids: list[int]):
    with _lock:
        for pos, aid in enumerate(ordered_ids):
            _get_conn().execute(
                "UPDATE digest_articles SET position=? WHERE digest_id=? AND article_id=?",
                (pos, digest_id, aid),
            )
        _get_conn().commit()


def digest_article_count(digest_id: int) -> int:
    row = _get_conn().execute(
        "SELECT COUNT(*) FROM digest_articles WHERE digest_id=?", (digest_id,)
    ).fetchone()
    return row[0] if row else 0


def digest_seed(digest_id: int, kind: str, period_start: str, period_end: str, top_n: int):
    """Populate digest_articles for a fresh digest:
       1. every article pinned for this period (included=1)
       2. then top-N-by-relevance from remaining curated daily articles, until reaching top_n total
    """
    if kind not in ("weekly", "monthly"):
        raise ValueError(f"digest_seed: bad kind {kind!r}")
    # Pick the right pin column based on kind. Same allow-list reasoning
    # as ``article_set_pin`` — column name can't be parameterised.
    pin_col = "pin_weekly" if kind == "weekly" else "pin_monthly"

    pool = articles_in_period(period_start, period_end)
    # Daily curator's `included` flag means "in the daily newsletter"; we use it as a
    # quality gate (curator already vetted these). Pinned articles bypass the gate.
    pinned = [a for a in pool if a.get(pin_col, 0)]
    pinned_ids = {a["id"] for a in pinned}
    # Candidates = anything in the period that isn't already pinned and
    # was included in its source day. Sorted by relevance so the highest-
    # scoring stories fill the remaining slots.
    candidates = [a for a in pool
                  if a["id"] not in pinned_ids
                  and a.get("included", 1)]
    candidates.sort(key=lambda a: a.get("relevance_score", 0), reverse=True)

    # Pinned first (position by source date desc — matches the SQL ORDER), then top-N fillers
    # ``max(0, ...)`` defends against a degenerate case where there are
    # already more pinned articles than the requested cap.
    selected = list(pinned) + candidates[: max(0, top_n - len(pinned))]

    with _lock:
        conn = _get_conn()
        # `with conn:` runs the block inside an implicit transaction, committing
        # on clean exit and rolling back on any exception. Without this, a crash
        # mid-loop leaves the digest half-populated — the count is non-zero so
        # the next visit skips re-seeding, and the user sees a partial digest
        # indefinitely.
        with conn:
            # Wipe any previous seed first so re-seeding is idempotent and
            # the curator can hit "refresh" to discard manual reordering.
            conn.execute("DELETE FROM digest_articles WHERE digest_id=?", (digest_id,))
            for pos, a in enumerate(selected):
                conn.execute(
                    "INSERT INTO digest_articles(digest_id, article_id, position, included) "
                    "VALUES (?,?,?,1)",
                    (digest_id, a["id"], pos),
                )


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
    allowed = {"name", "content", "active"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"prompt_update: disallowed columns {bad}")
    fields = ", ".join(f"{k}=?" for k in kwargs)  # nosec B608 — keys pre-validated against allowlist
    with _lock:
        _get_conn().execute(f"UPDATE prompts SET {fields} WHERE id=?", [*kwargs.values(), id])  # nosec B608
        _get_conn().commit()


def prompt_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM prompts WHERE id=?", (id,))
        _get_conn().commit()


# ── Subscribers ───────────────────────────────────────────────────────────────

def subscriber_list() -> list[dict]:
    return [dict(r) for r in _get_conn().execute("SELECT * FROM subscribers ORDER BY id").fetchall()]


def subscriber_create(email: str, name: str = "") -> dict | None:
    """Admin-side direct create — bypasses double-opt-in since the admin is trusted."""
    try:
        with _lock:
            cur = _get_conn().execute(
                "INSERT INTO subscribers(email, name, confirmed, unsubscribe_token) VALUES(?,?,1,?)",
                (email, name, str(uuid.uuid4())),
            )
            _get_conn().commit()
        return dict(_get_conn().execute("SELECT * FROM subscribers WHERE id=?", (cur.lastrowid,)).fetchone())
    except Exception:
        return None


def subscriber_update(id: int, **kwargs):
    if not kwargs:
        return
    allowed = {"active", "name", "cadence"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"subscriber_update: disallowed columns {bad}")
    if "cadence" in kwargs and kwargs["cadence"] not in ("daily", "weekly", "monthly"):
        raise ValueError(f"subscriber_update: bad cadence {kwargs['cadence']!r}")
    fields = ", ".join(f"{k}=?" for k in kwargs)  # nosec B608 — keys pre-validated against allowlist
    with _lock:
        _get_conn().execute(f"UPDATE subscribers SET {fields} WHERE id=?", [*kwargs.values(), id])  # nosec B608
        _get_conn().commit()


def subscriber_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM subscribers WHERE id=?", (id,))
        _get_conn().commit()


def subscriber_active(cadence: str | None = None) -> list[dict]:
    """Active subscribers, optionally filtered to a single cadence."""
    if cadence is None:
        rows = _get_conn().execute("SELECT * FROM subscribers WHERE active=1").fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT * FROM subscribers WHERE active=1 AND cadence=?", (cadence,)
        ).fetchall()
    return [dict(r) for r in rows]


def subscriber_get_by_token(token: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM subscribers WHERE unsubscribe_token=?", (token,)
    ).fetchone()
    return dict(row) if row else None


def subscriber_unsubscribe_by_token(token: str):
    with _lock:
        _get_conn().execute(
            "UPDATE subscribers SET active=0 WHERE unsubscribe_token=?", (token,)
        )
        _get_conn().commit()


def subscriber_get_by_email(email: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM subscribers WHERE email=?", (email.lower(),)
    ).fetchone()
    return dict(row) if row else None


def subscriber_create_pending(email: str, cadence: str, confirm_token: str) -> dict | None:
    """Insert a new subscriber in the pending state (active=0, confirmed=0). Returns the row,
    or None on email-uniqueness conflict — callers should check subscriber_get_by_email first."""
    if cadence not in ("daily", "weekly", "monthly"):
        raise ValueError(f"subscriber_create_pending: bad cadence {cadence!r}")
    try:
        with _lock:
            # Email is lowercased so subscribers don't get duplicated on
            # case differences. ``unsubscribe_token`` is minted up-front so
            # it's available immediately when the user later confirms.
            cur = _get_conn().execute(
                """INSERT INTO subscribers
                   (email, name, active, cadence, confirmed, confirm_token, unsubscribe_token)
                   VALUES (?, '', 0, ?, 0, ?, ?)""",
                (email.lower(), cadence, confirm_token, str(uuid.uuid4())),
            )
            _get_conn().commit()
        return dict(_get_conn().execute(
            "SELECT * FROM subscribers WHERE id=?", (cur.lastrowid,)
        ).fetchone())
    except Exception:
        # UNIQUE(email) violation lands here. Returning None keeps the
        # public-site flow from leaking "this email is already subscribed"
        # — important to avoid a free email-existence oracle.
        return None


def subscriber_set_confirm_token(id: int, token: str | None):
    """Rotate (or clear) the confirm token for a subscriber row."""
    with _lock:
        _get_conn().execute(
            "UPDATE subscribers SET confirm_token=? WHERE id=?", (token, id)
        )
        _get_conn().commit()


def subscriber_confirm(token: str) -> dict | None:
    """Mark a subscriber confirmed via their single-use confirm token. Returns the row
    on success, None if the token is unknown / already used."""
    if not token:
        return None
    row = _get_conn().execute(
        "SELECT * FROM subscribers WHERE confirm_token=?", (token,)
    ).fetchone()
    if not row:
        return None
    # Confirming flips three flags atomically: confirmed=1 (DOI satisfied),
    # active=1 (will receive newsletters), and confirm_token=NULL (one-shot
    # token spent — the same link can't be replayed to re-activate after
    # an unsubscribe).
    with _lock:
        _get_conn().execute(
            "UPDATE subscribers SET confirmed=1, active=1, confirm_token=NULL WHERE id=?",
            (row["id"],),
        )
        _get_conn().commit()
    return dict(_get_conn().execute(
        "SELECT * FROM subscribers WHERE id=?", (row["id"],)
    ).fetchone())


# ── Feedback (signal / noise) ────────────────────────────────────────────────

def feedback_record(subscriber_id: int, newsletter_id: int, vote: str) -> None:
    """Upsert a vote — a subscriber can change their mind on a given newsletter,
    but only the latest vote counts. The (subscriber_id, newsletter_id) UNIQUE
    constraint enforces one row per pair; ON CONFLICT swaps in the new vote."""
    if vote not in ("signal", "noise"):
        raise ValueError(f"feedback_record: bad vote {vote!r}")
    with _lock:
        _get_conn().execute(
            "INSERT INTO feedback(subscriber_id, newsletter_id, vote) VALUES (?,?,?) "
            "ON CONFLICT(subscriber_id, newsletter_id) "
            "DO UPDATE SET vote=excluded.vote, created_at=CURRENT_TIMESTAMP",
            (subscriber_id, newsletter_id, vote),
        )
        _get_conn().commit()


def feedback_counts_by_subscriber() -> dict[int, dict[str, int]]:
    """One pass over the feedback table, grouped by subscriber. Returns
    {sub_id: {'signal': int, 'noise': int}}; subscribers with no feedback are
    absent from the result (callers should default to zero)."""
    rows = _get_conn().execute(
        "SELECT subscriber_id, vote, COUNT(*) AS n FROM feedback "
        "GROUP BY subscriber_id, vote"
    ).fetchall()
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["subscriber_id"], {"signal": 0, "noise": 0})[r["vote"]] = r["n"]
    return out


def feedback_for_newsletter(newsletter_id: int) -> dict[str, int]:
    """Aggregate signal/noise totals for a single newsletter."""
    rows = _get_conn().execute(
        "SELECT vote, COUNT(*) AS n FROM feedback WHERE newsletter_id=? GROUP BY vote",
        (newsletter_id,),
    ).fetchall()
    out = {"signal": 0, "noise": 0}
    for r in rows:
        out[r["vote"]] = r["n"]
    return out


# ── Voice audio (ElevenLabs TTS → S3) ────────────────────────────────────────

def voice_audio_get(newsletter_id: int) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM voice_audio WHERE newsletter_id=?", (newsletter_id,)
    ).fetchone()
    return dict(row) if row else None


def voice_audio_upsert(newsletter_id: int, **fields):
    """Insert or update the row. The status column has a CHECK constraint, so
    callers passing a bogus status will trip a clean SQLite error instead of
    silently corrupting state."""
    allowed = {"status", "s3_key", "duration_sec", "voice_text", "error"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"voice_audio_upsert: disallowed columns {bad}")
    if not fields:
        return
    # Build the SQL dynamically because callers pass arbitrary subsets of
    # fields. Column names come from the allow-list above so this can't
    # be SQL-injected; values are still bound as parameters.
    cols = ", ".join(fields)  # nosec B608 — keys pre-validated against allowlist
    placeholders = ", ".join("?" * len(fields))
    sets = ", ".join(f"{k}=excluded.{k}" for k in fields)
    with _lock:
        _get_conn().execute(
            f"INSERT INTO voice_audio(newsletter_id, {cols}) "  # nosec B608 — keys pre-validated against allowlist
            f"VALUES (?, {placeholders}) "
            # Touch updated_at on every upsert so the polling UI can detect
            # stalled "generating" states (no movement for >N minutes).
            f"ON CONFLICT(newsletter_id) DO UPDATE SET {sets}, "
            f"updated_at=CURRENT_TIMESTAMP",
            (newsletter_id, *fields.values()),
        )
        _get_conn().commit()


def voice_audio_clear(newsletter_id: int):
    """Drop the row so a fresh generation starts from a clean slate."""
    with _lock:
        _get_conn().execute(
            "DELETE FROM voice_audio WHERE newsletter_id=?", (newsletter_id,)
        )
        _get_conn().commit()


def newsletter_get_voice_enabled(newsletter_id: int) -> bool:
    """Per-newsletter render flag. Mirrors the TOC pattern: a config_kv row
    keyed by 'voice_<id>' lets us reuse the existing settings plumbing without
    bloating the newsletters table."""
    row = _get_conn().execute(
        "SELECT value FROM config_kv WHERE key=?", (f"voice_{newsletter_id}",)
    ).fetchone()
    return row[0] == "1" if row else False


def newsletter_set_voice_enabled(newsletter_id: int, enabled: bool):
    cfg_set(f"voice_{newsletter_id}", "1" if enabled else "0")


# ── LLM Audit Log ─────────────────────────────────────────────────────────────

def audit_log(operation: str, model: str, input_tokens: int, output_tokens: int,
              cached_tokens: int, article_id: int | None, result_snippet: str):
    """Record one Claude API call. The snippet is truncated at 500 chars
    so an unusually verbose summary doesn't bloat the audit table — the
    full text is on the article row anyway, this is just for at-a-glance
    inspection on the audit page."""
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
    """Most recent audit rows for the audit page. Ordered by id rather
    than timestamp so ties resolve deterministically (timestamps have
    1-second resolution; bulk runs cluster on the same second)."""
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


def email_template_create(name: str, description: str, subject: str, html: str,
                            article_html: str) -> dict:
    with _lock:
        cur = _get_conn().execute(
            "INSERT INTO email_templates(name, description, subject, html, article_html) "
            "VALUES (?,?,?,?,?)",
            (name, description, subject, html, article_html),
        )
        _get_conn().commit()
    return dict(_get_conn().execute("SELECT * FROM email_templates WHERE id=?", (cur.lastrowid,)).fetchone())


def email_template_update(id: int, **kwargs):
    if not kwargs:
        return
    # header_html is intentionally NOT in this allowlist — it lives in
    # config_kv now (see _migrate_header_to_global). Adding it back here
    # would re-fork the header content per-template and undo the lift.
    allowed = {"name", "description", "subject", "html", "article_html"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"email_template_update: disallowed columns {bad}")
    fields = ", ".join(f"{k}=?" for k in kwargs)  # nosec B608 — keys pre-validated against allowlist
    with _lock:
        _get_conn().execute(f"UPDATE email_templates SET {fields} WHERE id=?", [*kwargs.values(), id])  # nosec B608
        _get_conn().commit()


def email_template_delete(id: int):
    """Delete a custom template. The ``is_builtin=0`` filter protects the
    six bundled templates from accidental deletion via the API — they're
    re-created on every startup anyway, but that's not a great UX surprise."""
    with _lock:
        _get_conn().execute("DELETE FROM email_templates WHERE id=? AND is_builtin=0", (id,))
        _get_conn().commit()


# The next four pairs (template_id, subject, header, toc) all follow the
# same pattern: per-newsletter overrides stored as ``<key>_<id>`` rows in
# config_kv rather than dedicated columns on the newsletters table. Trade-off:
# we get per-issue settings without growing the table; we lose typing and
# referential integrity. For sparse, optional flags this is the right call.

def newsletter_get_template_id(newsletter_id: int) -> int | None:
    row = _get_conn().execute(
        "SELECT value FROM config_kv WHERE key=?", (f"tmpl_{newsletter_id}",)
    ).fetchone()
    return int(row[0]) if row else None


def newsletter_set_template_id(newsletter_id: int, template_id: int):
    cfg_set(f"tmpl_{newsletter_id}", str(template_id))


def newsletter_get_subject(newsletter_id: int) -> str | None:
    """Subject override for a single issue. Returns None when the issue
    uses the template's default subject (which is the common case)."""
    row = _get_conn().execute(
        "SELECT value FROM config_kv WHERE key=?", (f"subject_{newsletter_id}",)
    ).fetchone()
    return row[0] if row else None


def newsletter_set_subject(newsletter_id: int, subject: str):
    cfg_set(f"subject_{newsletter_id}", subject)


def newsletter_get_header(newsletter_id: int) -> bool:
    """Per-newsletter header render flag. Stored under 'header_<id>' in
    config_kv to keep parity with the TOC + voice toggles."""
    row = _get_conn().execute(
        "SELECT value FROM config_kv WHERE key=?", (f"header_{newsletter_id}",)
    ).fetchone()
    return row[0] == "1" if row else False


def newsletter_set_header(newsletter_id: int, enabled: bool):
    cfg_set(f"header_{newsletter_id}", "1" if enabled else "0")


def newsletter_get_toc(newsletter_id: int) -> bool:
    row = _get_conn().execute(
        "SELECT value FROM config_kv WHERE key=?", (f"toc_{newsletter_id}",)
    ).fetchone()
    return row[0] == "1" if row else False


def newsletter_set_toc(newsletter_id: int, enabled: bool):
    cfg_set(f"toc_{newsletter_id}", "1" if enabled else "0")


# ── RSS Feeds ─────────────────────────────────────────────────────────────────

def rss_feed_list() -> list[dict]:
    rows = _get_conn().execute("SELECT * FROM rss_feeds ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def rss_feed_active() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM rss_feeds WHERE active=1"
    ).fetchall()
    return [dict(r) for r in rows]


def rss_feed_create(url: str, name: str, max_articles: int = 5) -> dict | None:
    try:
        with _lock:
            cur = _get_conn().execute(
                "INSERT INTO rss_feeds(url, name, max_articles) VALUES(?,?,?)",
                (url, name, max_articles),
            )
            _get_conn().commit()
        return dict(_get_conn().execute("SELECT * FROM rss_feeds WHERE id=?", (cur.lastrowid,)).fetchone())
    except Exception:
        return None


def rss_feed_update(id: int, **kwargs):
    if not kwargs:
        return
    allowed = {"active", "name", "max_articles", "url"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"rss_feed_update: disallowed columns {bad}")
    fields = ", ".join(f"{k}=?" for k in kwargs)  # nosec B608 — keys pre-validated against allowlist
    with _lock:
        _get_conn().execute(f"UPDATE rss_feeds SET {fields} WHERE id=?", [*kwargs.values(), id])  # nosec B608
        _get_conn().commit()


def rss_feed_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM rss_feeds WHERE id=?", (id,))
        _get_conn().commit()

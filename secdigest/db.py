"""All SQLite operations. Single module — import this everywhere you need data access."""
import sqlite3
import threading
import uuid
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
    source          TEXT    DEFAULT 'hn',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE IF NOT EXISTS subscribers (
    id                INTEGER PRIMARY KEY,
    email             TEXT    UNIQUE NOT NULL,
    name              TEXT    DEFAULT '',
    active            INTEGER DEFAULT 1,
    unsubscribe_token TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
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
You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
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
You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
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
You're receiving this because you subscribed to SecDigest. &nbsp;&middot;&nbsp;
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
You're receiving this because you subscribed to SecDigest.<br>
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
You're receiving this because you subscribed to SecDigest.<br>
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
        _migrate_subscriber_tokens(conn)
        _migrate_article_source(conn)
        _migrate_builtin_template_unsubscribe(conn)
        _migrate_summary_prompt(conn)
        _migrate_builtin_remove_hn_links(conn)
        _migrate_add_grid_template(conn)
        _migrate_add_mobile_templates(conn)
        _migrate_builtin_remove_hn_points(conn)


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


def _migrate_subscriber_tokens(conn):
    """Add unsubscribe_token column to subscribers and backfill any NULLs."""
    try:
        conn.execute("ALTER TABLE subscribers ADD COLUMN unsubscribe_token TEXT")
        conn.commit()
    except Exception:
        pass
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
    allowed = {"status", "sent_at"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"newsletter_update: disallowed columns {bad}")
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
                   relevance_reason: str, position: int,
                   included: int = 1, source: str = 'hn') -> int:
    hn_url = f"https://news.ycombinator.com/item?id={hn_id}" if hn_id else None
    with _lock:
        cur = _get_conn().execute(
            """INSERT OR IGNORE INTO articles
               (newsletter_id, hn_id, title, url, hn_url, hn_score, hn_comments,
                relevance_score, relevance_reason, position, included, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (newsletter_id, hn_id, title, url,
             hn_url,
             hn_score, hn_comments, relevance_score, relevance_reason, position,
             included, source),
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
    allowed = {"summary", "included", "title", "url", "relevance_score", "relevance_reason", "position"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"article_update: disallowed columns {bad}")
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
    rows = _get_conn().execute(
        "SELECT url FROM articles WHERE url IS NOT NULL AND url != ''"
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
    allowed = {"name", "content", "active"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"prompt_update: disallowed columns {bad}")
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
                "INSERT INTO subscribers(email, name, unsubscribe_token) VALUES(?,?,?)",
                (email, name, str(uuid.uuid4())),
            )
            _get_conn().commit()
        return dict(_get_conn().execute("SELECT * FROM subscribers WHERE id=?", (cur.lastrowid,)).fetchone())
    except Exception:
        return None


def subscriber_update(id: int, **kwargs):
    if not kwargs:
        return
    allowed = {"active", "name"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"subscriber_update: disallowed columns {bad}")
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
    allowed = {"name", "description", "subject", "html", "article_html"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"email_template_update: disallowed columns {bad}")
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
    fields = ", ".join(f"{k}=?" for k in kwargs)
    with _lock:
        _get_conn().execute(f"UPDATE rss_feeds SET {fields} WHERE id=?", [*kwargs.values(), id])
        _get_conn().commit()


def rss_feed_delete(id: int):
    with _lock:
        _get_conn().execute("DELETE FROM rss_feeds WHERE id=?", (id,))
        _get_conn().commit()

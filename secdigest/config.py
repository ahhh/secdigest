import os
from pathlib import Path

# Project root is two levels up from this file (secdigest/config.py → secdigest/ → root)
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH = str(DATA_DIR / "secdigest.db")

DEFAULT_PASSWORD_HASH = os.environ.get("PASSWORD_HASH", "")

# DB-backed config keys — these can be edited at runtime via the Settings page.
# Values here are the initial defaults written on first run.
DB_CONFIG_DEFAULTS = {
    "smtp_host":     os.environ.get("SMTP_HOST", ""),
    "smtp_port":     os.environ.get("SMTP_PORT", "587"),
    "smtp_user":     os.environ.get("SMTP_USER", ""),
    "smtp_pass":     os.environ.get("SMTP_PASS", ""),
    "smtp_from":     os.environ.get("SMTP_FROM", "SecDigest <noreply@example.com>"),
    "fetch_time":    os.environ.get("FETCH_TIME", "00:00"),
    "hn_min_score":  os.environ.get("HN_MIN_SCORE", "50"),
    "max_articles":  os.environ.get("MAX_ARTICLES", "15"),
    "auto_send":     "0",
    "password_hash": DEFAULT_PASSWORD_HASH,
}

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Env-level config (can't be changed at runtime)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH = str(DATA_DIR / "secdigest.db")

DEFAULT_PASSWORD_HASH = os.environ.get("PASSWORD_HASH", "")

# DB-backed config keys with env fallbacks
DB_CONFIG_DEFAULTS = {
    "smtp_host":     os.environ.get("SMTP_HOST", ""),
    "smtp_port":     os.environ.get("SMTP_PORT", "587"),
    "smtp_user":     os.environ.get("SMTP_USER", ""),
    "smtp_pass":     os.environ.get("SMTP_PASS", ""),
    "smtp_from":     os.environ.get("SMTP_FROM", "SecDigest <noreply@example.com>"),
    "fetch_time":    os.environ.get("FETCH_TIME", "07:00"),
    "hn_min_score":  os.environ.get("HN_MIN_SCORE", "50"),
    "max_articles":  os.environ.get("MAX_ARTICLES", "15"),
    "auto_send":     "0",
    "password_hash": DEFAULT_PASSWORD_HASH,
}

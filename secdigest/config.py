"""Centralised process-level configuration.

Two layers of config exist in this app:

1. **Process config (this module)** — values that don't change without a
   restart: secrets, file paths, host/port, TLS settings. Sourced from
   environment variables (with an optional ``.env`` file in dev). These
   are imported as plain module attributes (``config.SECRET_KEY`` etc.).

2. **DB-backed config (``DB_CONFIG_DEFAULTS`` below + ``db.cfg_get``)** —
   values the operator can change at runtime from the Settings page:
   SMTP creds, fetch time, HN thresholds, voice/TTS keys, etc. The
   defaults dict is seeded into the ``config_kv`` table on first run.

Keep secrets out of the repo: SECRET_KEY, ANTHROPIC_API_KEY, SMTP_PASS
should always come from the environment, never be checked in.
"""
import os
from pathlib import Path

# Project root is two levels up from this file (secdigest/config.py → secdigest/ → root)
PROJECT_ROOT = Path(__file__).parent.parent

# Load .env from project root if present. Real env vars take precedence (override=False),
# so production deployments using systemd's EnvironmentFile or Docker --env keep working.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    # python-dotenv is a dev convenience; in production env vars are
    # injected by systemd/Docker so the import doesn't have to succeed.
    pass

# All persistent state (the SQLite DB, generated files) lives here. We
# create the directory eagerly so the first DB connection doesn't fail
# on a fresh checkout.
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# SECRET_KEY: signs sessions and is the input to crypto.py for at-rest
# settings encryption. Rotating it logs everyone out AND breaks every
# encrypted SMTP/API-key value in the DB — change with care.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
# Anthropic API key for Claude calls (scoring + summarising).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DB_PATH = str(DATA_DIR / "secdigest.db")

# ── Public site (separate uvicorn instance) ─────────────────────────────────
# Setting PUBLIC_SITE_ENABLED=1 starts a second FastAPI app on PUBLIC_PORT for
# the landing page + subscribe/confirm/unsubscribe flow. PUBLIC_BASE_URL is the
# public-facing URL inserted into confirmation/unsubscribe links — set this to
# whatever the public site is reachable at (e.g. https://secdigest.example.com).
PUBLIC_SITE_ENABLED = os.environ.get("PUBLIC_SITE_ENABLED", "0") == "1"
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "0.0.0.0")
PUBLIC_PORT = int(os.environ.get("PUBLIC_PORT", "8000"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")

# ── TLS (uvicorn-direct HTTPS) ──────────────────────────────────────────────
# Defaults to ON. When enabled, run.py passes the cert+key paths through to
# uvicorn so both the admin and public apps serve HTTPS directly. To disable
# (e.g. when nginx terminates TLS in front, or in dev) set TLS_ENABLED=0.
#
# Cert resolution: if TLS_CERTFILE+TLS_KEYFILE are set, those are used as-is.
# Otherwise TLS_DOMAIN is used to derive the standard Let's Encrypt layout:
#     /etc/letsencrypt/live/<TLS_DOMAIN>/fullchain.pem
#     /etc/letsencrypt/live/<TLS_DOMAIN>/privkey.pem
# Cert generation is out of scope — run certbot yourself before turning this on.
TLS_ENABLED = os.environ.get("TLS_ENABLED", "1") == "1"
TLS_DOMAIN = os.environ.get("TLS_DOMAIN", "")
TLS_CERTFILE = os.environ.get("TLS_CERTFILE", "")
TLS_KEYFILE = os.environ.get("TLS_KEYFILE", "")
TLS_LETSENCRYPT_DIR = "/etc/letsencrypt/live"


def resolve_tls_paths() -> tuple[str, str]:
    """Compute the (certfile, keyfile) pair uvicorn should use.

    Priority:
      1. Explicit TLS_CERTFILE / TLS_KEYFILE (both must be set together)
      2. TLS_DOMAIN → /etc/letsencrypt/live/<domain>/fullchain.pem + privkey.pem
      3. ("", "") if neither configured — caller must validate.
    """
    # Explicit overrides win — useful when certs live somewhere unusual,
    # or when running with a self-signed cert in staging.
    if TLS_CERTFILE and TLS_KEYFILE:
        return TLS_CERTFILE, TLS_KEYFILE
    # Otherwise infer the standard certbot/Let's Encrypt layout from the domain.
    if TLS_DOMAIN:
        base = f"{TLS_LETSENCRYPT_DIR}/{TLS_DOMAIN}"
        return f"{base}/fullchain.pem", f"{base}/privkey.pem"
    # Caller must check: empty pair signals "TLS requested but not configured".
    return "", ""


def validate_tls_config() -> tuple[str, str] | None:
    """Validate TLS configuration at startup. Returns the resolved (cert, key)
    pair when TLS is enabled and ready; returns None when TLS is disabled.
    Raises RuntimeError with an actionable message when TLS is enabled but
    misconfigured — better to fail loudly than silently fall back to HTTP."""
    # Disabled path is intentional — admins terminating TLS at nginx
    # should set TLS_ENABLED=0 and not be bothered by cert validation here.
    if not TLS_ENABLED:
        return None

    # All three error branches below raise with a hint that tells the
    # operator exactly what to do next, instead of a stack trace from
    # uvicorn 30 lines later.
    cert, key = resolve_tls_paths()
    if not cert or not key:
        raise RuntimeError(
            "TLS_ENABLED=1 but no certificate paths configured.\n"
            "  • Set TLS_DOMAIN=<domain> to use the Let's Encrypt layout at "
            f"{TLS_LETSENCRYPT_DIR}/<domain>/, or\n"
            "  • Set TLS_CERTFILE=<path> and TLS_KEYFILE=<path> for an "
            "explicit cert pair.\n"
            "  • To run plain HTTP (dev, or behind a TLS-terminating proxy) "
            "set TLS_ENABLED=0."
        )

    # Existence/readability check up front so we don't fail mid-startup
    # when uvicorn tries to load the cert pair.
    if not Path(cert).is_file():
        raise RuntimeError(
            f"TLS_CERTFILE not readable at {cert}.\n"
            "Generate certs first (e.g. `certbot certonly --standalone -d "
            f"{TLS_DOMAIN or '<domain>'}`) or unset TLS_ENABLED."
        )
    if not Path(key).is_file():
        raise RuntimeError(
            f"TLS_KEYFILE not readable at {key}.\n"
            "Check filesystem permissions — Let's Encrypt private keys are "
            "typically only readable by root."
        )
    return cert, key


# Bootstrap admin password hash. If PASSWORD_HASH is set in the env, it
# becomes the initial value of the DB-backed ``password_hash`` setting on
# first run; the operator can later change it via the Settings page.
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
    "hn_pool_min":   os.environ.get("HN_POOL_MIN", "10"),
    "max_articles":  os.environ.get("MAX_ARTICLES", "15"),
    "max_curator_articles": os.environ.get("MAX_CURATOR_ARTICLES", "10"),
    "relevance_threshold":  os.environ.get("RELEVANCE_THRESHOLD", "5.0"),
    "base_url":      os.environ.get("BASE_URL", "http://localhost:8000"),
    "auto_send":     "0",
    "feedback_enabled": "1",
    # Global newsletter header — same markup gets injected into every
    # issue whose per-newsletter "Include header" toggle is on. Lives in
    # config_kv (not on the template) so it doesn't fork across templates.
    "header_html":   "",
    # Voice summaries (ElevenLabs TTS → S3). Disabled by default; the Settings
    # page is the source of truth. The keys are seeded as empty strings so that
    # cfg_get returns "" rather than None when no value is set.
    "voice_summary_enabled": "0",
    "elevenlabs_api_key":    "",
    "elevenlabs_voice_id":   "21m00Tcm4TlvDq8ikWAM",  # 'Rachel' — free-tier default
    "elevenlabs_model":      "eleven_turbo_v2_5",
    # Narration speed multiplier passed to voice_settings.speed. 1.0 is the
    # voice's natural cadence; 1.1 is ~10% faster and the sweet spot for
    # newsletter narration (less filler, no chipmunk effect). ElevenLabs
    # accepts 0.7–1.2; we clamp on save.
    "elevenlabs_speed":      "1.10",
    "aws_access_key_id":     "",
    "aws_secret_access_key": "",
    "aws_s3_bucket":         "",
    "aws_s3_region":         "us-east-1",
    "aws_s3_prefix":         "secdigest/audio/",
    "password_hash": DEFAULT_PASSWORD_HASH,
}

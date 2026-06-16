"""Public-facing FastAPI app: landing page, subscribe flow, unsubscribe.
Runs on its own port (PUBLIC_PORT, default 8000) alongside the admin app on 8080.
Shares the same SQLite database via secdigest.db.

There is intentionally no auth here — the surface is /, /subscribe, /confirm/<uuid>,
and /unsubscribe/<uuid>. Per-IP rate limits live in secdigest.web.security."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from secdigest import crypto, db
from secdigest.public.routes import router

PUBLIC_DIR = Path(__file__).parent
STATIC_DIR = PUBLIC_DIR / "static"


def _warn_if_smtp_undecryptable() -> None:
    """Loudly warn at startup when this process can't decrypt the stored SMTP
    password.

    The public site runs as its own process with its own environment. The SMTP
    password is stored encrypted under the admin app's ``SECRET_KEY``; if this
    process has a different ``SECRET_KEY``, ``crypto.decrypt`` silently returns
    ``""`` and every confirmation email fails with an opaque SMTP auth error.
    Surfacing it here turns a silent, per-subscriber failure into one obvious
    line in the startup log. We only flag an actual ciphertext — an empty
    password or legacy plaintext is not a misconfiguration.
    """
    smtp_pass = db.cfg_all().get("smtp_pass", "")
    if crypto.is_encrypted(smtp_pass) and crypto.decrypt(smtp_pass) == "":
        print(
            "[public] WARNING: stored SMTP password could not be decrypted in "
            "this process — confirmation emails will fail. This almost always "
            "means SECRET_KEY differs from the admin app that saved it. Set the "
            "same SECRET_KEY in both processes and re-save the password in Settings."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent init — both apps may call this on startup; ``init_db()``
    # uses CREATE TABLE IF NOT EXISTS and migration guards, so it's safe.
    db.init_db()
    _warn_if_smtp_undecryptable()
    yield


# Disable the auto-generated OpenAPI / Swagger / ReDoc endpoints. The
# public app has no API meant for third parties; hiding them avoids
# advertising internal route shapes to crawlers.
app = FastAPI(
    lifespan=lifespan,
    title="SecDigest",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(router)

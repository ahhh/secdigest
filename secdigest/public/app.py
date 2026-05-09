"""Public-facing FastAPI app: landing page, subscribe flow, unsubscribe.
Runs on its own port (PUBLIC_PORT, default 8000) alongside the admin app on 8080.
Shares the same SQLite database via secdigest.db.

There is intentionally no auth here — the surface is /, /subscribe, /confirm/<uuid>,
and /unsubscribe/<uuid>. Per-IP rate limits live in secdigest.web.security."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from secdigest import db
from secdigest.public.routes import router

PUBLIC_DIR = Path(__file__).parent
STATIC_DIR = PUBLIC_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent init — both apps may call this on startup; ``init_db()``
    # uses CREATE TABLE IF NOT EXISTS and migration guards, so it's safe.
    db.init_db()
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

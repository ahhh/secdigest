"""FastAPI application factory. Entry point for uvicorn.

This is the admin app (port 8080 in dev). The public app lives at
``secdigest.public.app`` and runs separately. Together they share the
SQLite DB but otherwise have isolated routers, sessions, and statics.

Boot order matters here:
1. Validate ``SECRET_KEY`` is set — running with the default would
   trivially defeat session signing and at-rest encryption.
2. ``db.init_db()`` — creates schema + runs migrations.
3. ``ensure_default_password()`` — seeds 'secdigest' if no admin pw is set.
4. Start the APScheduler so the daily fetch fires automatically.

On shutdown, the scheduler is stopped so uvicorn can exit promptly.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from secdigest import config, db
from secdigest.web import auth, templates
from secdigest.web.auth import is_authed, verify_password, ensure_default_password
from secdigest.web.csrf import verify_csrf
from secdigest.web.routes import (
    newsletter, prompts, subscribers, settings, email_templates_route,
    unsubscribe, feeds, digest, voice,
)
from secdigest.web.security import login_allowed, login_record_failure, login_clear
import secdigest.scheduler as sched

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Async context-managed startup/shutdown. Anything before ``yield``
    runs once at boot; anything after runs at shutdown."""
    # Hard-fail rather than booting with a known-bad key — operators have
    # accidentally shipped this default before.
    if config.SECRET_KEY == "dev-secret-change-me":
        raise RuntimeError(
            "SECRET_KEY is set to the default value. Set the SECRET_KEY env var to a "
            "long random string (e.g. `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`) "
            "before starting the app."
        )
    db.init_db()
    ensure_default_password()
    sched.start_scheduler()
    yield
    sched.stop_scheduler()


app = FastAPI(lifespan=lifespan, title="SecDigest")


# NOTE on middleware order: Starlette's add_middleware uses insert(0, ...), so the
# LAST middleware registered becomes the OUTERMOST one (runs first on incoming
# requests). We need SessionMiddleware to run before force_default_password_reset
# so that request.session is populated. Therefore the @middleware decorator must
# come BEFORE add_middleware(SessionMiddleware, ...) in source order.
@app.middleware("http")
async def force_default_password_reset(request: Request, call_next):
    path = request.url.path
    # Allow these paths even with default password:
    allowed = {"/login", "/logout", "/forced-password-change"}
    if (path in allowed
        or path.startswith("/static/")
        or path.startswith("/unsubscribe/")):
        return await call_next(request)
    # Defensive: if SessionMiddleware hasn't populated session yet, skip the check
    if "session" not in request.scope:
        return await call_next(request)
    if is_authed(request) and auth.is_default_password():
        return RedirectResponse("/forced-password-change", status_code=302)
    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    # 7-day session lifetime: the operator visits the admin most days, so a
    # week balances "don't log out too aggressively" against "stale sessions
    # don't linger forever".
    max_age=86400 * 7,
    # ``strict`` blocks the session cookie from being sent on cross-site
    # navigations — strongest CSRF defence at the cookie layer.
    same_site="strict",
    https_only=False,  # set to True in production behind TLS
)


# /static serves CSS/JS bundled with the admin templates.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Mount each feature's router. Keeping them in separate files keeps any
# single route file scrollable and tells you what to grep for when
# tracking down a bug ("what file owns POST /day/<date>?").
app.include_router(newsletter.router)
app.include_router(prompts.router)
app.include_router(subscribers.router)
app.include_router(settings.router)
app.include_router(email_templates_route.router)
app.include_router(unsubscribe.router)
app.include_router(feeds.router)
app.include_router(digest.router)
app.include_router(voice.router)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    # Rate-limit FIRST so an attacker can't keep hammering the bcrypt
    # verify (which is intentionally slow). Per-IP bucket; see security.py.
    if not login_allowed(request):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again in 15 minutes."},
            status_code=429,
        )
    ph = db.cfg_get("password_hash")
    if ph and verify_password(password, ph):
        # Successful login clears the IP's failure history so a successful
        # operator login doesn't carry a quota debt from earlier typos.
        login_clear(request)
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=302)
    # Failed login: count against the bucket. Generic "Wrong password"
    # message — never confirm whether the password hash even exists, etc.
    login_record_failure(request)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Wrong password"}, status_code=401
    )


@app.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/forced-password-change", response_class=HTMLResponse)
async def forced_password_change_page(request: Request, error: str = ""):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "forced_password_change.html",
        {"request": request, "error": error},
    )


@app.post("/forced-password-change", dependencies=[Depends(verify_csrf)])
async def forced_password_change_submit(request: Request,
                                         new_password: str = Form(...),
                                         confirm_password: str = Form(...)):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=302)
    if new_password != confirm_password:
        return RedirectResponse("/forced-password-change?error=Passwords+do+not+match", status_code=302)
    if len(new_password) < 8:
        return RedirectResponse("/forced-password-change?error=Password+must+be+at+least+8+characters", status_code=302)
    if new_password == "secdigest":
        return RedirectResponse("/forced-password-change?error=Pick+a+different+password", status_code=302)
    from secdigest.web.auth import hash_password
    db.cfg_set("password_hash", hash_password(new_password))
    return RedirectResponse("/?msg=Password+updated", status_code=302)

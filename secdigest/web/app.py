"""FastAPI application factory. Entry point for uvicorn."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from secdigest import config, db
from secdigest.web import templates
from secdigest.web.auth import is_authed, pwd_ctx, ensure_default_password
from secdigest.web.routes import newsletter, prompts, subscribers, settings
import secdigest.scheduler as sched

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    ensure_default_password()
    sched.start_scheduler()
    yield
    sched.stop_scheduler()


app = FastAPI(lifespan=lifespan, title="SecDigest")
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=86400 * 30)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(newsletter.router)
app.include_router(prompts.router)
app.include_router(subscribers.router)
app.include_router(settings.router)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    ph = db.cfg_get("password_hash")
    if ph and pwd_ctx.verify(password, ph):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Wrong password"}, status_code=401
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

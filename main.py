"""SecDigest — FastAPI web application."""
import asyncio
from datetime import date as dt_date
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext

import config
import db
import fetcher
import summarizer
import mailer
import scheduler

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _ensure_default_password()
    scheduler.start_scheduler()
    yield
    scheduler.stop_scheduler()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=86400 * 30)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _ensure_default_password():
    ph = db.cfg_get("password_hash")
    if not ph:
        default_hash = pwd_ctx.hash("secdigest")
        db.cfg_set("password_hash", default_hash)
        print("\n" + "!"*60)
        print("  DEFAULT PASSWORD: secdigest")
        print("  Change it at /settings after first login!")
        print("!"*60 + "\n")


def _authed(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def _redirect_login():
    return RedirectResponse("/login", status_code=302)


def _today() -> str:
    return dt_date.today().isoformat()


# ── Auth ────────────────────────────────────────────────────────────────────

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


# ── Home / Today ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _authed(request):
        return _redirect_login()
    return RedirectResponse(f"/day/{_today()}", status_code=302)


# ── Newsletter Day View ──────────────────────────────────────────────────────

@app.get("/day/{date_str}", response_class=HTMLResponse)
async def day_view(request: Request, date_str: str):
    if not _authed(request):
        return _redirect_login()
    newsletter = db.newsletter_get(date_str)
    articles = db.article_list(newsletter["id"]) if newsletter else []
    return templates.TemplateResponse("day.html", {
        "request": request,
        "date_str": date_str,
        "newsletter": newsletter,
        "articles": articles,
        "is_today": date_str == _today(),
    })


@app.post("/day/{date_str}/fetch")
async def day_fetch(request: Request, date_str: str):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    asyncio.create_task(fetcher.run_fetch(date_str))
    return RedirectResponse(f"/day/{date_str}?msg=Fetching+articles...", status_code=302)


@app.post("/day/{date_str}/summarize")
async def day_summarize(request: Request, date_str: str):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    newsletter = db.newsletter_get(date_str)
    if not newsletter:
        return RedirectResponse(f"/day/{date_str}?msg=No+newsletter+found", status_code=302)
    asyncio.create_task(asyncio.to_thread(summarizer.summarize_newsletter, newsletter["id"]))
    return RedirectResponse(f"/day/{date_str}?msg=Generating+summaries...", status_code=302)


@app.post("/day/{date_str}/send")
async def day_send(request: Request, date_str: str):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    ok, msg = mailer.send_newsletter(date_str)
    status = "ok" if ok else "error"
    return RedirectResponse(f"/day/{date_str}?msg={msg.replace(' ', '+')}&status={status}", status_code=302)


@app.post("/day/{date_str}/article/{article_id}/summary")
async def update_summary(
    request: Request, date_str: str, article_id: int,
    summary: str = Form(...)
):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.article_update(article_id, summary=summary)
    return RedirectResponse(f"/day/{date_str}", status_code=302)


@app.post("/day/{date_str}/article/{article_id}/regenerate")
async def regenerate_summary(request: Request, date_str: str, article_id: int):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    asyncio.create_task(asyncio.to_thread(summarizer.summarize_article, article_id))
    return RedirectResponse(f"/day/{date_str}?msg=Regenerating+summary...", status_code=302)


@app.post("/day/{date_str}/article/{article_id}/toggle")
async def toggle_article(request: Request, date_str: str, article_id: int):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    conn_row = db._get_conn().execute(
        "SELECT included FROM articles WHERE id=?", (article_id,)
    ).fetchone()
    if conn_row:
        db.article_update(article_id, included=0 if conn_row[0] else 1)
    return RedirectResponse(f"/day/{date_str}", status_code=302)


@app.post("/day/{date_str}/reorder")
async def reorder_articles(request: Request, date_str: str):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()
    order = form.getlist("order")
    newsletter = db.newsletter_get(date_str)
    if newsletter and order:
        db.article_reorder(newsletter["id"], [int(x) for x in order])
    return RedirectResponse(f"/day/{date_str}", status_code=302)


# ── Archive ──────────────────────────────────────────────────────────────────

@app.get("/archive", response_class=HTMLResponse)
async def archive(request: Request):
    if not _authed(request):
        return _redirect_login()
    newsletters = db.newsletter_list()
    counts = {}
    for n in newsletters:
        arts = db.article_list(n["id"])
        counts[n["id"]] = sum(1 for a in arts if a.get("included", 1))
    return templates.TemplateResponse("archive.html", {
        "request": request,
        "newsletters": newsletters,
        "counts": counts,
    })


# ── Prompts ──────────────────────────────────────────────────────────────────

@app.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    if not _authed(request):
        return _redirect_login()
    return templates.TemplateResponse("prompts.html", {
        "request": request,
        "prompts": db.prompt_list(),
    })


@app.post("/prompts")
async def create_prompt(
    request: Request,
    name: str = Form(...),
    type_: str = Form(..., alias="type"),
    content: str = Form(...),
):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.prompt_create(name, type_, content)
    return RedirectResponse("/prompts", status_code=302)


@app.post("/prompts/{prompt_id}/update")
async def update_prompt(
    request: Request, prompt_id: int,
    name: str = Form(...),
    content: str = Form(...),
):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.prompt_update(prompt_id, name=name, content=content)
    return RedirectResponse("/prompts", status_code=302)


@app.post("/prompts/{prompt_id}/toggle")
async def toggle_prompt(request: Request, prompt_id: int):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    row = db._get_conn().execute("SELECT active FROM prompts WHERE id=?", (prompt_id,)).fetchone()
    if row:
        db.prompt_update(prompt_id, active=0 if row[0] else 1)
    return RedirectResponse("/prompts", status_code=302)


@app.post("/prompts/{prompt_id}/delete")
async def delete_prompt(request: Request, prompt_id: int):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.prompt_delete(prompt_id)
    return RedirectResponse("/prompts", status_code=302)


# ── Subscribers ──────────────────────────────────────────────────────────────

@app.get("/subscribers", response_class=HTMLResponse)
async def subscribers_page(request: Request):
    if not _authed(request):
        return _redirect_login()
    return templates.TemplateResponse("subscribers.html", {
        "request": request,
        "subscribers": db.subscriber_list(),
    })


@app.post("/subscribers")
async def add_subscriber(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    result = db.subscriber_create(email.strip().lower(), name.strip())
    msg = "Added" if result else "Already+exists"
    return RedirectResponse(f"/subscribers?msg={msg}", status_code=302)


@app.post("/subscribers/{sub_id}/delete")
async def delete_subscriber(request: Request, sub_id: int):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.subscriber_delete(sub_id)
    return RedirectResponse("/subscribers", status_code=302)


@app.post("/subscribers/{sub_id}/toggle")
async def toggle_subscriber(request: Request, sub_id: int):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    row = db._get_conn().execute(
        "SELECT active FROM subscribers WHERE id=?", (sub_id,)
    ).fetchone()
    if row:
        from db import _lock, _get_conn
        with _lock:
            _get_conn().execute(
                "UPDATE subscribers SET active=? WHERE id=?",
                (0 if row[0] else 1, sub_id)
            )
            _get_conn().commit()
    return RedirectResponse("/subscribers", status_code=302)


# ── Settings ─────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _authed(request):
        return _redirect_login()
    cfg = db.cfg_all()
    audit = db.audit_recent(20)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "cfg": cfg,
        "audit": audit,
    })


@app.post("/settings")
async def save_settings(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()

    fields = [
        "smtp_host", "smtp_port", "smtp_user", "smtp_from",
        "fetch_time", "hn_min_score", "max_articles",
    ]
    for f in fields:
        if f in form:
            db.cfg_set(f, form[f])

    if form.get("smtp_pass"):
        db.cfg_set("smtp_pass", form["smtp_pass"])

    db.cfg_set("auto_send", "1" if form.get("auto_send") else "0")

    if form.get("new_password"):
        db.cfg_set("password_hash", pwd_ctx.hash(form["new_password"]))

    scheduler.reschedule(db.cfg_get("fetch_time"))
    return RedirectResponse("/settings?msg=Saved", status_code=302)

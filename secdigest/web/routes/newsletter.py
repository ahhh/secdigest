"""Routes: newsletter day view, archive, fetch, summarize, send, article actions."""
import asyncio
from datetime import date as dt_date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db, fetcher, summarizer, mailer
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login

router = APIRouter()


def _today() -> str:
    return dt_date.today().isoformat()


# ── Home ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_authed(request):
        return redirect_login()
    return RedirectResponse(f"/day/{_today()}", status_code=302)


# ── Archive ───────────────────────────────────────────────────────────────────

@router.get("/archive", response_class=HTMLResponse)
async def archive(request: Request):
    if not is_authed(request):
        return redirect_login()
    newsletters = db.newsletter_list()
    counts = {
        n["id"]: sum(1 for a in db.article_list(n["id"]) if a.get("included", 1))
        for n in newsletters
    }
    return templates.TemplateResponse("archive.html", {
        "request": request,
        "newsletters": newsletters,
        "counts": counts,
    })


# ── Day view ──────────────────────────────────────────────────────────────────

@router.get("/day/{date_str}", response_class=HTMLResponse)
async def day_view(request: Request, date_str: str):
    if not is_authed(request):
        return redirect_login()
    newsletter = db.newsletter_get(date_str)
    articles = db.article_list(newsletter["id"]) if newsletter else []
    view = request.query_params.get("view", "curator")
    email_templates = db.email_template_list()
    active_template_id = (
        db.newsletter_get_template_id(newsletter["id"]) if newsletter else None
    ) or (email_templates[0]["id"] if email_templates else None)
    active_subject = (
        db.newsletter_get_subject(newsletter["id"]) if newsletter else None
    )
    if not active_subject and email_templates:
        tmpl = next((t for t in email_templates if t["id"] == active_template_id), email_templates[0] if email_templates else None)
        active_subject = tmpl["subject"].replace("{date}", date_str) if tmpl else f"SecDigest — {date_str}"
    return templates.TemplateResponse("day.html", {
        "request": request,
        "date_str": date_str,
        "newsletter": newsletter,
        "articles": articles,
        "is_today": date_str == _today(),
        "fetching": request.query_params.get("fetching") == "1",
        "view": view,
        "email_templates": email_templates,
        "active_template_id": active_template_id,
        "active_subject": active_subject,
    })


# ── Actions ───────────────────────────────────────────────────────────────────

@router.post("/day/{date_str}/fetch")
async def day_fetch(request: Request, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    newsletter = db.newsletter_get(date_str)
    if newsletter and db.article_hn_ids(newsletter["id"]):
        return RedirectResponse(f"/day/{date_str}?msg=Articles+already+fetched", status_code=302)
    asyncio.create_task(fetcher.run_fetch(date_str))
    return RedirectResponse(f"/day/{date_str}?fetching=1", status_code=302)


@router.get("/day/{date_str}/preview", response_class=HTMLResponse)
async def day_preview(request: Request, date_str: str, template_id: int = 0):
    if not is_authed(request):
        return HTMLResponse("", status_code=401)
    newsletter = db.newsletter_get(date_str)
    _placeholder = lambda msg: HTMLResponse(
        f'<!DOCTYPE html><html><body style="margin:0;padding:40px;background:#0d1117;'
        f'color:#6e7681;font-family:monospace;text-align:center;"><p>{msg}</p></body></html>'
    )
    if not newsletter:
        return _placeholder("No newsletter for this date.")
    articles = db.article_list(newsletter["id"])
    if not any(a.get("included", 1) for a in articles):
        return _placeholder("No included articles yet — add them in the Curator tab.")
    tid = template_id or None
    return HTMLResponse(mailer.render_email_html(newsletter, articles, tid))


@router.post("/day/{date_str}/set-template")
async def set_template(request: Request, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()
    template_id = int(form.get("template_id", 0))
    subject = form.get("subject", "").strip()
    newsletter = db.newsletter_get_or_create(date_str)
    if template_id:
        db.newsletter_set_template_id(newsletter["id"], template_id)
    if subject:
        db.newsletter_set_subject(newsletter["id"], subject)
    return RedirectResponse(f"/day/{date_str}?view=builder", status_code=302)


@router.post("/day/{date_str}/summarize")
async def day_summarize(request: Request, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    newsletter = db.newsletter_get(date_str)
    if not newsletter:
        return RedirectResponse(f"/day/{date_str}?msg=No+newsletter+found", status_code=302)
    asyncio.create_task(asyncio.to_thread(summarizer.summarize_newsletter, newsletter["id"]))
    return RedirectResponse(f"/day/{date_str}?msg=Generating+summaries...", status_code=302)


@router.post("/day/{date_str}/send")
async def day_send(request: Request, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    ok, msg = mailer.send_newsletter(date_str)
    status = "ok" if ok else "error"
    return RedirectResponse(
        f"/day/{date_str}?msg={msg.replace(' ', '+')}&status={status}", status_code=302
    )


# ── Article actions ───────────────────────────────────────────────────────────

@router.post("/day/{date_str}/article/{article_id}/summary")
async def update_summary(request: Request, date_str: str, article_id: int,
                         summary: str = Form(...)):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.article_update(article_id, summary=summary)
    return RedirectResponse(f"/day/{date_str}", status_code=302)


@router.post("/day/{date_str}/article/{article_id}/regenerate")
async def regenerate_summary(request: Request, date_str: str, article_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    asyncio.create_task(asyncio.to_thread(summarizer.summarize_article, article_id))
    return RedirectResponse(f"/day/{date_str}?msg=Regenerating+summary...", status_code=302)


@router.post("/day/{date_str}/article/{article_id}/toggle")
async def toggle_article(request: Request, date_str: str, article_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    article = db.article_get(article_id)
    if article:
        db.article_update(article_id, included=0 if article["included"] else 1)
    return RedirectResponse(f"/day/{date_str}", status_code=302)


@router.post("/day/{date_str}/reorder")
async def reorder_articles(request: Request, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()
    order = form.getlist("order")
    newsletter = db.newsletter_get(date_str)
    if newsletter and order:
        db.article_reorder(newsletter["id"], [int(x) for x in order])
    return RedirectResponse(f"/day/{date_str}", status_code=302)

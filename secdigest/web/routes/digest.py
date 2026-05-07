"""Routes: weekly and monthly digest curator + builder.

The digest model: each weekly/monthly digest is a row in `newsletters` with kind != 'daily'.
Articles are joined via `digest_articles` rather than the per-day `articles.newsletter_id`
relation — daily articles can therefore appear in multiple digests without duplication.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db, mailer, periods
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login
from secdigest.web.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


def _bounds(kind: str, date_str: str) -> tuple[str, str]:
    if kind == "weekly":
        return periods.iso_week_bounds(date_str)
    if kind == "monthly":
        return periods.month_bounds(date_str)
    raise ValueError(f"bad kind {kind!r}")


def _ensure_digest(kind: str, period_start: str, period_end: str) -> dict:
    return db.newsletter_get_or_create(period_start, kind=kind,
                                        period_start=period_start, period_end=period_end)


def _kind_label(kind: str) -> str:
    return {"weekly": "Weekly", "monthly": "Monthly"}[kind]


# ── Digest curator/builder view ───────────────────────────────────────────────

async def _digest_view(request: Request, kind: str, date_str: str):
    if not is_authed(request):
        return redirect_login()
    # date_str is reflected into JS via the digest.html <script> tag; backstop
    # the |tojson template filter with a route-level regex check so a malformed
    # date 404s before reaching the template at all.
    from secdigest.web.routes.newsletter import _validate_date
    _validate_date(date_str)
    period_start, period_end = _bounds(kind, date_str)
    digest = _ensure_digest(kind, period_start, period_end)
    # Auto-seed on first visit when the digest is empty
    if db.digest_article_count(digest["id"]) == 0:
        max_n = int(db.cfg_get("max_curator_articles") or 10)
        db.digest_seed(digest["id"], kind=kind,
                       period_start=period_start, period_end=period_end, top_n=max_n)
    articles = db.digest_article_list(digest["id"])
    view = request.query_params.get("view", "curator")
    email_templates = db.email_template_list()
    active_template_id = (
        db.newsletter_get_template_id(digest["id"]) or
        (email_templates[0]["id"] if email_templates else None)
    )
    active_subject = db.newsletter_get_subject(digest["id"])
    if not active_subject and email_templates:
        tmpl = next((t for t in email_templates if t["id"] == active_template_id),
                    email_templates[0])
        default_subject = f"SecDigest {_kind_label(kind)} — {{date}}"
        raw = tmpl["subject"] if tmpl["subject"] else default_subject
        # If the built-in subject still uses the daily form, swap in the digest form
        if "Weekly" not in raw and "Monthly" not in raw and kind in ("weekly", "monthly"):
            raw = default_subject
        active_subject = raw.replace("{date}", period_start)
    active_toc = db.newsletter_get_toc(digest["id"])
    active_voice = db.newsletter_get_voice_enabled(digest["id"])
    voice_summary_enabled = db.cfg_get("voice_summary_enabled") == "1"
    voice_status = db.voice_audio_get(digest["id"])

    return templates.TemplateResponse("digest.html", {
        "request": request,
        "kind": kind,
        "kind_label": _kind_label(kind),
        "date_str": date_str,
        "period_start": period_start,
        "period_end": period_end,
        "digest": digest,
        "articles": articles,
        "view": view,
        "email_templates": email_templates,
        "active_template_id": active_template_id,
        "active_subject": active_subject,
        "active_toc": active_toc,
        "active_voice": active_voice,
        "voice_summary_enabled": voice_summary_enabled,
        "voice_status": voice_status,
    })


@router.get("/week/{date_str}", response_class=HTMLResponse)
async def week_view(request: Request, date_str: str):
    return await _digest_view(request, "weekly", date_str)


@router.get("/month/{date_str}", response_class=HTMLResponse)
async def month_view(request: Request, date_str: str):
    return await _digest_view(request, "monthly", date_str)


# ── Actions ───────────────────────────────────────────────────────────────────

def _redirect(kind: str, date_str: str, view: str | None = None, msg: str | None = None,
              status: str | None = None) -> RedirectResponse:
    base = "/week/" if kind == "weekly" else "/month/"
    qs = []
    if view: qs.append(f"view={view}")
    if msg: qs.append(f"msg={msg.replace(' ', '+')}")
    if status: qs.append(f"status={status}")
    suffix = ("?" + "&".join(qs)) if qs else ""
    return RedirectResponse(f"{base}{date_str}{suffix}", status_code=302)


async def _auto_select(request: Request, kind: str, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    period_start, period_end = _bounds(kind, date_str)
    digest = _ensure_digest(kind, period_start, period_end)
    max_n = int(db.cfg_get("max_curator_articles") or 10)
    db.digest_seed(digest["id"], kind=kind,
                   period_start=period_start, period_end=period_end, top_n=max_n)
    return _redirect(kind, date_str, msg="Refreshed from pinned + top relevance")


@router.post("/week/{date_str}/auto-select")
async def week_auto_select(request: Request, date_str: str):
    return await _auto_select(request, "weekly", date_str)


@router.post("/month/{date_str}/auto-select")
async def month_auto_select(request: Request, date_str: str):
    return await _auto_select(request, "monthly", date_str)


async def _toggle(request: Request, kind: str, date_str: str, article_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    period_start, _ = _bounds(kind, date_str)
    digest = db.newsletter_get(period_start, kind=kind)
    if digest:
        db.digest_article_toggle(digest["id"], article_id)
    return _redirect(kind, date_str)


@router.post("/week/{date_str}/article/{article_id}/toggle")
async def week_toggle(request: Request, date_str: str, article_id: int):
    return await _toggle(request, "weekly", date_str, article_id)


@router.post("/month/{date_str}/article/{article_id}/toggle")
async def month_toggle(request: Request, date_str: str, article_id: int):
    return await _toggle(request, "monthly", date_str, article_id)


async def _remove(request: Request, kind: str, date_str: str, article_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    period_start, _ = _bounds(kind, date_str)
    digest = db.newsletter_get(period_start, kind=kind)
    if digest:
        db.digest_article_remove(digest["id"], article_id)
    return _redirect(kind, date_str)


@router.post("/week/{date_str}/article/{article_id}/remove")
async def week_remove(request: Request, date_str: str, article_id: int):
    return await _remove(request, "weekly", date_str, article_id)


@router.post("/month/{date_str}/article/{article_id}/remove")
async def month_remove(request: Request, date_str: str, article_id: int):
    return await _remove(request, "monthly", date_str, article_id)


async def _reorder(request: Request, kind: str, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()
    order = form.getlist("order")
    period_start, _ = _bounds(kind, date_str)
    digest = db.newsletter_get(period_start, kind=kind)
    if digest and order:
        db.digest_article_reorder(digest["id"], [int(x) for x in order])
    return _redirect(kind, date_str)


@router.post("/week/{date_str}/reorder")
async def week_reorder(request: Request, date_str: str):
    return await _reorder(request, "weekly", date_str)


@router.post("/month/{date_str}/reorder")
async def month_reorder(request: Request, date_str: str):
    return await _reorder(request, "monthly", date_str)


async def _set_template(request: Request, kind: str, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()
    template_id = int(form.get("template_id", 0))
    subject = form.get("subject", "").strip()
    include_toc = form.get("include_toc") == "1"
    period_start, period_end = _bounds(kind, date_str)
    digest = _ensure_digest(kind, period_start, period_end)
    if template_id:
        db.newsletter_set_template_id(digest["id"], template_id)
    if subject:
        db.newsletter_set_subject(digest["id"], subject)
    db.newsletter_set_toc(digest["id"], include_toc)
    return _redirect(kind, date_str, view="builder")


@router.post("/week/{date_str}/set-template")
async def week_set_template(request: Request, date_str: str):
    return await _set_template(request, "weekly", date_str)


@router.post("/month/{date_str}/set-template")
async def month_set_template(request: Request, date_str: str):
    return await _set_template(request, "monthly", date_str)


# ── Preview / send ────────────────────────────────────────────────────────────

_PREVIEW_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src https: data:; style-src 'unsafe-inline'",
    "X-Frame-Options": "SAMEORIGIN",
}


def _placeholder(msg: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!DOCTYPE html><html><body style="margin:0;padding:40px;background:#0d1117;'
        f'color:#6e7681;font-family:monospace;text-align:center;"><p>{msg}</p></body></html>',
        headers=_PREVIEW_HEADERS,
    )


async def _preview(request: Request, kind: str, date_str: str,
                   template_id: int = 0, include_toc: int = 0):
    if not is_authed(request):
        return HTMLResponse("", status_code=401)
    period_start, _ = _bounds(kind, date_str)
    digest = db.newsletter_get(period_start, kind=kind)
    if not digest:
        return _placeholder("Open the digest first to seed it.")
    articles = db.digest_article_list(digest["id"])
    if not any(a.get("included", 1) for a in articles):
        return _placeholder("No included articles — toggle some on in the curator.")
    return HTMLResponse(
        mailer.render_email_html(digest, articles, template_id or None,
                                 include_toc=bool(include_toc)),
        headers=_PREVIEW_HEADERS,
    )


@router.get("/week/{date_str}/preview", response_class=HTMLResponse)
async def week_preview(request: Request, date_str: str, template_id: int = 0, include_toc: int = 0):
    return await _preview(request, "weekly", date_str, template_id, include_toc)


@router.get("/month/{date_str}/preview", response_class=HTMLResponse)
async def month_preview(request: Request, date_str: str, template_id: int = 0, include_toc: int = 0):
    return await _preview(request, "monthly", date_str, template_id, include_toc)


async def _send(request: Request, kind: str, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    period_start, _ = _bounds(kind, date_str)
    ok, msg = mailer.send_newsletter(period_start, kind=kind)
    return _redirect(kind, date_str, view="builder", msg=msg, status=("ok" if ok else "error"))


@router.post("/week/{date_str}/send")
async def week_send(request: Request, date_str: str):
    return await _send(request, "weekly", date_str)


@router.post("/month/{date_str}/send")
async def month_send(request: Request, date_str: str):
    return await _send(request, "monthly", date_str)


async def _send_test(request: Request, kind: str, date_str: str, test_recipient: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    period_start, _ = _bounds(kind, date_str)
    ok, msg = mailer.send_test_email(period_start, test_recipient, kind=kind)
    return _redirect(kind, date_str, view="builder", msg=msg, status=("ok" if ok else "error"))


@router.post("/week/{date_str}/send-test")
async def week_send_test(request: Request, date_str: str, test_recipient: str = Form(...)):
    return await _send_test(request, "weekly", date_str, test_recipient)


@router.post("/month/{date_str}/send-test")
async def month_send_test(request: Request, date_str: str, test_recipient: str = Form(...)):
    return await _send_test(request, "monthly", date_str, test_recipient)

"""Routes: newsletter day view, archive, fetch, summarize, send, article actions."""
import asyncio
from datetime import date as dt_date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db, fetcher, summarizer, mailer, periods
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login
from secdigest.web.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


def _today() -> str:
    return dt_date.today().isoformat()


def _validate_date(date_str: str) -> str:
    """Reject anything that isn't a real ISO YYYY-MM-DD before letting it reach
    a template. Path params are dropped into JS contexts in day.html / digest.html;
    Jinja autoescape doesn't cover JS, so we backstop with `|tojson` in the
    template AND a regex check at the route boundary for defence-in-depth."""
    try:
        dt_date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=404, detail="Bad date")
    return date_str


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
    dailies = db.newsletter_list(limit=365, kind="daily")
    weeklies = {n["period_start"]: n for n in db.newsletter_list(limit=365, kind="weekly")}
    monthlies = {n["period_start"]: n for n in db.newsletter_list(limit=120, kind="monthly")}

    daily_counts = {n["id"]: db.article_count(n["id"]) for n in dailies}
    weekly_counts = {n["id"]: db.digest_article_count(n["id"]) for n in weeklies.values()}
    monthly_counts = {n["id"]: db.digest_article_count(n["id"]) for n in monthlies.values()}

    # Group dailies into Month → Week → [Day] in newest-first order, while preserving
    # encounter order so the template can render without re-sorting.
    months: list[dict] = []
    months_by_first: dict[str, dict] = {}
    weeks_by_monday: dict[str, dict] = {}

    for n in dailies:
        month_first, _ = periods.month_bounds(n["date"])
        monday, sunday = periods.iso_week_bounds(n["date"])

        m = months_by_first.get(month_first)
        if not m:
            m = {
                "label": periods.month_label(n["date"]),
                "first": month_first,
                "monthly_digest": monthlies.get(month_first),
                "weeks": [],
            }
            months_by_first[month_first] = m
            months.append(m)

        w = weeks_by_monday.get(monday)
        if not w:
            w = {
                "label": f"Week of {monday}",
                "monday": monday,
                "sunday": sunday,
                "weekly_digest": weeklies.get(monday),
                "days": [],
            }
            weeks_by_monday[monday] = w
            m["weeks"].append(w)

        w["days"].append(n)

    return templates.TemplateResponse("archive.html", {
        "request": request,
        "months": months,
        "daily_counts": daily_counts,
        "weekly_counts": weekly_counts,
        "monthly_counts": monthly_counts,
    })


# ── Day view ──────────────────────────────────────────────────────────────────

@router.get("/day/{date_str}", response_class=HTMLResponse)
async def day_view(request: Request, date_str: str):
    if not is_authed(request):
        return redirect_login()
    _validate_date(date_str)
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
    active_toc = db.newsletter_get_toc(newsletter["id"]) if newsletter else False
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
        "active_toc": active_toc,
    })


# ── Actions ───────────────────────────────────────────────────────────────────

@router.post("/day/{date_str}/fetch")
async def day_fetch(request: Request, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    newsletter = db.newsletter_get(date_str)
    if newsletter and db.article_count(newsletter["id"]) > 0:
        return RedirectResponse(f"/day/{date_str}?msg=Articles+already+fetched", status_code=302)
    asyncio.create_task(fetcher.run_fetch(date_str))
    return RedirectResponse(f"/day/{date_str}?fetching=1", status_code=302)


@router.get("/day/{date_str}/preview", response_class=HTMLResponse)
async def day_preview(request: Request, date_str: str, template_id: int = 0, include_toc: int = 0):
    if not is_authed(request):
        return HTMLResponse("", status_code=401)
    _preview_headers = {
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src https: data:; style-src 'unsafe-inline'",
        "X-Frame-Options": "SAMEORIGIN",
    }
    newsletter = db.newsletter_get(date_str)
    _placeholder = lambda msg: HTMLResponse(
        f'<!DOCTYPE html><html><body style="margin:0;padding:40px;background:#0d1117;'
        f'color:#6e7681;font-family:monospace;text-align:center;"><p>{msg}</p></body></html>',
        headers=_preview_headers,
    )
    if not newsletter:
        return _placeholder("No newsletter for this date.")
    articles = db.article_list(newsletter["id"])
    if not any(a.get("included", 1) for a in articles):
        return _placeholder("No included articles yet — add them in the Curator tab.")
    tid = template_id or None
    return HTMLResponse(
        mailer.render_email_html(newsletter, articles, tid, include_toc=bool(include_toc)),
        headers=_preview_headers,
    )


@router.post("/day/{date_str}/set-template")
async def set_template(request: Request, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()
    template_id = int(form.get("template_id", 0))
    subject = form.get("subject", "").strip()
    include_toc = form.get("include_toc") == "1"
    newsletter = db.newsletter_get_or_create(date_str)
    if template_id:
        db.newsletter_set_template_id(newsletter["id"], template_id)
    if subject:
        db.newsletter_set_subject(newsletter["id"], subject)
    db.newsletter_set_toc(newsletter["id"], include_toc)
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


@router.post("/day/{date_str}/send-test")
async def day_send_test(request: Request, date_str: str,
                        test_recipient: str = Form(...)):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    ok, msg = mailer.send_test_email(date_str, test_recipient)
    status = "ok" if ok else "error"
    return RedirectResponse(
        f"/day/{date_str}?view=builder&msg={msg.replace(' ', '+')}&status={status}",
        status_code=302,
    )


@router.get("/day/{date_str}/pool", response_class=HTMLResponse)
async def day_pool(request: Request, date_str: str):
    if not is_authed(request):
        return redirect_login()
    newsletter = db.newsletter_get(date_str)
    articles = db.article_list(newsletter["id"]) if newsletter else []
    articles = sorted(articles, key=lambda a: a.get("relevance_score", 0), reverse=True)
    included_count = sum(1 for a in articles if a.get("included", 1))
    max_curator = int(db.cfg_get("max_curator_articles") or 10)
    return templates.TemplateResponse("pool.html", {
        "request": request,
        "date_str": date_str,
        "newsletter": newsletter,
        "articles": articles,
        "included_count": included_count,
        "max_curator": max_curator,
    })


@router.post("/day/{date_str}/auto-select")
async def auto_select(request: Request, date_str: str):
    if not is_authed(request):
        return RedirectResponse(f"/day/{date_str}/pool", status_code=302)
    newsletter = db.newsletter_get(date_str)
    max_curator = int(db.cfg_get("max_curator_articles") or 10)
    if newsletter:
        db.article_auto_select(newsletter["id"], max_curator)
    return RedirectResponse(
        f"/day/{date_str}/pool?msg=Top+{max_curator}+articles+selected", status_code=302
    )


# ── Article actions ───────────────────────────────────────────────────────────

@router.post("/day/{date_str}/article/add")
async def add_article(request: Request, date_str: str,
                      url: str = Form(""),
                      title: str = Form(...),
                      summary: str = Form(""),
                      auto_summarize: str = Form("0")):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    if not url.strip() and not summary.strip():
        return RedirectResponse(
            f"/day/{date_str}?msg=URL+or+summary+required&status=error", status_code=302
        )
    newsletter = db.newsletter_get_or_create(date_str)
    articles = db.article_list(newsletter["id"])
    position = max((a["position"] for a in articles), default=-1) + 1
    reason = "manually added" if url.strip() else "editorial note"
    article_id = db.article_insert(
        newsletter_id=newsletter["id"],
        hn_id=None,
        title=title.strip(),
        url=url.strip(),
        hn_score=0,
        hn_comments=0,
        relevance_score=10.0,
        relevance_reason=reason,
        position=position,
    )
    if summary.strip():
        db.article_update(article_id, summary=summary.strip())
    elif auto_summarize == "1":
        asyncio.create_task(asyncio.to_thread(summarizer.summarize_article, article_id))
    return RedirectResponse(f"/day/{date_str}", status_code=302)


@router.post("/day/{date_str}/article/{article_id}/summary")
async def update_summary(request: Request, date_str: str, article_id: int,
                         summary: str = Form(...)):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.article_update(article_id, summary=summary)
    return RedirectResponse(f"/day/{date_str}", status_code=302)


@router.get("/day/{date_str}/article/{article_id}/json")
async def article_json(request: Request, date_str: str, article_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    article = db.article_get(article_id)
    if not article:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"id": article_id, "summary": article.get("summary")})


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


@router.post("/day/{date_str}/article/{article_id}/pin/{period}")
async def pin_article(request: Request, date_str: str, article_id: int, period: str):
    """Toggle pin_weekly or pin_monthly on a daily article.
    Side effect: if a digest for the article's period already exists, the article is
    added (or removed, when unpinning) so the digest stays in sync without re-seeding."""
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    if period not in ("weekly", "monthly"):
        return RedirectResponse(f"/day/{date_str}?msg=Bad+period&status=error", status_code=302)
    article = db.article_get(article_id)
    if not article:
        return RedirectResponse(f"/day/{date_str}", status_code=302)

    pin_col = f"pin_{period}"
    new_pinned = not bool(article.get(pin_col, 0))
    db.article_set_pin(article_id, period, new_pinned)

    # Find the source daily's date so we can locate the matching digest window
    source = db.newsletter_get(date_str)
    if source:
        if period == "weekly":
            p_start, p_end = periods.iso_week_bounds(source["date"])
        else:
            p_start, p_end = periods.month_bounds(source["date"])
        digest = db.newsletter_get(p_start, kind=period)
        if digest:
            if new_pinned:
                # Append at end so the curator's manual ordering stays intact
                count = db.digest_article_count(digest["id"])
                db.digest_article_add(digest["id"], article_id, position=count, included=1)
            else:
                db.digest_article_remove(digest["id"], article_id)
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

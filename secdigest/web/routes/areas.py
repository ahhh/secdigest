"""Routes: per-area weekly issue editor (/areas) and curated trail list (/trails).

Each area (Utah, the Poconos) has one weekly issue built around a 7-day weather
forecast, a random easy/moderate trail, and operator-added trip ads. This module
is the admin surface for building, previewing, and sending those issues, plus
managing the trail pool that backs the random pick (auto-imported from komoot,
hand-editable here). Auth-required + CSRF-protected, mirroring feeds.py.
"""
from datetime import date as dt_date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db, mailer, areas
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login
from secdigest.web.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])

# Preview iframe CSP — same hardening as the day-view preview.
_PREVIEW_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src https: data:; style-src 'unsafe-inline'",
    "X-Frame-Options": "SAMEORIGIN",
}


def _issue_window() -> tuple[str, str]:
    """(period_start, period_end) for the upcoming Saturday issue. period_start
    is the next Saturday (today if today is Saturday); the window spans the
    following 7 days so the forecast covers the week ahead."""
    today = dt_date.today()
    days_ahead = (5 - today.weekday()) % 7  # Saturday == weekday 5
    sat = today + timedelta(days=days_ahead)
    return sat.isoformat(), (sat + timedelta(days=6)).isoformat()


def _area_or_404(slug: str) -> dict | None:
    return areas.area_by_slug(slug)


# ── Area issue editor ─────────────────────────────────────────────────────────

@router.get("/areas", response_class=HTMLResponse)
async def areas_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    week_start, week_end = _issue_window()
    cards = []
    for area in areas.AREAS:
        slug = area["slug"]
        nl = db.newsletter_get(week_start, kind=slug)
        arts = db.article_list(nl["id"]) if nl else []
        cards.append({
            "area": area,
            "newsletter": nl,
            "articles": arts,
            "list_url": db.area_list_url_get(slug),
            "trail_count": len(db.trail_list(slug)),
            "subscriber_count": len(db.subscriber_active_for_area(slug)),
        })
    return templates.TemplateResponse(request, "areas.html", {
        "cards": cards,
        "week_start": week_start,
        "week_end": week_end,
    })


@router.post("/areas/{slug}/build")
async def area_build(request: Request, slug: str):
    if not is_authed(request):
        return RedirectResponse("/areas", status_code=302)
    if not _area_or_404(slug):
        return RedirectResponse("/areas?msg=Unknown+area&status=error", status_code=302)
    week_start, week_end = _issue_window()
    areas.build_area_issue(slug, week_start, week_end)
    return RedirectResponse(f"/areas?msg=Built+{slug}+issue", status_code=302)


@router.post("/areas/{slug}/rebuild-weather")
async def area_rebuild_weather(request: Request, slug: str):
    if not is_authed(request):
        return RedirectResponse("/areas", status_code=302)
    area = _area_or_404(slug)
    week_start, _ = _issue_window()
    nl = db.newsletter_get(week_start, kind=slug)
    if area and nl:
        areas.refresh_weather(nl["id"], area)
    return RedirectResponse("/areas?msg=Weather+refreshed", status_code=302)


@router.post("/areas/{slug}/reroll-trail")
async def area_reroll_trail(request: Request, slug: str):
    if not is_authed(request):
        return RedirectResponse("/areas", status_code=302)
    area = _area_or_404(slug)
    week_start, _ = _issue_window()
    nl = db.newsletter_get(week_start, kind=slug)
    if area and nl:
        ok = areas.reroll_trail(nl["id"], area)
        if not ok:
            return RedirectResponse(
                f"/areas?msg=No+trails+for+{slug}+%E2%80%94+import+or+add+some&status=error",
                status_code=302)
    return RedirectResponse("/areas?msg=New+trail+picked", status_code=302)


@router.post("/areas/{slug}/import-trails")
async def area_import_trails(request: Request, slug: str):
    if not is_authed(request):
        return RedirectResponse("/areas", status_code=302)
    if not _area_or_404(slug):
        return RedirectResponse("/areas?msg=Unknown+area&status=error", status_code=302)
    added = areas.refresh_area_trails(slug)
    return RedirectResponse(f"/areas?msg=Imported+{added}+new+trails+from+komoot", status_code=302)


@router.post("/areas/{slug}/list-url")
async def area_set_list_url(request: Request, slug: str, list_url: str = Form("")):
    if not is_authed(request):
        return RedirectResponse("/areas", status_code=302)
    if _area_or_404(slug):
        db.area_list_url_set(slug, list_url.strip())
    return RedirectResponse("/areas?msg=List+URL+saved", status_code=302)


@router.post("/areas/{slug}/add-trip")
async def area_add_trip(request: Request, slug: str,
                        title: str = Form(...),
                        url: str = Form(""),
                        summary: str = Form("")):
    """Add a custom 'trip' card (operator-advertised guided trip) to the issue.
    Mirrors the manual article add in newsletter.py but stamps source='trip'."""
    if not is_authed(request):
        return RedirectResponse("/areas", status_code=302)
    if not _area_or_404(slug):
        return RedirectResponse("/areas?msg=Unknown+area&status=error", status_code=302)
    week_start, week_end = _issue_window()
    nl = db.newsletter_get_or_create(week_start, kind=slug,
                                     period_start=week_start, period_end=week_end)
    arts = db.article_list(nl["id"])
    position = max((a["position"] for a in arts), default=-1) + 1
    aid = db.article_insert(
        newsletter_id=nl["id"], hn_id=None, title=title.strip(), url=url.strip(),
        hn_score=0, hn_comments=0, relevance_score=10.0,
        relevance_reason="trip ad", position=position,
        included=1, source="trip", source_name="Trip",
    )
    if summary.strip():
        db.article_update(aid, summary=summary.strip())
    return RedirectResponse("/areas?msg=Trip+added", status_code=302)


@router.post("/areas/{slug}/article/{article_id}/remove")
async def area_remove_article(request: Request, slug: str, article_id: int):
    if not is_authed(request):
        return RedirectResponse("/areas", status_code=302)
    db.article_delete(article_id)
    return RedirectResponse("/areas?msg=Removed", status_code=302)


@router.get("/areas/{slug}/preview", response_class=HTMLResponse)
async def area_preview(request: Request, slug: str):
    if not is_authed(request):
        return HTMLResponse("", status_code=401)
    week_start, _ = _issue_window()
    nl = db.newsletter_get(week_start, kind=slug)

    def _placeholder(msg):
        return HTMLResponse(
            f'<!DOCTYPE html><html><body style="margin:0;padding:40px;background:#0d1117;'
            f'color:#6e7681;font-family:monospace;text-align:center;"><p>{msg}</p></body></html>',
            headers=_PREVIEW_HEADERS,
        )
    if not nl:
        return _placeholder("Not built yet — hit Build for this area.")
    arts = db.article_list(nl["id"])
    if not any(a.get("included", 1) for a in arts):
        return _placeholder("No content yet.")
    return HTMLResponse(
        mailer.render_email_html(nl, arts), headers=_PREVIEW_HEADERS,
    )


@router.post("/areas/{slug}/send-test")
async def area_send_test(request: Request, slug: str, test_recipient: str = Form(...)):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    week_start, _ = _issue_window()
    ok, msg = mailer.send_test_email(week_start, test_recipient, kind=slug)
    status = "ok" if ok else "error"
    return RedirectResponse(f"/areas?msg={msg.replace(' ', '+')}&status={status}", status_code=302)


@router.post("/areas/{slug}/send")
async def area_send(request: Request, slug: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    week_start, _ = _issue_window()
    ok, msg = mailer.send_newsletter(week_start, kind=slug)
    status = "ok" if ok else "error"
    return RedirectResponse(f"/areas?msg={msg.replace(' ', '+')}&status={status}", status_code=302)


# ── Trail list ────────────────────────────────────────────────────────────────

@router.get("/trails", response_class=HTMLResponse)
async def trails_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    grouped = []
    for area in areas.AREAS:
        grouped.append({"area": area, "trails": db.trail_list(area["slug"])})
    return templates.TemplateResponse(request, "trails.html", {
        "grouped": grouped,
        "area_slugs": areas.area_slugs(),
    })


@router.post("/trails/add")
async def trails_add(request: Request,
                     area: str = Form(...),
                     name: str = Form(...),
                     alltrails_url: str = Form(""),
                     difficulty: str = Form("moderate"),
                     length_mi: float = Form(0),
                     blurb: str = Form("")):
    if not is_authed(request):
        return RedirectResponse("/trails", status_code=302)
    if area not in areas.area_slugs():
        return RedirectResponse("/trails?msg=Unknown+area&status=error", status_code=302)
    if difficulty not in ("easy", "moderate", "difficult"):
        difficulty = "moderate"
    db.trail_create(area, name.strip(), alltrails_url.strip(), difficulty, length_mi, blurb.strip())
    return RedirectResponse("/trails?msg=Trail+added", status_code=302)


@router.post("/trails/{trail_id}/toggle")
async def trails_toggle(request: Request, trail_id: int):
    if not is_authed(request):
        return RedirectResponse("/trails", status_code=302)
    cur = next((t for t in db.trail_list() if t["id"] == trail_id), None)
    if cur:
        db.trail_update(trail_id, active=0 if cur["active"] else 1)
    return RedirectResponse("/trails", status_code=302)


@router.post("/trails/{trail_id}/delete")
async def trails_delete(request: Request, trail_id: int):
    if not is_authed(request):
        return RedirectResponse("/trails", status_code=302)
    db.trail_delete(trail_id)
    return RedirectResponse("/trails?msg=Trail+removed", status_code=302)

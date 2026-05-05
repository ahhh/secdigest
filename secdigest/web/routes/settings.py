"""Routes: settings page and configuration save."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login, hash_password
import secdigest.scheduler as sched

router = APIRouter()


def _humanize_errors(cfg: dict) -> list[dict]:
    """Convert raw persisted error strings into human-readable notice dicts."""
    errors = []
    raw = cfg.get("last_curation_error", "")
    if raw:
        el = raw.lower()
        if "api_key" in el or "auth_token" in el or "authentication method" in el:
            headline = "Claude API key is missing or invalid"
            detail = ("Set the ANTHROPIC_API_KEY environment variable to enable AI-powered "
                      "article curation. Articles are currently being filtered by keyword matching instead.")
        elif "429" in el or "rate limit" in el or "rate_limit" in el:
            headline = "Claude API rate limit reached"
            detail = "Too many requests were sent to the Claude API. This will clear automatically on the next successful fetch."
        elif "quota" in el or "billing" in el or "credit" in el or "insufficient" in el:
            headline = "Claude API quota or billing issue"
            detail = "Check your Anthropic account usage and billing status at console.anthropic.com."
        elif "connect" in el or "timeout" in el or "network" in el or "name or service" in el:
            headline = "Could not reach the Claude API"
            detail = "The request timed out or the network is unreachable. Check your connection and try again."
        else:
            headline = "Article curation failed unexpectedly"
            detail = "Claude returned an error while scoring articles. Check the technical detail below."
        errors.append({"headline": headline, "detail": detail, "raw": raw})
    return errors


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    cfg = db.cfg_all()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "cfg": cfg,
        "errors": _humanize_errors(cfg),
        "audit": db.audit_recent(20),
    })


@router.post("/settings")
async def save_settings(request: Request):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()

    for field in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                  "fetch_time", "hn_min_score", "max_articles", "base_url"):
        if field in form:
            db.cfg_set(field, form[field])

    if form.get("smtp_pass"):
        db.cfg_set("smtp_pass", form["smtp_pass"])

    db.cfg_set("auto_send", "1" if form.get("auto_send") else "0")

    if form.get("new_password"):
        db.cfg_set("password_hash", hash_password(form["new_password"]))

    sched.reschedule(db.cfg_get("fetch_time"))
    return RedirectResponse("/settings?msg=Saved", status_code=302)


@router.post("/settings/clear-curation-error")
async def clear_curation_error(request: Request):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.cfg_set("last_curation_error", "")
    return JSONResponse({"ok": True})

"""Routes: settings page and configuration save."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login, hash_password
import secdigest.scheduler as sched

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "cfg": db.cfg_all(),
        "audit": db.audit_recent(20),
    })


@router.post("/settings")
async def save_settings(request: Request):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()

    for field in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                  "fetch_time", "hn_min_score", "max_articles"):
        if field in form:
            db.cfg_set(field, form[field])

    if form.get("smtp_pass"):
        db.cfg_set("smtp_pass", form["smtp_pass"])

    db.cfg_set("auto_send", "1" if form.get("auto_send") else "0")

    if form.get("new_password"):
        db.cfg_set("password_hash", hash_password(form["new_password"]))

    sched.reschedule(db.cfg_get("fetch_time"))
    return RedirectResponse("/settings?msg=Saved", status_code=302)

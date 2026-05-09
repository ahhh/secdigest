"""Routes: subscriber list management.

Admin-only counterpart to the public-site subscribe flow. Adding from
here bypasses double-opt-in (the operator is trusted), unlike the public
``/subscribe`` route which requires the email confirmation click.
"""
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login
from secdigest.web.csrf import verify_csrf

# Same pragmatic email regex used on the public side.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

router = APIRouter(dependencies=[Depends(verify_csrf)])


@router.get("/subscribers", response_class=HTMLResponse)
async def subscribers_page(request: Request):
    if not is_authed(request):
        return redirect_login()
    return templates.TemplateResponse("subscribers.html", {
        "request": request,
        "subscribers": db.subscriber_list(),
        "feedback_counts": db.feedback_counts_by_subscriber(),
    })


@router.post("/subscribers")
async def add_subscriber(request: Request, email: str = Form(...), name: str = Form("")):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    email_clean = (email or "").strip().replace("\r", "").replace("\n", "")
    if not _EMAIL_RE.match(email_clean):
        return RedirectResponse("/subscribers?msg=Invalid+email+address&status=error", status_code=302)
    result = db.subscriber_create(email_clean.lower(), name.strip())
    msg = "Added" if result else "Already+exists"
    return RedirectResponse(f"/subscribers?msg={msg}", status_code=302)


@router.post("/subscribers/{sub_id}/toggle")
async def toggle_subscriber(request: Request, sub_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    subs = db.subscriber_list()
    match = next((s for s in subs if s["id"] == sub_id), None)
    if match:
        db.subscriber_update(sub_id, active=0 if match["active"] else 1)
    return RedirectResponse("/subscribers", status_code=302)


@router.post("/subscribers/{sub_id}/cadence")
async def set_cadence(request: Request, sub_id: int, cadence: str = Form(...)):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    if cadence not in ("daily", "weekly", "monthly"):
        return RedirectResponse("/subscribers?msg=Bad+cadence&status=error", status_code=302)
    db.subscriber_update(sub_id, cadence=cadence)
    return RedirectResponse("/subscribers", status_code=302)


@router.post("/subscribers/{sub_id}/delete")
async def delete_subscriber(request: Request, sub_id: int):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.subscriber_delete(sub_id)
    return RedirectResponse("/subscribers", status_code=302)

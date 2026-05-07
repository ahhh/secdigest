"""Public routes: landing page, subscribe, confirm, unsubscribe."""
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from secdigest import config, db, mailer
from secdigest.web.security import (
    subscribe_allowed, subscribe_record,
    unsubscribe_allowed, unsubscribe_record,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_VALID_CADENCES = ("daily", "weekly", "monthly")


def _public_base_url() -> str:
    """Base URL the confirmation/unsubscribe links should point at. Falls back to
    the admin's base_url config if PUBLIC_BASE_URL isn't set."""
    return (config.PUBLIC_BASE_URL
            or db.cfg_get("base_url")
            or "http://localhost:8000").rstrip("/")


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, msg: str = "", status: str = ""):
    return templates.TemplateResponse("landing.html", {
        "request": request,
        "message": msg or None,
        "status": status or "info",
    })


@router.post("/subscribe", response_class=HTMLResponse)
async def subscribe(request: Request,
                    email: str = Form(...),
                    cadence: str = Form("daily"),
                    website: str = Form("")):
    # Honeypot — real browsers don't fill the hidden 'website' field.
    # Pretend success either way so bots can't probe whether they tripped it.
    if website.strip():
        return templates.TemplateResponse("thanks.html", {
            "request": request, "email": email,
        })

    if not subscribe_allowed(request):
        return templates.TemplateResponse("landing.html", {
            "request": request,
            "message": "Too many attempts from your network. Try again in an hour.",
            "status": "error",
        }, status_code=429)
    subscribe_record(request)

    email_clean = (email or "").strip().lower().replace("\r", "").replace("\n", "")
    if not _EMAIL_RE.match(email_clean):
        return templates.TemplateResponse("landing.html", {
            "request": request,
            "message": "That doesn't look like a valid email address.",
            "status": "error",
        }, status_code=400)
    if cadence not in _VALID_CADENCES:
        cadence = "daily"

    sub = db.subscriber_get_by_email(email_clean)
    if sub and sub.get("confirmed") and sub.get("active"):
        return templates.TemplateResponse("landing.html", {
            "request": request,
            "message": "You're already subscribed.",
            "status": "ok",
        })

    confirm_token = str(uuid.uuid4())
    if sub:
        # Re-issue confirmation: caller may have lost the previous email, or wants to
        # change cadence. Don't auto-activate yet — they still have to click the link.
        db.subscriber_set_confirm_token(sub["id"], confirm_token)
        db.subscriber_update(sub["id"], cadence=cadence)
    else:
        db.subscriber_create_pending(email_clean, cadence, confirm_token)

    confirm_url = f"{_public_base_url()}/confirm/{confirm_token}"
    ok, smtp_msg = mailer.send_confirmation_email(email_clean, confirm_url)
    if not ok:
        # Log it, but don't reveal SMTP details to the public. The admin can resend
        # by inspecting the row and re-triggering.
        print(f"[public] confirmation email failed for {email_clean}: {smtp_msg}")
        return templates.TemplateResponse("landing.html", {
            "request": request,
            "message": "We couldn't send the confirmation email right now. Please try again in a few minutes.",
            "status": "error",
        }, status_code=503)

    return templates.TemplateResponse("thanks.html", {
        "request": request,
        "email": email_clean,
    })


@router.get("/confirm/{token}", response_class=HTMLResponse)
async def confirm(request: Request, token: str):
    sub = db.subscriber_confirm(token)
    return templates.TemplateResponse("confirmed.html", {
        "request": request,
        "ok": sub is not None,
        "cadence": sub["cadence"] if sub else None,
    })


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(request: Request, token: str):
    if not unsubscribe_allowed(request):
        return templates.TemplateResponse("unsubscribed.html", {
            "request": request,
            "message": "Too many attempts from your network. Try again later.",
        }, status_code=429)
    unsubscribe_record(request)

    sub = db.subscriber_get_by_token(token)
    if sub and sub.get("active"):
        db.subscriber_unsubscribe_by_token(token)
        msg = "You've been unsubscribed from SecDigest."
    elif sub:
        msg = "You're already unsubscribed."
    else:
        msg = "This unsubscribe link is invalid or has already been used."
    return templates.TemplateResponse("unsubscribed.html", {
        "request": request, "message": msg,
    })

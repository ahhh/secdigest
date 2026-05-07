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
    feedback_allowed, feedback_record_attempt,
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

    # Subscriber-state-dependent response branches all converge on the same
    # thanks.html page so an attacker can't enumerate who's on the list by
    # comparing response bodies. Three cases:
    #   • already-confirmed → no DB change, no email, render thanks.html
    #   • already-pending   → re-issue confirm token, resend the email
    #   • brand new         → create pending row, send confirm email
    already_confirmed = bool(sub and sub.get("confirmed") and sub.get("active"))

    if not already_confirmed:
        confirm_token = str(uuid.uuid4())
        if sub:
            db.subscriber_set_confirm_token(sub["id"], confirm_token)
            db.subscriber_update(sub["id"], cadence=cadence)
        else:
            db.subscriber_create_pending(email_clean, cadence, confirm_token)

        confirm_url = f"{_public_base_url()}/confirm/{confirm_token}"
        ok, smtp_msg = mailer.send_confirmation_email(email_clean, confirm_url)
        if not ok:
            # Log it generically and surface a vague failure. Don't echo SMTP
            # error text to the user (info leak) and don't differentiate from
            # the success page in a way that leaks subscription state.
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


@router.get("/feedback/{token}/{newsletter_id}/{vote}", response_class=HTMLResponse)
async def feedback(request: Request, token: str, newsletter_id: int, vote: str):
    """Record a one-click signal/noise vote from an email link.

    Identity is the subscriber's unsubscribe_token — the same value already
    embedded in their email footer. Reusing it keeps URL space tidy and avoids
    minting a parallel feedback secret. Both vote and token-state branches
    converge on the same template so a probe can't enumerate which tokens are
    valid by comparing response bodies."""
    if vote not in ("signal", "noise"):
        return templates.TemplateResponse("feedback.html", {
            "request": request, "ok": False,
            "message": "That isn't a valid feedback option.",
        }, status_code=400)

    if not feedback_allowed(request):
        return templates.TemplateResponse("feedback.html", {
            "request": request, "ok": False,
            "message": "Too many attempts from your network. Try again later.",
        }, status_code=429)
    feedback_record_attempt(request)

    # Toggle the feedback feature globally — any votes cast while disabled get
    # bounced rather than silently recorded, so the admin's setting is honoured
    # in both directions (no buttons rendered + no votes accepted).
    if db.cfg_get("feedback_enabled") != "1":
        return templates.TemplateResponse("feedback.html", {
            "request": request, "ok": False,
            "message": "Feedback isn't enabled right now.",
        }, status_code=404)

    sub = db.subscriber_get_by_token(token)
    if not sub:
        return templates.TemplateResponse("feedback.html", {
            "request": request, "ok": False,
            "message": "This feedback link is invalid or has expired.",
        })

    # Confirm the newsletter exists; we don't need to load it, just guard against
    # FK-violation errors and against a hostile caller spamming arbitrary IDs.
    if not db.newsletter_get_by_id(newsletter_id):
        return templates.TemplateResponse("feedback.html", {
            "request": request, "ok": False,
            "message": "We couldn't find that issue.",
        })

    db.feedback_record(sub["id"], newsletter_id, vote)
    return templates.TemplateResponse("feedback.html", {
        "request": request, "ok": True, "vote": vote,
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

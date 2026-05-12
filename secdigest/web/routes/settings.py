"""Routes: settings page and configuration save.

Backs the /settings page where every DB-backed config key is editable.
Also hosts two diagnostic endpoints (``/settings/test-smtp`` and
``/settings/test-voice``) that verify credentials by actually connecting
to the upstream service and reporting which step failed — much more
useful than "send a real email and see what happens".
"""
import smtplib
import ssl
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from secdigest import crypto, db
from secdigest.web import templates
from secdigest.web.auth import is_authed, redirect_login, hash_password
from secdigest.web.csrf import verify_csrf
import secdigest.scheduler as sched

router = APIRouter(dependencies=[Depends(verify_csrf)])


def _humanize_errors(cfg: dict) -> list[dict]:
    """Convert raw persisted error strings into human-readable notice dicts.

    The fetcher stores its last LLM error verbatim in ``last_curation_error``.
    Surfacing that raw string at the top of the settings page would be
    confusing for an operator who doesn't know what e.g. ``401 invalid
    api_key`` means; we string-match on common patterns and surface a
    headline + actionable detail. The raw text is still passed through
    under ``raw`` so an advanced user can drill in.
    """
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
            detail = ("Too many requests were sent to the Claude API. "
                      "This will clear automatically on the next successful fetch.")
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
    """Single endpoint for the entire settings form. Each section is handled
    explicitly so we can apply the right transform per field (encryption
    for secrets, checkbox-to-flag for booleans, hash for the password)."""
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    form = await request.form()

    # Plain-text fields: copy through 1:1 if present in the form.
    # Anything not in the form is simply not updated, so partial saves work.
    for field in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                  "fetch_time", "hn_min_score", "max_articles", "max_curator_articles",
                  "relevance_threshold", "base_url",
                  "elevenlabs_voice_id", "elevenlabs_model", "elevenlabs_speed",
                  "aws_access_key_id", "aws_s3_bucket", "aws_s3_region", "aws_s3_prefix"):
        if field in form:
            db.cfg_set(field, form[field])

    # Secrets get encrypted via crypto.py before persisting. We only
    # update when the field is non-blank so the operator can leave the
    # password input empty to retain the existing stored value.
    if form.get("smtp_pass"):
        db.cfg_set("smtp_pass", crypto.encrypt(form["smtp_pass"]))
    # Both secrets are encrypted at rest with the same stream cipher used for
    # smtp_pass. We only write when a non-blank value is submitted so the
    # password-input "leave blank to keep current" UX behaves as advertised.
    if form.get("elevenlabs_api_key"):
        db.cfg_set("elevenlabs_api_key", crypto.encrypt(form["elevenlabs_api_key"]))
    if form.get("aws_secret_access_key"):
        db.cfg_set("aws_secret_access_key", crypto.encrypt(form["aws_secret_access_key"]))

    # Checkboxes: present-and-truthy = "1", absent = "0". Standard HTML
    # form behaviour is that an unchecked checkbox doesn't appear in the
    # body at all, so ``form.get(...)`` is the right way to read them.
    db.cfg_set("auto_send", "1" if form.get("auto_send") else "0")
    db.cfg_set("feedback_enabled", "1" if form.get("feedback_enabled") else "0")
    db.cfg_set("voice_summary_enabled", "1" if form.get("voice_summary_enabled") else "0")

    # Password change is its own field — bcrypt-hashed before storage.
    if form.get("new_password"):
        db.cfg_set("password_hash", hash_password(form["new_password"]))

    # If the operator changed fetch_time, the running scheduler needs
    # to be told about the new cron — otherwise we'd still fire at the old time.
    sched.reschedule(db.cfg_get("fetch_time"))
    return RedirectResponse("/settings?msg=Saved", status_code=302)


@router.post("/settings/test-smtp")
async def test_smtp(request: Request):
    """Walks the SMTP handshake and reports the outcome of each step
    (Connect → EHLO → STARTTLS → Login). Stops at the first failure so
    the operator can see exactly where things broke instead of getting a
    generic "send failed" later."""
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    cfg = db.cfg_all()
    host = cfg.get("smtp_host", "")
    port = int(cfg.get("smtp_port", 587))
    user = cfg.get("smtp_user", "")
    password = crypto.decrypt(cfg.get("smtp_pass", ""))
    steps = []

    # Local helper to keep each "step" recording one-liner.
    def step(label, ok, msg):
        steps.append({"label": label, "ok": ok, "msg": msg})

    if not host:
        step("Config", False, "smtp_host is not set")
        return JSONResponse({"ok": False, "steps": steps})

    step("Config", True, f"{host}:{port}  user={user or '(none)'}  pass={'set' if password else 'not set'}")

    context = ssl.create_default_context()
    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
        step("Connect", True, f"TCP connection to {host}:{port} OK")
    except Exception as e:
        step("Connect", False, str(e))
        return JSONResponse({"ok": False, "steps": steps})

    try:
        server.ehlo()
        step("EHLO", True, "Server accepted EHLO")
    except Exception as e:
        step("EHLO", False, str(e))
        server.close()
        return JSONResponse({"ok": False, "steps": steps})

    if port != 465:
        try:
            server.starttls(context=context)
            server.ehlo()
            step("STARTTLS", True, "TLS negotiated")
        except Exception as e:
            step("STARTTLS", False, str(e))
            server.close()
            return JSONResponse({"ok": False, "steps": steps})

    if user:
        try:
            server.login(user, password)
            step("Login", True, f"Authenticated as {user}")
        except smtplib.SMTPAuthenticationError as e:
            step("Login", False, f"Auth rejected — {e.smtp_error.decode(errors='replace')}")
            server.close()
            return JSONResponse({"ok": False, "steps": steps})
        except Exception as e:
            step("Login", False, str(e))
            server.close()
            return JSONResponse({"ok": False, "steps": steps})

    server.quit()
    return JSONResponse({"ok": True, "steps": steps})


@router.post("/settings/test-voice")
async def test_voice(request: Request):
    """Round-trip ElevenLabs + S3 with a tiny throwaway TTS clip. Confirms both
    sets of credentials are valid before an admin commits to enabling voice
    summaries on a real newsletter."""
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    from secdigest import voice
    ok, msg = voice.smoke_test()
    return JSONResponse({"ok": ok, "msg": msg})


@router.post("/settings/clear-curation-error")
async def clear_curation_error(request: Request):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    db.cfg_set("last_curation_error", "")
    return JSONResponse({"ok": True})

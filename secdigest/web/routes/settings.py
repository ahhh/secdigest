"""Routes: settings page and configuration save."""
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
                  "fetch_time", "hn_min_score", "max_articles", "max_curator_articles", "base_url",
                  "elevenlabs_voice_id", "elevenlabs_model",
                  "aws_access_key_id", "aws_s3_bucket", "aws_s3_region", "aws_s3_prefix"):
        if field in form:
            db.cfg_set(field, form[field])

    if form.get("smtp_pass"):
        db.cfg_set("smtp_pass", crypto.encrypt(form["smtp_pass"]))
    # Both secrets are encrypted at rest with the same stream cipher used for
    # smtp_pass. We only write when a non-blank value is submitted so the
    # password-input "leave blank to keep current" UX behaves as advertised.
    if form.get("elevenlabs_api_key"):
        db.cfg_set("elevenlabs_api_key", crypto.encrypt(form["elevenlabs_api_key"]))
    if form.get("aws_secret_access_key"):
        db.cfg_set("aws_secret_access_key", crypto.encrypt(form["aws_secret_access_key"]))

    db.cfg_set("auto_send", "1" if form.get("auto_send") else "0")
    db.cfg_set("feedback_enabled", "1" if form.get("feedback_enabled") else "0")
    db.cfg_set("voice_summary_enabled", "1" if form.get("voice_summary_enabled") else "0")

    if form.get("new_password"):
        db.cfg_set("password_hash", hash_password(form["new_password"]))

    sched.reschedule(db.cfg_get("fetch_time"))
    return RedirectResponse("/settings?msg=Saved", status_code=302)


@router.post("/settings/test-smtp")
async def test_smtp(request: Request):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    cfg = db.cfg_all()
    host = cfg.get("smtp_host", "")
    port = int(cfg.get("smtp_port", 587))
    user = cfg.get("smtp_user", "")
    password = crypto.decrypt(cfg.get("smtp_pass", ""))
    steps = []

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

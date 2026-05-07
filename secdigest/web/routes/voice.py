"""Routes: voice summary generation + status polling.

Three endpoints, each registered for all three kind/date prefixes (day, week,
month) so the curator UI can stay symmetrical across newsletter types:

  POST /{kind}/{date}/voice/generate   → kicks off background TTS, returns 202
  GET  /{kind}/{date}/voice/status     → JSON status snapshot for the polling UI
  POST /{kind}/{date}/voice/toggle     → flips the per-newsletter render flag

Generation runs on a daemon thread inside the same process; SQLite writes from
the worker go through the module-level lock in db.py so there's no cross-thread
hazard. We don't return the actual audio URL from /status — the URL is only
minted at email-send time so a leaked status response can't replay a presigned
URL past its window.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from secdigest import db, periods, voice
from secdigest.web.auth import is_authed
from secdigest.web.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


def _resolve(kind: str, date_str: str) -> dict | None:
    """Map (kind, date_str) → newsletter row. For digests we re-derive the
    period bounds from date_str rather than trusting a query param so a
    forged URL can't sidestep the bounds-validation done in digest.py."""
    if kind == "daily":
        return db.newsletter_get(date_str, kind="daily")
    if kind == "weekly":
        period_start, _ = periods.iso_week_bounds(date_str)
        return db.newsletter_get(period_start, kind="weekly")
    if kind == "monthly":
        period_start, _ = periods.month_bounds(date_str)
        return db.newsletter_get(period_start, kind="monthly")
    return None


def _kind_for_path(path_prefix: str) -> str:
    return {"day": "daily", "week": "weekly", "month": "monthly"}[path_prefix]


# ── /voice/generate ─────────────────────────────────────────────────────────

async def _generate(request: Request, kind: str, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    # The master toggle is the kill switch — we honour it on the generation
    # path as well as the render path so a stale curator tab can't kick off
    # an ElevenLabs charge after the admin disabled the feature.
    if db.cfg_get("voice_summary_enabled") != "1":
        return JSONResponse(
            {"error": "voice summaries are disabled in Settings"},
            status_code=403,
        )

    nl = _resolve(kind, date_str)
    if not nl:
        return JSONResponse({"error": "newsletter not found"}, status_code=404)

    voice.kick_off_generation(nl["id"], kind=kind)
    return JSONResponse({"status": "queued", "newsletter_id": nl["id"]},
                         status_code=202)


@router.post("/day/{date_str}/voice/generate")
async def gen_day(request: Request, date_str: str):
    return await _generate(request, "daily", date_str)


@router.post("/week/{date_str}/voice/generate")
async def gen_week(request: Request, date_str: str):
    return await _generate(request, "weekly", date_str)


@router.post("/month/{date_str}/voice/generate")
async def gen_month(request: Request, date_str: str):
    return await _generate(request, "monthly", date_str)


# ── /voice/status ───────────────────────────────────────────────────────────

async def _status(request: Request, kind: str, date_str: str):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    nl = _resolve(kind, date_str)
    if not nl:
        return JSONResponse({"status": "idle"})
    row = db.voice_audio_get(nl["id"])
    if not row:
        return JSONResponse({"status": "idle", "enabled": db.newsletter_get_voice_enabled(nl["id"])})
    return JSONResponse({
        "status": row["status"],
        "duration": row.get("duration_sec") or 0,
        "error": row.get("error") or "",
        "voice_text": row.get("voice_text") or "",
        "enabled": db.newsletter_get_voice_enabled(nl["id"]),
    })


@router.get("/day/{date_str}/voice/status")
async def status_day(request: Request, date_str: str):
    return await _status(request, "daily", date_str)


@router.get("/week/{date_str}/voice/status")
async def status_week(request: Request, date_str: str):
    return await _status(request, "weekly", date_str)


@router.get("/month/{date_str}/voice/status")
async def status_month(request: Request, date_str: str):
    return await _status(request, "monthly", date_str)


# ── /voice/toggle ───────────────────────────────────────────────────────────

async def _toggle(request: Request, kind: str, date_str: str,
                   enabled: str = Form("")):
    if not is_authed(request):
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    nl = _resolve(kind, date_str)
    if not nl:
        return JSONResponse({"error": "newsletter not found"}, status_code=404)
    db.newsletter_set_voice_enabled(nl["id"], enabled == "1")
    return JSONResponse({"ok": True, "enabled": enabled == "1"})


@router.post("/day/{date_str}/voice/toggle")
async def toggle_day(request: Request, date_str: str, enabled: str = Form("")):
    return await _toggle(request, "daily", date_str, enabled)


@router.post("/week/{date_str}/voice/toggle")
async def toggle_week(request: Request, date_str: str, enabled: str = Form("")):
    return await _toggle(request, "weekly", date_str, enabled)


@router.post("/month/{date_str}/voice/toggle")
async def toggle_month(request: Request, date_str: str, enabled: str = Form("")):
    return await _toggle(request, "monthly", date_str, enabled)

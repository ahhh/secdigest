"""Voice summary generation: ElevenLabs TTS → S3 upload → presigned URL.

This module is the single trust boundary for two third-party services. Two
properties matter for security:

  • Credentials never leak into logs, exception messages, or SMTP error text.
    `_redact()` is applied to every error string we surface; the API key never
    appears as a query string or logged kwarg.
  • The composed voice text is plain ASCII derived from already-summarised
    article fields, not raw user input — but we still sanitise to a length cap
    so a hostile feed item can't run up an ElevenLabs bill.

Generation runs on a daemon thread because TTS for 10–30s of speech takes
~10–20s wall-clock. We can't block an HTTP request that long, and we don't want
to drag in Celery/RQ for a feature that fires at most a few times per day.
The thread's only durable side-effects are DB writes and S3 puts; SQLite is
configured with check_same_thread=False and writes go through the module-level
_lock, so cross-thread access is safe.
"""
import io
import re
import threading
import uuid
from datetime import datetime, timedelta

import httpx

from secdigest import config, crypto, db


_ELEVENLABS_API = "https://api.elevenlabs.io/v1"

# Bytes-per-second estimate at ElevenLabs' default 128 kbps MP3 encoding. Used
# to derive a duration for the UI without pulling in a heavy MP3 parser.
# Actual bitrate varies by voice/model but the estimate is within ~10% — fine
# for a "0:32" label.
_BYTES_PER_SECOND_128KBPS = 16_000

# Hard cap on the text we send to ElevenLabs. Their character pricing is
# linear, and a runaway feed item shouldn't be able to drain the budget.
_MAX_TEXT_CHARS = 2_500
# Per-article summary cap inside the voice script. Keeps narration digestible
# (full summaries are 2-3 sentences; we want a one-liner) and bounds the worst
# case at 8 stories × ~180 chars ≈ 1.5kB before framing — comfortably under
# _MAX_TEXT_CHARS without needing a second-pass truncate.
_MAX_SUMMARY_CHARS_VOICE = 180

_REDACT_KEYS = ("api_key", "access_key", "secret", "password", "token")


def _redact(value: str) -> str:
    """Strip anything that smells like a credential out of a string before
    surfacing it to logs or the UI. The matchers are deliberately loose —
    false positives just produce '<redacted>' in an error message, false
    negatives leak a secret."""
    if not value:
        return ""
    out = str(value)
    for key in _REDACT_KEYS:
        out = re.sub(
            rf"({key}\s*[=:]\s*)[^\s,&]+",
            r"\1<redacted>",
            out,
            flags=re.IGNORECASE,
        )
    return out[:500]


# ── Text composition ────────────────────────────────────────────────────────

def _trim_summary_for_voice(summary: str, max_chars: int = _MAX_SUMMARY_CHARS_VOICE) -> str:
    """Pick a narrator-friendly slice of a written summary.

    Strategy: prefer the first complete sentence; fall back to a hard truncate
    on a word boundary. We don't want a half-sentence trailing into the next
    article's intro line, so an ellipsis is appended only when we cut mid-thought.
    """
    if not summary:
        return ""
    s = " ".join(summary.split())  # collapse whitespace/newlines for narration
    if len(s) <= max_chars:
        return s
    # First-sentence-fits-the-budget path
    end = -1
    for i, ch in enumerate(s[:max_chars]):
        if ch in ".!?" and i + 1 < len(s) and s[i + 1] in (" ", "\t"):
            end = i + 1
    if end > 0:
        return s[:end]
    # No clean sentence break — truncate at a word boundary with an ellipsis
    cut = s.rfind(" ", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    return s[:cut].rstrip() + "…"


def compose_voice_text(newsletter: dict, articles: list[dict],
                        kind: str = "daily") -> str:
    """Build the script we send to ElevenLabs. Kept under ~45s of narration on
    purpose — long enough to land each story, short enough that subscribers
    actually finish it. Each story gets a title line plus a one-sentence cut
    of its summary; full summaries are too long to listen to back-to-back."""
    included = [a for a in articles if a.get("included", 1)]
    if not included:
        return ""

    label = {"daily": "today's", "weekly": "this week's",
             "monthly": "this month's"}.get(kind, "today's")
    date_str = newsletter.get("date", "")
    n = len(included)
    plural = "story" if n == 1 else "stories"

    parts = [f"SecDigest, {date_str}.",
             f"{n} {plural} in {label} issue."]
    for i, a in enumerate(included[:8], 1):
        title = (a.get("title") or "").strip().rstrip(".")
        if not title:
            continue
        parts.append(f"Story {i}: {title}.")
        summary = _trim_summary_for_voice(a.get("summary") or "")
        if summary:
            parts.append(summary)
    if len(included) > 8:
        parts.append(f"And {len(included) - 8} more.")

    text = " ".join(parts)
    return text[:_MAX_TEXT_CHARS]


# ── ElevenLabs ──────────────────────────────────────────────────────────────

class VoiceConfigError(Exception):
    """Raised when settings haven't been wired up yet — distinguishes a config
    gap (no API key) from a transient generation failure (API down)."""


def _resolve_elevenlabs_config() -> dict:
    cfg = db.cfg_all()
    api_key_enc = cfg.get("elevenlabs_api_key", "")
    if not api_key_enc:
        raise VoiceConfigError("ElevenLabs API key not set in Settings")
    api_key = crypto.decrypt(api_key_enc)
    voice_id = cfg.get("elevenlabs_voice_id", "").strip()
    if not voice_id:
        raise VoiceConfigError("ElevenLabs voice ID not set in Settings")
    model = cfg.get("elevenlabs_model", "eleven_turbo_v2_5").strip()
    return {"api_key": api_key, "voice_id": voice_id, "model": model}


def _generate_audio_bytes(text: str) -> bytes:
    """POST to ElevenLabs and return MP3 bytes. The API key travels in the
    xi-api-key header — never as a query param, so it can't leak into reverse
    proxy access logs."""
    elc = _resolve_elevenlabs_config()
    url = f"{_ELEVENLABS_API}/text-to-speech/{elc['voice_id']}"
    payload = {
        "text": text,
        "model_id": elc["model"],
        # voice_settings are optional — defaults are reasonable for narration
    }
    headers = {
        "xi-api-key": elc["api_key"],
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, json=payload, headers=headers)
    if r.status_code != 200:
        # ElevenLabs returns JSON error bodies. Propagate the message but never
        # the headers (which echo the API key back in some envs).
        try:
            detail = r.json().get("detail", {})
            msg = detail.get("message") or str(detail) or r.text[:200]
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(_redact(f"ElevenLabs {r.status_code}: {msg}"))
    return r.content


def _estimate_duration_seconds(audio_bytes: bytes) -> int:
    """Rough estimate from byte count. Good enough for a UI label; replace with
    mutagen if you ever need frame-accurate timestamps."""
    if not audio_bytes:
        return 0
    return max(1, round(len(audio_bytes) / _BYTES_PER_SECOND_128KBPS))


# ── S3 ──────────────────────────────────────────────────────────────────────

def _resolve_s3_config() -> dict:
    cfg = db.cfg_all()
    bucket = cfg.get("aws_s3_bucket", "").strip()
    region = cfg.get("aws_s3_region", "").strip()
    if not bucket or not region:
        raise VoiceConfigError("AWS S3 bucket / region not set in Settings")
    access_key = cfg.get("aws_access_key_id", "").strip()
    secret_enc = cfg.get("aws_secret_access_key", "")
    secret = crypto.decrypt(secret_enc) if secret_enc else ""
    if not access_key or not secret:
        raise VoiceConfigError("AWS access key / secret not set in Settings")
    prefix = cfg.get("aws_s3_prefix", "secdigest/audio/").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return {
        "bucket": bucket, "region": region, "prefix": prefix,
        "access_key": access_key, "secret_key": secret,
    }


def _s3_client(s3cfg: dict):
    """Lazy import: boto3 isn't a hard dep until someone enables voice. Keeps
    cold-start time down on hosts that don't use this feature."""
    import boto3  # noqa: WPS433
    return boto3.client(
        "s3",
        region_name=s3cfg["region"],
        aws_access_key_id=s3cfg["access_key"],
        aws_secret_access_key=s3cfg["secret_key"],
    )


def _upload_to_s3(audio_bytes: bytes, newsletter_id: int) -> str:
    """Returns the S3 object key (not the URL — URLs are minted lazily so
    they can't expire before the email is sent)."""
    s3cfg = _resolve_s3_config()
    key = f"{s3cfg['prefix']}{newsletter_id}/{uuid.uuid4()}.mp3"
    client = _s3_client(s3cfg)
    client.put_object(
        Bucket=s3cfg["bucket"],
        Key=key,
        Body=audio_bytes,
        ContentType="audio/mpeg",
        # Strict cache header keeps presigned-URL replays from being cached
        # by intermediaries past the URL's expiry.
        CacheControl="private, max-age=0, no-store",
    )
    return key


def presigned_url(s3_key: str, expires_in: int = 7 * 24 * 3600) -> str:
    """7-day default expiry — long enough that a subscriber who reads the
    email a few days late still gets audio, short enough that a leaked URL
    decays quickly. AWS caps presigned-URL lifetime at 7 days for sigv4."""
    s3cfg = _resolve_s3_config()
    client = _s3_client(s3cfg)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": s3cfg["bucket"], "Key": s3_key},
        ExpiresIn=expires_in,
    )


# ── Orchestration ───────────────────────────────────────────────────────────

def _generate_pipeline(newsletter_id: int, kind: str):
    """The whole flow as one function so the thread entry point is trivial.

    Each stage updates the DB row so the polling UI can show fine-grained
    progress (queued → generating → ready). Failures are caught at the top of
    the function and persisted as status='failed' with a redacted message —
    the UI surfaces it directly to the admin."""
    try:
        db.voice_audio_upsert(newsletter_id, status="generating", error=None)

        nl = db.newsletter_get_by_id(newsletter_id)
        if not nl:
            raise RuntimeError(f"newsletter {newsletter_id} disappeared")
        if kind == "daily":
            articles = db.article_list(newsletter_id)
        else:
            articles = db.digest_article_list(newsletter_id)

        text = compose_voice_text(nl, articles, kind=kind)
        if not text:
            raise RuntimeError("no included articles to narrate")
        db.voice_audio_upsert(newsletter_id, voice_text=text)

        audio = _generate_audio_bytes(text)
        s3_key = _upload_to_s3(audio, newsletter_id)
        db.voice_audio_upsert(
            newsletter_id,
            status="ready",
            s3_key=s3_key,
            duration_sec=_estimate_duration_seconds(audio),
        )
    except Exception as e:
        db.voice_audio_upsert(
            newsletter_id, status="failed", error=_redact(str(e)),
        )


def kick_off_generation(newsletter_id: int, kind: str) -> None:
    """Mark queued, then spin up a daemon thread. Returns immediately so the
    HTTP handler can respond 202 and the UI can start polling."""
    db.voice_audio_upsert(newsletter_id, status="queued", error=None)
    t = threading.Thread(
        target=_generate_pipeline,
        args=(newsletter_id, kind),
        daemon=True,
        name=f"voice-gen-{newsletter_id}",
    )
    t.start()


# ── Test helper ─────────────────────────────────────────────────────────────

def smoke_test() -> tuple[bool, str]:
    """Tiny TTS + S3 round-trip used by the Settings 'Test' button. Generates
    a 4-word phrase to keep the API spend trivial, and uploads/deletes a probe
    object so the credentials are exercised end-to-end."""
    try:
        audio = _generate_audio_bytes("SecDigest voice smoke test.")
        s3cfg = _resolve_s3_config()
        client = _s3_client(s3cfg)
        probe_key = f"{s3cfg['prefix']}smoke-test-{uuid.uuid4()}.mp3"
        client.put_object(
            Bucket=s3cfg["bucket"], Key=probe_key, Body=audio,
            ContentType="audio/mpeg",
        )
        client.delete_object(Bucket=s3cfg["bucket"], Key=probe_key)
        return True, f"OK — {len(audio)} bytes generated and round-tripped via S3"
    except VoiceConfigError as e:
        return False, str(e)
    except Exception as e:
        return False, _redact(str(e))

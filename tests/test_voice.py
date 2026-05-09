"""Tests for the voice-summary feature.

Covers:
  • Schema migration (voice_audio table exists)
  • Text composition keeps the cap, includes title list
  • _redact() scrubs anything credential-shaped from error strings
  • Settings save round-trips encrypted secrets (api_key, aws_secret)
  • Voice routes: generate kicks off a build, status returns row, toggle persists
  • Master toggle is honoured on the generate path (kill switch works)
  • render_email_html positions the voice block above the TOC
  • Mailer's _voice_block_for returns empty string in every disabled branch
  • boto3 import is lazy — code paths that don't need S3 don't choke

The ElevenLabs HTTP call is intercepted by the existing stub_httpx fixture.
boto3 has no equivalent stub yet; we monkeypatch a tiny fake S3 client into
voice._s3_client so tests don't need AWS credentials or network."""
import pytest

from secdigest import crypto, db, mailer, voice
from tests.conftest import get_csrf


# ── Fakes ──────────────────────────────────────────────────────────────────

class _FakeS3Client:
    """Just enough of the boto3 S3 client surface for voice.py to feel at home.

    Records every put_object so tests can assert on bucket/key/body, and
    returns a stable fake presigned URL so tests can match it exactly."""
    def __init__(self):
        self.puts: list[dict] = []
        self.deletes: list[dict] = []
        self.urls: list[dict] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.urls.append({"op": op, "params": Params, "expires_in": ExpiresIn})
        return f"https://fake-s3.example/{Params['Bucket']}/{Params['Key']}?expiry={ExpiresIn}"


@pytest.fixture
def fake_s3(monkeypatch):
    """Replace voice._s3_client so we never reach for boto3/AWS during tests."""
    fake = _FakeS3Client()
    monkeypatch.setattr(voice, "_s3_client", lambda cfg: fake)
    return fake


@pytest.fixture
def voice_creds(tmp_db):
    """Seed the DB with valid-looking voice + S3 credentials so the resolve
    helpers don't raise VoiceConfigError. Secrets go through crypto.encrypt
    matching the production save path."""
    db.cfg_set("voice_summary_enabled", "1")
    db.cfg_set("elevenlabs_api_key", crypto.encrypt("xi-testkey-123"))
    db.cfg_set("elevenlabs_voice_id", "test-voice")
    db.cfg_set("elevenlabs_model", "eleven_turbo_v2_5")
    db.cfg_set("aws_access_key_id", "AKIATEST")
    db.cfg_set("aws_secret_access_key", crypto.encrypt("aws-secret-456"))
    db.cfg_set("aws_s3_bucket", "secdigest-test")
    db.cfg_set("aws_s3_region", "us-east-1")
    db.cfg_set("aws_s3_prefix", "secdigest/audio/")


def _seed_daily():
    """Build a daily newsletter with two included articles — the minimum
    needed for compose_voice_text to produce a non-empty script."""
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="CVE in libfoo",
        url="https://x/a", hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="r", position=0, included=1,
    )
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="New malware family discovered",
        url="https://x/b", hn_score=0, hn_comments=0,
        relevance_score=8.0, relevance_reason="r", position=1, included=1,
    )
    return n


# ── Schema ─────────────────────────────────────────────────────────────────

def test_voice_audio_table_exists(tmp_db):
    cols = {r[1] for r in db._get_conn()
            .execute("PRAGMA table_info(voice_audio)").fetchall()}
    assert {"newsletter_id", "status", "s3_key", "duration_sec",
            "voice_text", "error"}.issubset(cols)


def test_voice_audio_upsert_round_trips(tmp_db):
    n = _seed_daily()
    db.voice_audio_upsert(n["id"], status="generating")
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k/1.mp3", duration_sec=42)
    row = db.voice_audio_get(n["id"])
    assert row["status"] == "ready"
    assert row["s3_key"] == "k/1.mp3"
    assert row["duration_sec"] == 42


def test_voice_audio_upsert_rejects_unknown_column(tmp_db):
    n = _seed_daily()
    with pytest.raises(ValueError):
        db.voice_audio_upsert(n["id"], status="ready", evil="DROP TABLE")


# ── Text composition ───────────────────────────────────────────────────────

def test_compose_voice_text_uses_titles_and_caps_to_eight(tmp_db):
    """Titles use NATO words to avoid colliding with the 'Story N:' label
    format the composer emits — otherwise 'Story 8' appears as a label even
    when only 8 articles are included, masking off-by-one bugs in the cap."""
    n = db.newsletter_get_or_create("2026-05-04")
    nato = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
            "golf", "hotel", "india", "juliet", "kilo", "lima"]
    for i, name in enumerate(nato):
        db.article_insert(
            newsletter_id=n["id"], hn_id=None, title=name,
            url=f"https://x/{i}", hn_score=0, hn_comments=0,
            relevance_score=9 - i * 0.1, relevance_reason="r", position=i, included=1,
        )
    text = voice.compose_voice_text(n, db.article_list(n["id"]))
    assert "12 stories" in text
    assert "alpha" in text and "hotel" in text  # first 8 included
    assert "india" not in text and "lima" not in text  # past the cap
    assert "And 4 more" in text


def test_compose_voice_text_inserts_pause_between_stories(tmp_db):
    """A <break> SSML tag should sit before every story announcement except
    the first — so the listener gets a clear beat between one story's
    summary and the next 'Story N:' intro. ElevenLabs treats unknown SSML
    as text, so silently dropping support for break tags would surface as
    the narrator literally reading 'break time zero point seven seconds' —
    which is exactly the kind of regression this test catches."""
    n = db.newsletter_get_or_create("2026-05-04")
    nato = ["alpha", "bravo", "charlie", "delta"]
    for i, name in enumerate(nato):
        db.article_insert(
            newsletter_id=n["id"], hn_id=None, title=name,
            url=f"https://x/{i}", hn_score=0, hn_comments=0,
            relevance_score=9 - i * 0.1, relevance_reason="r", position=i, included=1,
        )
    text = voice.compose_voice_text(n, db.article_list(n["id"]))
    # 4 stories → 3 inter-story pauses (none before Story 1)
    assert text.count('<break time="0.7s" />') == 3
    # Each break should sit immediately before a "Story N:" label
    for i in (2, 3, 4):
        assert f'<break time="0.7s" /> Story {i}:' in text
    # No leading break — Story 1 follows directly from the issue intro
    assert text.split("Story 1:")[0].count("<break") == 0


def test_compose_voice_text_includes_article_summaries(tmp_db):
    """The narrator should read each article's short summary alongside its
    title — voice subscribers shouldn't have to click through just to know
    what the story is about."""
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="Critical CVE in libfoo",
        url="https://x/a", hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="r", position=0, included=1,
    )
    db.article_update(1, summary="Remote code execution affecting libfoo 1.0–2.3. Patch landed.")
    text = voice.compose_voice_text(n, db.article_list(n["id"]))
    assert "Critical CVE in libfoo" in text
    assert "Remote code execution" in text


def test_compose_voice_text_trims_long_summaries(tmp_db):
    """Per-article summaries get capped so the total script stays under the
    overall char budget without relying on a tail truncate that could chop
    a sentence mid-word."""
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="t",
        url="https://x", hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="r", position=0, included=1,
    )
    long_summary = "Sentence one. " + ("padding " * 100)
    db.article_update(1, summary=long_summary)
    text = voice.compose_voice_text(n, db.article_list(n["id"]))
    # Either the first sentence fits or the trimmed slice ends with an ellipsis
    assert "Sentence one." in text or "…" in text
    assert "padding " * 80 not in text


def test_trim_summary_prefers_sentence_boundary():
    short = "First sentence here. Second one trails."
    out = voice._trim_summary_for_voice(short, max_chars=180)
    # Short input passes through unchanged
    assert out == short

    # Truncation at sentence boundary, no ellipsis when we got a clean cut
    sample = "First sentence. " + "x" * 500
    out = voice._trim_summary_for_voice(sample, max_chars=50)
    assert out == "First sentence."

    # No sentence break in budget → word-boundary truncate with ellipsis
    no_period = "word " * 100
    out = voice._trim_summary_for_voice(no_period, max_chars=30)
    assert out.endswith("…")
    assert " word" not in out[-3:]  # didn't cut mid-word


def test_compose_voice_text_empty_when_no_included(tmp_db):
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="excluded",
        url="https://x", hn_score=0, hn_comments=0,
        relevance_score=1.0, relevance_reason="r", position=0, included=0,
    )
    assert voice.compose_voice_text(n, db.article_list(n["id"])) == ""


def test_compose_voice_text_respects_max_chars(tmp_db):
    """Hard cap matters because ElevenLabs charges per character. A pathological
    feed item with a 10kB title shouldn't be able to drain the budget."""
    n = db.newsletter_get_or_create("2026-05-04")
    db.article_insert(
        newsletter_id=n["id"], hn_id=None, title="a" * 50_000,
        url="https://x", hn_score=0, hn_comments=0,
        relevance_score=9.0, relevance_reason="r", position=0, included=1,
    )
    text = voice.compose_voice_text(n, db.article_list(n["id"]))
    assert len(text) <= voice._MAX_TEXT_CHARS


# ── Redaction ──────────────────────────────────────────────────────────────

# ── Speed setting ──────────────────────────────────────────────────────────

def test_clamp_speed_handles_blank_and_garbage():
    """Settings typos shouldn't break voice generation. A blank or
    unparseable value falls back to the default rather than raising."""
    assert voice._clamp_speed(None) == voice._SPEED_DEFAULT
    assert voice._clamp_speed("") == voice._SPEED_DEFAULT
    assert voice._clamp_speed("not-a-number") == voice._SPEED_DEFAULT


def test_clamp_speed_clips_to_elevenlabs_window():
    """ElevenLabs returns 422 outside 0.7–1.2; we clamp at write time so a
    bogus value doesn't fail mid-generation."""
    assert voice._clamp_speed("0.1") == voice._SPEED_MIN
    assert voice._clamp_speed("9.9") == voice._SPEED_MAX
    assert voice._clamp_speed("1.05") == 1.05  # in-window passes through


def test_generate_audio_bytes_passes_speed_in_voice_settings(
        tmp_db, voice_creds, monkeypatch):
    """Pin the wire-format contract: voice_settings.speed must arrive in
    every TTS request, sourced from the configured value, so a regression
    that drops it would surface as voice output reverting to default speed."""
    db.cfg_set("elevenlabs_speed", "1.15")
    captured = {}

    class _FakeResp:
        status_code = 200
        content = b"\xff\xfb" + b"\x00" * 100

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, json=None, headers=None):
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(voice.httpx, "Client", _FakeClient)
    voice._generate_audio_bytes("hello world")

    vs = captured["json"]["voice_settings"]
    assert vs["speed"] == 1.15
    # API key never appears in body — header-only delivery
    assert "api_key" not in str(captured["json"]).lower()
    assert captured["headers"]["xi-api-key"] == "xi-testkey-123"


def test_redact_strips_credential_shaped_substrings():
    sample = "ElevenLabs 401: api_key=sk_live_abc123 invalid"
    out = voice._redact(sample)
    assert "sk_live_abc123" not in out
    assert "<redacted>" in out


def test_redact_handles_secret_and_token_keywords():
    sample = "boto3 error: secret=AKIASUPERSECRET access_key=AKIA1234 token=eyJ.foo"
    out = voice._redact(sample)
    for leak in ("AKIASUPERSECRET", "AKIA1234", "eyJ.foo"):
        assert leak not in out


# ── Settings persistence ──────────────────────────────────────────────────

async def test_settings_save_encrypts_voice_and_aws_secrets(admin_client):
    tok = await get_csrf(admin_client, "/settings")
    form = {
        "csrf_token": tok,
        "smtp_host": "smtp.test.invalid", "smtp_port": "587",
        "smtp_user": "", "smtp_from": "SecDigest <test@test.invalid>",
        "fetch_time": "00:00", "hn_min_score": "50",
        "max_articles": "15", "max_curator_articles": "10",
        "base_url": "http://localhost:8000",
        "voice_summary_enabled": "on",
        "elevenlabs_api_key": "xi-secret-from-form",
        "elevenlabs_voice_id": "rachel",
        "elevenlabs_model": "eleven_turbo_v2_5",
        "aws_access_key_id": "AKIANEW",
        "aws_secret_access_key": "aws-secret-from-form",
        "aws_s3_bucket": "my-bucket", "aws_s3_region": "us-east-1",
        "aws_s3_prefix": "secdigest/audio/",
    }
    r = await admin_client.post("/settings", data=form)
    assert r.status_code == 302

    # Round-trip the encryption: stored value must decrypt back to the input.
    stored_xi = db.cfg_get("elevenlabs_api_key")
    assert stored_xi != "xi-secret-from-form", "must be encrypted at rest"
    assert crypto.decrypt(stored_xi) == "xi-secret-from-form"

    stored_aws = db.cfg_get("aws_secret_access_key")
    assert stored_aws != "aws-secret-from-form"
    assert crypto.decrypt(stored_aws) == "aws-secret-from-form"


async def test_settings_save_keeps_secret_when_form_blank(admin_client):
    """The 'leave blank to keep current' UX must not zero out a previously
    saved API key when an admin tweaks an unrelated field."""
    db.cfg_set("elevenlabs_api_key", crypto.encrypt("original-key"))
    tok = await get_csrf(admin_client, "/settings")
    form = {
        "csrf_token": tok,
        "smtp_host": "smtp.test.invalid", "smtp_port": "587",
        "smtp_user": "", "smtp_from": "SecDigest <test@test.invalid>",
        "fetch_time": "00:00", "hn_min_score": "50",
        "max_articles": "15", "max_curator_articles": "10",
        "base_url": "http://localhost:8000",
        "elevenlabs_api_key": "",  # blank — should keep current
        "elevenlabs_voice_id": "rachel",
        "elevenlabs_model": "eleven_turbo_v2_5",
        "aws_access_key_id": "AKIANEW",
        "aws_secret_access_key": "",  # blank — should keep current
        "aws_s3_bucket": "my-bucket", "aws_s3_region": "us-east-1",
        "aws_s3_prefix": "secdigest/audio/",
    }
    await admin_client.post("/settings", data=form)
    assert crypto.decrypt(db.cfg_get("elevenlabs_api_key")) == "original-key"


# ── Routes ─────────────────────────────────────────────────────────────────

async def test_voice_generate_kicks_off_when_master_enabled(
        admin_client, voice_creds, fake_s3, monkeypatch):
    """The generate endpoint should return 202 and immediately mark the row
    queued. Stub _generate_pipeline to no-op so the test doesn't depend on
    threading scheduling."""
    n = _seed_daily()
    monkeypatch.setattr(voice, "_generate_pipeline", lambda *a, **kw: None)

    tok = await get_csrf(admin_client, "/day/2026-05-04")
    r = await admin_client.post(
        "/day/2026-05-04/voice/generate",
        headers={"X-CSRF-Token": tok},
    )
    assert r.status_code == 202
    row = db.voice_audio_get(n["id"])
    assert row["status"] == "queued"


async def test_voice_generate_blocked_when_master_disabled(
        admin_client, voice_creds):
    """Stale curator tab can't sneak past the kill switch."""
    _seed_daily()
    db.cfg_set("voice_summary_enabled", "0")
    tok = await get_csrf(admin_client, "/day/2026-05-04")
    r = await admin_client.post(
        "/day/2026-05-04/voice/generate",
        headers={"X-CSRF-Token": tok},
    )
    assert r.status_code == 403


async def test_voice_status_reflects_db_state(admin_client, voice_creds):
    n = _seed_daily()
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k/1.mp3",
                          duration_sec=42)
    r = await admin_client.get("/day/2026-05-04/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["duration"] == 42
    # Critically: status must NOT include the s3_key. Leaking it would
    # let an authed admin precompute presigned URLs out of band, which
    # defeats the 'mint at send time' design.
    assert "s3_key" not in body


async def test_voice_toggle_persists_per_newsletter(admin_client, voice_creds):
    n = _seed_daily()
    tok = await get_csrf(admin_client, "/day/2026-05-04")
    r = await admin_client.post(
        "/day/2026-05-04/voice/toggle",
        data={"enabled": "1"},
        headers={"X-CSRF-Token": tok},
    )
    assert r.status_code == 200
    assert db.newsletter_get_voice_enabled(n["id"]) is True
    await admin_client.post(
        "/day/2026-05-04/voice/toggle",
        data={"enabled": "0"},
        headers={"X-CSRF-Token": tok},
    )
    assert db.newsletter_get_voice_enabled(n["id"]) is False


# ── Email rendering ────────────────────────────────────────────────────────

def test_render_places_voice_block_above_toc(tmp_db):
    n = _seed_daily()
    arts = db.article_list(n["id"])
    voice_block = mailer._render_voice_block("https://fake/audio.mp3", duration_sec=42)
    body = mailer.render_email_html(
        n, arts, unsubscribe_url="http://x/u",
        include_toc=True, voice_block=voice_block,
    )
    voice_idx = body.find("Listen to this issue")
    toc_idx = body.find("Contents")
    art_idx = body.find("CVE in libfoo")
    assert voice_idx >= 0 and toc_idx >= 0 and art_idx >= 0
    assert voice_idx < toc_idx < art_idx, \
        f"order broken: voice={voice_idx} toc={toc_idx} art={art_idx}"


def test_render_omits_voice_when_block_empty(tmp_db):
    n = _seed_daily()
    body = mailer.render_email_html(n, db.article_list(n["id"]),
                                    unsubscribe_url="http://x/u")
    assert "Listen to this issue" not in body
    assert "{voice_block}" not in body


# ── _voice_block_for guards ────────────────────────────────────────────────

def test_voice_block_for_empty_when_master_off(tmp_db):
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "0")
    db.newsletter_set_voice_enabled(n["id"], True)
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k.mp3")
    assert mailer._voice_block_for(n["id"]) == ""


def test_voice_block_for_empty_when_per_newsletter_off(tmp_db):
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], False)
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k.mp3")
    assert mailer._voice_block_for(n["id"]) == ""


def test_voice_block_for_empty_when_status_not_ready(tmp_db):
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], True)
    db.voice_audio_upsert(n["id"], status="generating")
    assert mailer._voice_block_for(n["id"]) == ""


def test_voice_block_for_empty_when_presign_fails(tmp_db, monkeypatch):
    """A broken S3 config must NOT block the email. Silent fallback to no
    voice block is the right behaviour; failing the whole send because the
    audio link can't be minted would be worse than just dropping the audio."""
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], True)
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k.mp3")

    def boom(*a, **kw):
        raise RuntimeError("S3 unreachable")
    monkeypatch.setattr(voice, "presigned_url", boom)
    assert mailer._voice_block_for(n["id"]) == ""


def test_voice_block_for_preview_shows_placeholder_when_audio_missing(tmp_db):
    """The builder iframe should reflect the toggle even when audio hasn't
    been generated yet — admins iterate on layout BEFORE spending ElevenLabs
    credits, so we render a non-functional placeholder instead of nothing."""
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], True)
    block = mailer._voice_block_for_preview(n["id"])
    assert "Listen to this issue" in block
    assert "preview" in block.lower()
    # Placeholder href is a no-op anchor — no real S3 URL minted
    assert 'href="#"' in block


def test_voice_block_for_preview_uses_real_url_when_ready(tmp_db, monkeypatch):
    """When audio IS ready the preview should match the real send exactly,
    so the admin sees the live presigned URL (and can click to verify)."""
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], True)
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k.mp3", duration_sec=42)
    monkeypatch.setattr(voice, "presigned_url",
                        lambda key, **kw: f"https://fake/{key}")
    block = mailer._voice_block_for_preview(n["id"])
    assert "https://fake/k.mp3" in block
    assert "0:42" in block
    assert "preview" not in block.lower()


def test_voice_block_for_preview_empty_when_disabled(tmp_db):
    """Toggle off (per-newsletter or master) should suppress the preview block
    entirely — otherwise the iframe diverges from what would actually send."""
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], False)
    assert mailer._voice_block_for_preview(n["id"]) == ""

    db.newsletter_set_voice_enabled(n["id"], True)
    db.cfg_set("voice_summary_enabled", "0")
    assert mailer._voice_block_for_preview(n["id"]) == ""


async def test_day_preview_renders_voice_placeholder(admin_client):
    """End-to-end: hit the actual /day/{date}/preview route and confirm the
    iframe HTML carries the voice block when the toggle is on."""
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], True)
    r = await admin_client.get("/day/2026-05-04/preview")
    assert r.status_code == 200
    assert "Listen to this issue" in r.text


def test_voice_block_for_preview_renders_when_presign_fails(tmp_db, monkeypatch):
    """If audio is ready but S3 minting throws (rotated creds, etc.) the
    preview should still show SOMETHING — the placeholder — rather than a
    silent disappearance that misleads the admin into thinking the toggle
    isn't taking effect."""
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], True)
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k.mp3")
    monkeypatch.setattr(voice, "presigned_url",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    block = mailer._voice_block_for_preview(n["id"])
    assert "Listen to this issue" in block
    assert "preview" in block.lower()


def test_voice_block_for_renders_when_all_aligned(tmp_db, monkeypatch):
    n = _seed_daily()
    db.cfg_set("voice_summary_enabled", "1")
    db.newsletter_set_voice_enabled(n["id"], True)
    db.voice_audio_upsert(n["id"], status="ready", s3_key="k.mp3", duration_sec=33)
    monkeypatch.setattr(voice, "presigned_url", lambda key, **kw: f"https://fake/{key}")
    block = mailer._voice_block_for(n["id"])
    assert "Listen to this issue" in block
    assert "https://fake/k.mp3" in block
    assert "0:33" in block


# ── Pipeline integration ───────────────────────────────────────────────────

def test_generate_pipeline_writes_ready_row(tmp_db, voice_creds, fake_s3, monkeypatch):
    """Drive the whole synchronous body of _generate_pipeline (skipping the
    thread launch) with a fake ElevenLabs and a fake S3 client. The row
    should land in status='ready' with a duration estimate."""
    n = _seed_daily()
    monkeypatch.setattr(voice, "_generate_audio_bytes",
                        lambda text: b"\xff\xfb" + b"\x00" * 32_000)  # ~2s of audio
    voice._generate_pipeline(n["id"], "daily")
    row = db.voice_audio_get(n["id"])
    assert row["status"] == "ready"
    assert row["s3_key"] and row["s3_key"].endswith(".mp3")
    assert row["duration_sec"] >= 1
    # Confirm the audio was actually uploaded with the right content type
    assert fake_s3.puts and fake_s3.puts[0]["ContentType"] == "audio/mpeg"


def test_generate_pipeline_writes_failed_row_on_api_error(
        tmp_db, voice_creds, fake_s3, monkeypatch):
    n = _seed_daily()

    def boom(text):
        raise RuntimeError("ElevenLabs 401: api_key=sk_live_xyz invalid")
    monkeypatch.setattr(voice, "_generate_audio_bytes", boom)
    voice._generate_pipeline(n["id"], "daily")
    row = db.voice_audio_get(n["id"])
    assert row["status"] == "failed"
    assert "sk_live_xyz" not in (row["error"] or ""), \
        "credential leaked into error column"

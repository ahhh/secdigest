"""Shared pytest fixtures.

The smoke-test suite is meant to run on a remote CI box with no outbound network
and no real SMTP. Three blanket stub fixtures handle that:

  • stub_smtp     — patches mailer._smtp_send AND smtplib.SMTP/SMTP_SSL so every
                    code path that tries to send mail is intercepted and recorded.
  • stub_anthropic— patches anthropic.Anthropic so the curation/summarizer paths
                    never make real API calls. Tests can preload responses.
  • stub_httpx    — patches httpx.Client and httpx.AsyncClient so HN/RSS/article
                    fetches return canned data instead of hitting the wire.

Plus convenience fixtures: admin_client (already-logged-in async TestClient),
public_client (public-site TestClient), and a CSRF-token helper.

Almost every test wants the network blanket-stubbed; you can opt in via the
isolated fixtures, or use `full_stubs` to grab all three in one shot.
"""
import json
import re
from collections import deque
from typing import Any

import pytest

import secdigest.db as db_module
from secdigest import config


# ── Database / scheduler ─────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Redirect all DB operations to a throwaway SQLite file for the test.

    Also defangs env vars that would otherwise leak from the user's .env file
    into the test database via init_db's seed_config:

      • PASSWORD_HASH — if your real .env carries a production hash, init_db
        seeds the test DB with it, ensure_default_password no-ops because the
        hash is "set", and any test that logs in with the default password
        ("secdigest") fails because the hash is for a different password.
      • Anthropic / SMTP creds aren't read at seed time so they're harmless
        here — the stub fixtures handle them at call time.

    After the schema is initialised we explicitly clear password_hash and call
    ensure_default_password() so tests can rely on a known-good login.
    """
    monkeypatch.setenv("PASSWORD_HASH", "")
    monkeypatch.setattr(config, "DEFAULT_PASSWORD_HASH", "")
    monkeypatch.setitem(config.DB_CONFIG_DEFAULTS, "password_hash", "")

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    db_module._conn = None
    db_module.init_db()

    # Belt-and-suspenders: regardless of whatever leaked through, force the
    # password hash to be the bcrypt of "secdigest" so default-password tests
    # have a known starting state. ensure_default_password is idempotent — it
    # only writes when the current hash is falsy.
    db_module.cfg_set("password_hash", "")
    from secdigest.web.auth import ensure_default_password
    ensure_default_password()

    yield db_path
    if db_module._conn:
        db_module._conn.close()
        db_module._conn = None


@pytest.fixture
def mock_scheduler(monkeypatch):
    """No-op the APScheduler so tests don't spin up cron threads."""
    monkeypatch.setattr("secdigest.scheduler.start_scheduler", lambda: None)
    monkeypatch.setattr("secdigest.scheduler.stop_scheduler", lambda: None)


# ── SMTP blanket stub ────────────────────────────────────────────────────────

class _SentMail(list):
    """list subclass with helpers for asserting on captured outbound mail."""
    def to(self, addr: str) -> list[dict]:
        return [m for m in self if m.get("to", "").lower() == addr.lower()]

    def with_subject_containing(self, snippet: str) -> list[dict]:
        return [m for m in self if snippet.lower() in m.get("subject", "").lower()]


@pytest.fixture
def stub_smtp(monkeypatch, tmp_db):
    """Replace every outbound mail path with a recorder.

    Returns a list-like object with helpers .to(addr) and .with_subject_containing(s).
    Each captured message is a dict with keys: to, subject, body, kind ('transactional'
    for the confirmation-email helper, 'newsletter' for send_newsletter/test paths).

    Belt + braces:
      1. We patch mailer._smtp_send (the helper used for confirmation emails)
      2. We patch smtplib.SMTP and SMTP_SSL (used directly by send_newsletter and
         send_test_email — those paths bypass _smtp_send).
    """
    sent = _SentMail()

    from secdigest import mailer

    def fake_smtp_send(to_email, subject, html_body, text_body):
        sent.append({
            "to": to_email, "subject": subject,
            "body": html_body, "text": text_body,
            "kind": "transactional",
        })
        return True, "ok"

    monkeypatch.setattr(mailer, "_smtp_send", fake_smtp_send)

    class FakeSMTP:
        """Drop-in for smtplib.SMTP / SMTP_SSL. Implements just enough of the API
        for mailer.py to feel at home."""
        def __init__(self, host, port=25, **kwargs):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, pw):
            pass

        def send_message(self, msg):
            sent.append({
                "to": str(msg.get("To", "")),
                "subject": str(msg.get("Subject", "")),
                "body": "",
                "text": "",
                "kind": "newsletter",
            })

        def quit(self):
            pass

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)

    # Seed minimal SMTP config so the "SMTP not configured" guard passes — tests
    # that *want* to assert the guard fires can clear these explicitly.
    db_module.cfg_set("smtp_host", "smtp.test.invalid")
    db_module.cfg_set("smtp_from", "SecDigest <test@test.invalid>")

    return sent


# ── Anthropic blanket stub ───────────────────────────────────────────────────

class _AnthropicKnob:
    """Test-side handle for the fake Anthropic client.

    Tests mutate `.responses` to control what the next .messages.create() call
    returns. Each entry is the JSON-encodable payload Claude is meant to return
    in its single text block (e.g. {"score": 8, "reason": "..."}).
    """
    def __init__(self):
        self.responses: deque[Any] = deque()
        self.calls: list[dict] = []

    def queue(self, *payloads):
        for p in payloads:
            self.responses.append(p)

    def queue_score(self, score: float, reason: str = "mock"):
        self.responses.append({"score": score, "reason": reason})


@pytest.fixture
def stub_anthropic(monkeypatch):
    """Patch anthropic.Anthropic so the curation/summarizer paths don't hit the wire.

    The fake client returns whatever's at the head of `knob.responses` (FIFO).
    If the queue is empty, it falls back to a generic mid-relevance score so tests
    don't crash on missing setup.
    """
    knob = _AnthropicKnob()

    class FakeUsage:
        input_tokens = 100
        output_tokens = 30
        cache_read_input_tokens = 0

    class FakeContent:
        def __init__(self, text):
            self.text = text

    class FakeResponse:
        def __init__(self, text):
            self.content = [FakeContent(text)]
            self.usage = FakeUsage()

    class FakeMessages:
        def create(self_inner, **kwargs):
            knob.calls.append(kwargs)
            payload = (knob.responses.popleft() if knob.responses
                       else {"score": 6.5, "reason": "default mock"})
            return FakeResponse(json.dumps(payload) if not isinstance(payload, str) else payload)

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropicClient)
    return knob


# ── httpx blanket stub ───────────────────────────────────────────────────────

class _HttpxKnob:
    """Test-side handle for the fake httpx client.

    Configure URL → response mappings via .route(pattern, **kwargs); the fake
    client matches by substring against the requested URL. Patterns are checked
    in insertion order; first match wins. URLs that match nothing return 404.
    """
    def __init__(self):
        self._routes: list[tuple[str, dict]] = []
        self.calls: list[str] = []

    def route(self, pattern: str, *, status: int = 200,
              json_data: Any = None, text: str = ""):
        self._routes = [(p, r) for p, r in self._routes if p != pattern]
        self._routes.append((pattern, {
            "status": status, "json_data": json_data,
            "text": text or (json.dumps(json_data) if json_data is not None else ""),
        }))

    def lookup(self, url: str) -> dict:
        self.calls.append(url)
        for pattern, resp in self._routes:
            if pattern in url:
                return resp
        return {"status": 404, "json_data": None, "text": "not found"}


@pytest.fixture
def stub_httpx(monkeypatch):
    """Replace httpx.Client and httpx.AsyncClient with offline fakes.

    Returns a knob; tests use it like:
        knob.route("hacker-news/topstories.json", json_data=[1, 2, 3])
        knob.route("/item/1.json", json_data={"id": 1, "title": "..."})

    Important: tests that drive a FastAPI app via httpx use the same
    httpx.AsyncClient class — but they pass `transport=ASGITransport(app=...)`,
    which means they want the *real* httpx behaviour, not a canned-response
    stub. The fake classes below detect that case and pass through to the real
    httpx implementation. Only application code making genuine outbound calls
    (no transport kwarg) gets the offline fake.
    """
    import httpx
    knob = _HttpxKnob()

    # Capture real classes BEFORE we replace them so the pass-through path can
    # delegate to them.
    _RealAsyncClient = httpx.AsyncClient
    _RealSyncClient = httpx.Client

    class FakeResponse:
        def __init__(self, status, json_data, text):
            self.status_code = status
            self._json = json_data
            self.text = text
            self.headers = {}

        def json(self):
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"status {self.status_code}", request=None, response=self)

    def _build(resp_dict):
        return FakeResponse(resp_dict["status"], resp_dict["json_data"], resp_dict["text"])

    def _is_asgi(kwargs):
        """A test calling AsyncClient(transport=ASGITransport(app=...)) wants the
        real httpx — it's testing the FastAPI app, not making outbound calls."""
        t = kwargs.get("transport")
        return t is not None and t.__class__.__name__ == "ASGITransport"

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self._delegate = _RealAsyncClient(**kwargs) if _is_asgi(kwargs) else None

        async def __aenter__(self):
            if self._delegate is not None:
                return await self._delegate.__aenter__()
            return self

        async def __aexit__(self, *exc):
            if self._delegate is not None:
                return await self._delegate.__aexit__(*exc)
            return False

        async def get(self, url, **kwargs):
            if self._delegate is not None:
                return await self._delegate.get(url, **kwargs)
            return _build(knob.lookup(url))

        async def post(self, url, **kwargs):
            if self._delegate is not None:
                return await self._delegate.post(url, **kwargs)
            return _build(knob.lookup(url))

    class FakeSyncClient:
        def __init__(self, **kwargs):
            self._delegate = _RealSyncClient(**kwargs) if _is_asgi(kwargs) else None

        def __enter__(self):
            if self._delegate is not None:
                return self._delegate.__enter__()
            return self

        def __exit__(self, *exc):
            if self._delegate is not None:
                return self._delegate.__exit__(*exc)
            return False

        def get(self, url, **kwargs):
            if self._delegate is not None:
                return self._delegate.get(url, **kwargs)
            return _build(knob.lookup(url))

        def post(self, url, **kwargs):
            if self._delegate is not None:
                return self._delegate.post(url, **kwargs)
            return _build(knob.lookup(url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(httpx, "Client", FakeSyncClient)
    return knob


@pytest.fixture
def full_stubs(stub_smtp, stub_anthropic, stub_httpx):
    """Convenience: grab all three blanket stubs at once."""
    class Stubs:
        smtp = stub_smtp
        anthropic = stub_anthropic
        httpx = stub_httpx
    return Stubs


# ── Public-site rate-limit reset ─────────────────────────────────────────────

@pytest.fixture
def reset_rate_limits():
    """Clear the in-memory per-IP buckets so tests are deterministic."""
    from secdigest.web import security
    security._SUBSCRIBE_ATTEMPTS.clear()
    security._UNSUBSCRIBE_ATTEMPTS.clear()
    security._LOGIN_ATTEMPTS.clear()
    security._FEEDBACK_ATTEMPTS.clear()
    yield
    security._SUBSCRIBE_ATTEMPTS.clear()
    security._UNSUBSCRIBE_ATTEMPTS.clear()
    security._LOGIN_ATTEMPTS.clear()
    security._FEEDBACK_ATTEMPTS.clear()


# ── HTTP clients ─────────────────────────────────────────────────────────────

@pytest.fixture
async def admin_client(tmp_db, mock_scheduler, stub_smtp):
    """Authenticated TestClient against the admin app, ready for state-changing
    POSTs. Login is performed once during fixture setup; subsequent requests
    inherit the session cookie.
    """
    from httpx import AsyncClient, ASGITransport
    from secdigest.web.auth import hash_password

    db_module.cfg_set("password_hash", hash_password("testpw"))
    from secdigest.web.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/login", data={"password": "testpw"})
        assert r.status_code == 302, f"login failed: {r.status_code}"
        yield client


@pytest.fixture
async def public_client(tmp_db, stub_smtp, reset_rate_limits):
    """TestClient against the public app. Stubs SMTP + resets rate limits so
    each test starts clean."""
    from httpx import AsyncClient, ASGITransport
    from secdigest.public.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        yield client


# ── Helpers ──────────────────────────────────────────────────────────────────

async def get_csrf(client, path: str = "/archive") -> str:
    """Pull a CSRF token from any rendered admin page. The token is set on first
    template render and stored in the session cookie, so any prior GET that
    rendered a template will do."""
    r = await client.get(path)
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    if m:
        return m.group(1)
    m = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
    assert m, f"no CSRF token in page {path!r}"
    return m.group(1)

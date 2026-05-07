# Testing

The test suite is designed to run on a remote CI box with no outbound
network and no real SMTP. Three blanket-stub fixtures take care of that.

## Running

```bash
# From the repo root
pytest tests/ -v

# Just one file
pytest tests/test_full_pipeline.py -v

# Just one test (substring match)
pytest tests/ -v -k 'subscribe and rate_limit'

# With more detail on failures
pytest tests/ -v --tb=short
```

`pytest.ini` configures:

```ini
[pytest]
asyncio_mode = auto       # async tests don't need @pytest.mark.asyncio
testpaths = tests
pythonpath = .            # so `import secdigest` works without an installed pkg
```

## Layout

```
tests/
├── conftest.py                       # shared fixtures (this file is the heart)
├── test_auth.py                      # password hashing
├── test_startup.py                   # app lifespan + login
├── test_admin_routes_smoke.py        # every authed admin GET returns 200
├── test_db_migrations.py             # fresh install + legacy upgrade
├── test_digest_seed_atomic.py        # transaction rollback in digest_seed
├── test_full_pipeline.py             # the headline E2E test
├── test_mailer_smoke.py              # render escape, kind-aware send, cadence
├── test_public_site.py               # landing, DOI, unsubscribe, rate limits
├── test_security_limiter.py          # bucket eviction + safety cap
├── test_tls_config.py                # TLS env resolution + validation
└── test_xss_date_validator.py        # date_str path-param JS-context defence
```

## conftest.py — the fixture catalogue

Every fixture is in one file so there's one place to look. Categorised:

### Database

- **`tmp_db`** — Redirects `config.DB_PATH` to a tmp_path file, runs
  `init_db()`, defangs `PASSWORD_HASH` env-var leakage. Yields the
  path. Closes the connection on teardown.

- **`mock_scheduler`** — No-ops `scheduler.start_scheduler` /
  `stop_scheduler`. Use this for tests that go through the admin app's
  lifespan.

### Egress blockers

- **`stub_smtp`** — Patches both `mailer._smtp_send` AND `smtplib.SMTP`
  / `smtplib.SMTP_SSL` so every code path that tries to send mail is
  intercepted. Returns a `_SentMail` list with `.to(addr)` and
  `.with_subject_containing(s)` helpers.

  ```python
  def test_sends_email(stub_smtp, tmp_db):
      ...
      mailer.send_newsletter("2026-05-04", kind="daily")
      assert any("alice@" in m["to"] for m in stub_smtp)
      assert stub_smtp.with_subject_containing("Daily")
  ```

- **`stub_anthropic`** — Patches `anthropic.Anthropic` so the
  curation/summarizer paths don't hit the wire. Tests preload responses
  with `knob.queue_score(8.5, "reason")`.

  ```python
  def test_scoring(stub_anthropic, ...):
      stub_anthropic.queue_score(9.5, "Critical CVE")
      stub_anthropic.queue_score(2.0, "General tech")
      score_articles([{"title": "..."}])
      # second article would get the 2.0 score
  ```

- **`stub_httpx`** — Patches `httpx.Client` AND `httpx.AsyncClient` to
  return canned responses based on URL substring matching. Configure
  with `knob.route(pattern, json_data=...)`.

  ```python
  def test_fetch(stub_httpx):
      stub_httpx.route("topstories.json", json_data=[123, 456])
      stub_httpx.route("/item/123.json", json_data={"title": "..."})
      ...
  ```

  > **⚠️ Important** — The fake `AsyncClient` detects when a test passes
  > `transport=ASGITransport(app=...)` (i.e. it's driving an in-process
  > FastAPI app, not making outbound calls) and **delegates to the real
  > httpx client** in that case. This is why `admin_client` and tests
  > using TestClient still work even when `stub_httpx` is active. If you
  > add a new fixture that uses a non-ASGI httpx transport, double-check
  > it doesn't get swallowed by the canned-response path.

- **`full_stubs`** — Convenience fixture that grabs all three. Useful
  for the E2E tests:

  ```python
  def test_pipeline(full_stubs, tmp_db, ...):
      full_stubs.smtp.clear()
      full_stubs.anthropic.queue_score(8.0)
      full_stubs.httpx.route("...", json_data=...)
  ```

### Web clients

- **`admin_client`** — Authed `httpx.AsyncClient` against the admin
  app. Login is performed during fixture setup; subsequent requests
  inherit the session cookie. Pulls in `tmp_db`, `mock_scheduler`,
  `stub_smtp`.

  ```python
  async def test_archive(admin_client):
      r = await admin_client.get("/archive")
      assert r.status_code == 200
  ```

- **`public_client`** — Plain (unauthed) `httpx.AsyncClient` against
  the public app. Pulls in `tmp_db`, `stub_smtp`, `reset_rate_limits`.

### Helpers

- **`reset_rate_limits`** — Clears the in-memory IP-bucket dicts so
  tests are deterministic. Most tests that use rate-limited routes
  should depend on this.

- **`get_csrf(client, path)`** — Module-level function (not a fixture).
  Pulls a CSRF token from any rendered admin page. Use before any
  state-changing POST:

  ```python
  from tests.conftest import get_csrf

  async def test_pin(admin_client):
      tok = await get_csrf(admin_client, "/day/2026-05-04")
      r = await admin_client.post(
          f"/day/2026-05-04/article/{aid}/pin/weekly",
          data={"csrf_token": tok},
      )
  ```

## How to add a test

### A simple admin route check

1. Add the path to `STATIC_ROUTES` in `test_admin_routes_smoke.py` — gives
   you a free 200 OK assertion via the parametrised test.

That's enough for "does this page render at all?" coverage. For
state-changing routes, write a focused test:

```python
# tests/test_admin_some_feature.py
async def test_thing_happens(admin_client, stub_smtp):
    tok = await get_csrf(admin_client, "/some/page")
    r = await admin_client.post("/some/route", data={
        "csrf_token": tok,
        "field": "value",
    })
    assert r.status_code == 302
    # Then assert on db state, captured emails, etc.
```

### A new pipeline test

Use `full_stubs` and queue up the network responses:

```python
async def test_thing(tmp_db, full_stubs, mock_scheduler):
    # Mock HN endpoints
    full_stubs.httpx.route("topstories.json", json_data=[1])
    full_stubs.httpx.route("/item/1.json",
                            json_data={"id": 1, "type": "story",
                                       "title": "...", "url": "...",
                                       "score": 100})
    # Mock the curation response
    full_stubs.anthropic.queue_score(9.0, "test reason")
    # Mock summarizer
    full_stubs.httpx.route("example.invalid",
                            text="<html><body>article body</body></html>")
    full_stubs.anthropic.queue({"summary": "Test summary."})

    # Drive the pipeline
    n = await fetcher.run_fetch("2026-05-04")
    ...
```

### Testing a new public route

Use `public_client`:

```python
async def test_new_endpoint(public_client, stub_smtp):
    r = await public_client.post("/new-endpoint", data={...})
    assert r.status_code == 200
```

If the route is rate-limited, depend on `reset_rate_limits` (already
auto-pulled by `public_client`).

### Testing schema migrations

The pattern in `test_db_migrations.py`:

```python
def test_legacy_db_upgrades_cleanly(legacy_db):
    """Build a SQLite file with the OLD schema, run init_db, verify."""
```

The `legacy_db` fixture writes a pre-migration SQLite file via raw
`sqlite3` (not the project's `db.py`), then init_db is called and the
test asserts on the resulting state.

## The egress-blocking pattern

All three stubs share the same shape:

```python
@pytest.fixture
def stub_X(monkeypatch):
    knob = SomeKnob()  # configurable handle for the test
    class FakeY: ...   # drop-in replacement
    monkeypatch.setattr("the.real.module", FakeY)
    return knob
```

The "knob" gives the test fine-grained control over what the stub
returns. For example, `_AnthropicKnob.responses` is a `deque` that the
fake `messages.create()` pops from FIFO.

> **🔧 Why patch at the module level rather than passing fakes in?**
> The application code creates clients inline (`httpx.AsyncClient()`,
> `anthropic.Anthropic()`) — there's no DI container to inject fakes
> into. Patching the imported class at the module level catches
> everything without changing application code.

> **⚠️ Gotcha** — If a future contributor adds a new outbound HTTP call
> in a module that doesn't already use `httpx`, the stub_httpx fixture
> won't catch it. Convention: every outbound HTTP must go through
> `httpx`. If you reach for `requests` or `urllib`, the tests will
> silently make real network calls.

## TestClient + ASGITransport pattern

Inline pattern for one-off tests:

```python
from httpx import AsyncClient, ASGITransport
from secdigest.web.app import app

async def test_something(tmp_db, mock_scheduler):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.get("/login")
    assert r.status_code == 200
```

Most tests use the `admin_client` or `public_client` fixtures so they
don't repeat this boilerplate — but the inline form is fine for tests
that need custom client behaviour (e.g. testing without auth).

## What the suite covers

Approximate matrix of feature → test file:

| Feature                                    | Test file(s)                          |
|--------------------------------------------|---------------------------------------|
| Password hashing + ensure_default_password | `test_auth.py`                        |
| App startup + login                        | `test_startup.py`                     |
| Admin routes don't 500                     | `test_admin_routes_smoke.py`          |
| Schema migrations (fresh + legacy)         | `test_db_migrations.py`               |
| `digest_seed` atomicity                    | `test_digest_seed_atomic.py`          |
| Full HN→email pipeline                     | `test_full_pipeline.py`               |
| Email rendering escapes                    | `test_mailer_smoke.py`                |
| Send routing (kind-aware, cadence filter)  | `test_mailer_smoke.py`, `test_full_pipeline.py` |
| CRLF header injection prevention           | `test_mailer_smoke.py`                |
| Public landing renders                     | `test_public_site.py`                 |
| DOI subscribe flow                         | `test_public_site.py`                 |
| Honeypot + invalid email                   | `test_public_site.py`                 |
| Confirmation single-use                    | `test_public_site.py`                 |
| Subscribe enumeration defence              | `test_public_site.py`                 |
| Per-IP rate limit triggers                 | `test_public_site.py`, `test_security_limiter.py` |
| Limiter dict bounding                      | `test_security_limiter.py`            |
| TLS env resolution + validation            | `test_tls_config.py`                  |
| `date_str` JS-context XSS defence          | `test_xss_date_validator.py`          |

## Running on a remote CI

On the remote box (assuming the venv is at `/opt/secdigest/.venv`):

```bash
cd /opt/secdigest
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest tests/ -v
```

The tests don't need an `.env` — `tmp_db` defangs `PASSWORD_HASH`, the
stub fixtures replace SMTP/HTTP/Anthropic. **No outbound network is
used.** If a test hits the network, it's a bug in that test's
mocking setup.

## Common test-debugging recipes

**A test passes locally but fails on the remote.**

Most likely env-var leakage. The user's `.env` on the box has values
that leak through `config.py` import → `init_db()` seed → test DB. Check
that `tmp_db` defangs the relevant var; pattern is in
`tests/conftest.py:46-66`.

**`assert 404 == 302`-style failures in `test_full_pipeline.py`.**

The test's diagnostic helper `_check_redirect(r, url)` includes the URL
+ response body in the error message. Check that one — usually the
issue is a route the test is hitting doesn't exist (typo) or the admin
app didn't fully initialise (e.g. missing CSRF token in the POST).

**`stub_httpx` is intercepting tests that should reach the real app.**

The stub's `_is_asgi` check looks for `transport=ASGITransport(...)` in
the kwargs. If you're constructing `AsyncClient` differently (e.g.
custom transport), it'll get the canned-response path. Either pass an
ASGITransport explicitly or extend `_is_asgi` to recognise your case.

**Tests pass but `pytest -v` shows hundreds of warnings.**

Most are deprecation warnings from FastAPI / httpx pinned versions.
Suppress in `pytest.ini` if they're noise:

```ini
[pytest]
filterwarnings =
    ignore::DeprecationWarning:fastapi.*
    ignore::DeprecationWarning:starlette.*
```

(Don't suppress everything — application-code deprecations are signal.)

# SecDigest

A self-hosted security newsletter platform. Pulls security stories from
Hacker News and any number of RSS feeds, scores them for relevance with
Claude, generates summaries, and lets you curate and send daily, weekly,
or monthly digests to a subscriber list — with a public landing page
where new readers can sign up.

Two FastAPI apps share a single SQLite database:

- **Admin** (port 8080) — password-gated CMS for fetch, curate, build
  emails, manage subscribers, send.
- **Public** (port 8000) — the landing page subscribers see, with
  double-opt-in signup and per-user unsubscribe links.

Both run as separate uvicorn instances; either can be disabled.

---

## Features

### Curation & content

- **Multi-source ingestion** — HN top + new stories merged with any
  number of active RSS/Atom feeds, dedup'd against every URL ever stored.
- **AI scoring** — Claude Haiku scores each article 0–10 for security
  relevance. Prompt-cached system prompt → ~80% input-token discount on
  bulk runs. Falls back to keyword matching if Claude errors.
- **HN slot reservation** — reserves a configurable minimum number of HN
  articles in the daily pool so RSS-heavy days don't crowd HN out.
- **Summaries** — per-article body fetch + Claude summary, cached prompt.
  Editable inline or regenerate with one click (live spinner + polled
  auto-refresh).
- **Source attribution** — articles carry the originating feed name;
  surfaces in the curator's ⓘ tooltip and the meta line.

### Curator

- **Daily curator** at `/day/<date>` — drag-to-reorder, edit summaries
  inline, toggle inclusion, add manual articles or editorial notes
  (URL-less).
- **Pool view** at `/day/<date>/pool` — every article scored that day,
  promote/demote individually or auto-select the top N.
- **Pin to digest** — per-article toggle buttons that mark articles for
  inclusion in the upcoming weekly or monthly digest.

### Digests

- **Weekly + monthly digest curators** at `/week/<monday>` and
  `/month/<first-of-month>`.
- **Auto-seed** on first visit: every pinned article in the period, plus
  the highest-relevance remaining articles up to `max_curator_articles`.
- **Refresh selection** — re-seed any time, overwriting manual ordering.
- **Edit anything** — toggle inclusion in the digest, remove from the
  digest, drag-reorder. Source-day articles stay untouched.

### Email

- **Six built-in templates** — Dark Terminal, Clean Light, Minimal,
  2-Column Grid, Mobile Dark, Mobile Light (the last two are tuned for
  Gmail iOS).
- **Live iframe preview** with sandboxed CSP — what you see is what
  subscribers will get.
- **Inline template editor** — edit HTML directly from the builder, save
  back to the existing template or fork a new one.
- **Optional table of contents** with anchor links into the email.
- **Per-subscriber unsubscribe links** — each delivered email carries a
  unique UUID; one-click unsubscribe page on the public site.
- **CRLF header injection guard** — single sanitisation boundary on
  Subject / From / To.

### Public site

- **Standalone landing page** at port 8000 — separate app, no auth, runs
  alongside the admin.
- **Double-opt-in subscribe flow** — confirmation email with a
  single-use UUID token; row stays inactive until the link is clicked.
- **Per-IP rate limiting** on `/subscribe` (5/hr) and `/unsubscribe`
  (10/hr) with bounded in-memory buckets.
- **Cyber-noir theme** — deep-black base, dim-neon accents, scanlines +
  glitch text done subtly. Editable HTML and a single CSS file.
- **Honeypot** field traps bot signups silently.
- **Cadence selector** on signup — daily / weekly / monthly. Subscribers
  only get the cadence they chose; admin can change it any time on
  `/subscribers`.

### Subscribers

- Add manually from the admin (admin-add bypasses DOI), or via the
  public site (DOI-required).
- Per-row cadence dropdown — daily / weekly / monthly.
- Wildcard filter on the subscriber list (`*` and `?` patterns).
- Pause / resume / delete from the admin.

### Operations

- **APScheduler daily cron** at `FETCH_TIME` runs fetch + summarise
  automatically. Optional `auto_send=1` triggers sends after.
- **Direct TLS in uvicorn** — `TLS_DOMAIN` resolves to standard Let's
  Encrypt paths; explicit `TLS_CERTFILE`/`TLS_KEYFILE` override. Or
  disable for nginx-fronted deploys.
- **Defaults that fail loud** — refuses to start with the dev
  `SECRET_KEY`; refuses to send with `noreply@example.com`; refuses to
  serve TLS with no certs configured.
- **Forced password change** — middleware redirects to
  `/forced-password-change` until the default `secdigest` password is
  changed.
- **LLM audit log** — every Claude call's tokens + cached-token count +
  one-line result snippet, viewable in `/settings`.
- **Encrypted SMTP password at rest** — HMAC stream cipher keyed off
  `SECRET_KEY`.

### Tests

- **103 tests, fully offline** — SMTP, Anthropic, and httpx are blanket-
  stubbed. The CI box doesn't need network or SMTP credentials.
- **End-to-end pipeline test** drives HN fetch → curate → daily send →
  weekly digest → public subscribe → confirm → unsubscribe in one shot.
- **Fixture catalogue** in `tests/conftest.py` — `tmp_db`, `stub_smtp`,
  `stub_anthropic`, `stub_httpx`, `admin_client`, `public_client`,
  `reset_rate_limits`.

---

## Architecture at a glance

```
                 ┌────────────────────┐  ┌────────────────────┐
   admin user →  │  Admin app (8080)  │  │ Public app (8000)  │  ← anyone
                 │  password-gated    │  │  no auth, DOI flow │
                 └─────────┬──────────┘  └─────────┬──────────┘
                           │                       │
                           └─────────┬─────────────┘
                                     │
                       ┌─────────────────────────────┐
                       │  Shared business layer       │
                       │  • db.py (single sqlite3)   │
                       │  • mailer.py                │
                       │  • fetcher.py / rss.py      │
                       │  • summarizer.py            │
                       │  • scheduler.py             │
                       └─────────────┬───────────────┘
                                     ▼
                            ┌─────────────────┐
                            │ data/secdigest.db │  WAL mode
                            └─────────────────┘
```

Both apps run in one Python process by default — `run.py` spawns the
public app in a daemon thread when `PUBLIC_SITE_ENABLED=1`. For
production, prefer two systemd units; see [docs/deployment.md](docs/deployment.md).

---

## Requirements

- Python **3.11+**
- An [Anthropic API key](https://console.anthropic.com/) — Claude Haiku
  is cheap (~$0.01–$0.05/day at default settings)
- SMTP credentials for sending (optional — the UI is fully usable
  without)
- For TLS direct from uvicorn: a Let's Encrypt cert (run certbot
  yourself) or any cert pair

---

## Quick start

```bash
# 1. Clone and enter
git clone https://github.com/your-org/secdigest
cd secdigest

# 2. Create virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
$EDITOR .env
# Minimum required:
#   SECRET_KEY=<run: python -c "import secrets; print(secrets.token_urlsafe(32))">
#   ANTHROPIC_API_KEY=sk-ant-...
# For dev (no certs yet), also set:
#   TLS_ENABLED=0

# 4. Run (.env auto-loads from the project root)
python run.py
```

Open <http://localhost:8080> — default password is **`secdigest`**.
You'll be redirected to `/forced-password-change` on first login until
you set a real password.

To also run the public landing page:

```bash
# Add to .env
PUBLIC_SITE_ENABLED=1
PUBLIC_BASE_URL=http://localhost:8000
```

Restart `python run.py` and open <http://localhost:8000>.

> **📖 Detailed docs in [`docs/`](docs/)** — architecture, every route,
> the schema, the content pipeline, email rendering, configuration, TLS,
> deployment, testing, and a debugging guide. Start with
> [`docs/README.md`](docs/README.md) for an index.

---

## Configuration

Two layers:

1. **Process env** — read once at import time. Auto-loaded from `.env`.
2. **DB-backed** — runtime-editable on `/settings`, stored in `config_kv`.
   Most env vars seed these on first run.

Minimum env vars worth knowing — the full catalogue is in
[`docs/configuration.md`](docs/configuration.md):

| Variable                | Default                        | Notes |
|-------------------------|--------------------------------|-------|
| `SECRET_KEY`            | `dev-secret-change-me`         | App refuses to start with this default. |
| `ANTHROPIC_API_KEY`     | —                              | Required for Claude scoring + summaries. |
| `PASSWORD_HASH`         | auto                           | bcrypt hash; auto-generated from `secdigest` on first run. |
| `SMTP_HOST` / `_PORT` / `_USER` / `_PASS` / `_FROM` | — | SMTP creds. |
| `FETCH_TIME`            | `00:00`                        | 24h local time for the daily cron. |
| `HN_MIN_SCORE`          | `50`                           | HN points threshold for "top stories". |
| `HN_POOL_MIN`           | `10`                           | Reserved HN slots in the daily pool. |
| `MAX_ARTICLES`          | `15`                           | Pool size after scoring. |
| `MAX_CURATOR_ARTICLES`  | `10`                           | Top N auto-included in the daily. |
| `BASE_URL`              | `http://localhost:8000`        | Used in unsubscribe links. |
| `PUBLIC_SITE_ENABLED`   | `0`                            | Spawn the public app alongside admin. |
| `PUBLIC_HOST` / `_PORT` | `0.0.0.0` / `8000`             | Public app bind. |
| `PUBLIC_BASE_URL`       | —                              | URL inserted into confirm + unsub emails. |
| `TLS_ENABLED`           | `1`                            | Set `0` for dev or nginx-fronted prod. |
| `TLS_DOMAIN`            | —                              | Auto-resolves to `/etc/letsencrypt/live/<domain>/`. |
| `TLS_CERTFILE` / `TLS_KEYFILE` | —                       | Explicit cert paths (override `TLS_DOMAIN`). |

> **Gmail SMTP:** use `smtp.gmail.com`, port `587` or `465`, and a
> [16-char App Password](https://myaccount.google.com/apppasswords) —
> regular passwords are rejected. Spaces in the App Password work either
> way; preserve as Gmail shows them.

---

## Running in production

The recommended pattern is two systemd units behind nginx that
terminates TLS — see [`docs/deployment.md`](docs/deployment.md) for the
full recipe. Short version:

```ini
# /etc/systemd/system/secdigest.service
[Service]
User=www-data
WorkingDirectory=/opt/secdigest
EnvironmentFile=/opt/secdigest/.env
ExecStart=/opt/secdigest/.venv/bin/uvicorn secdigest.web.app:app \
    --host 127.0.0.1 --port 8080
```

```ini
# /etc/systemd/system/secdigest-public.service
[Service]
User=www-data
WorkingDirectory=/opt/secdigest
EnvironmentFile=/opt/secdigest/.env
ExecStart=/opt/secdigest/.venv/bin/uvicorn secdigest.public.app:app \
    --host 127.0.0.1 --port 8000
```

```nginx
# Public (subscribers see this) and admin on separate hostnames:
server {
    listen 443 ssl http2;
    server_name secdigest.example.com;
    ssl_certificate     /etc/letsencrypt/live/secdigest.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/secdigest.example.com/privkey.pem;
    location / { proxy_pass http://127.0.0.1:8000; ...standard headers... }
}
server {
    listen 443 ssl http2;
    server_name admin.secdigest.example.com;
    # IP-restrict the admin block in production
    location / { proxy_pass http://127.0.0.1:8080; ...standard headers... }
}
```

```bash
sudo certbot --nginx -d secdigest.example.com -d admin.secdigest.example.com
```

For direct uvicorn TLS (no nginx), see [`docs/tls.md`](docs/tls.md).

---

## Project structure

```
secdigest/
├── run.py                       # Dev entry point — spawns admin + (optional) public
├── pytest.ini                   # asyncio_mode=auto, pythonpath=.
├── requirements.txt
├── .env.example
├── secdigest/                   # Main Python package
│   ├── config.py                # Env loading, TLS resolution, DB defaults
│   ├── db.py                    # All SQLite — schema, migrations, every helper
│   ├── crypto.py                # HMAC stream cipher for SMTP password at rest
│   ├── periods.py               # ISO-week + month bounds for digest grouping
│   ├── fetcher.py               # HN fetch + Claude scoring + HN slot reservation
│   ├── rss.py                   # RSS/Atom parser (stdlib XML)
│   ├── summarizer.py            # Per-article body fetch + Claude summary
│   ├── mailer.py                # Render + send (kind-aware, cadence filter)
│   ├── scheduler.py             # APScheduler daily job
│   ├── web/                     # ADMIN APP (port 8080)
│   │   ├── app.py               # FastAPI + lifespan + auth routes
│   │   ├── auth.py              # bcrypt + session helpers
│   │   ├── csrf.py              # CSRF tokens + verify_csrf dependency
│   │   ├── security.py          # SSRF guards + per-IP rate limiters
│   │   ├── routes/
│   │   │   ├── newsletter.py            # Day curator + archive + article ops
│   │   │   ├── digest.py                # Weekly + monthly curator
│   │   │   ├── feeds.py                 # RSS feed CRUD + HN pool min
│   │   │   ├── prompts.py               # Curation/summary prompt CRUD
│   │   │   ├── subscribers.py           # Subscriber list + cadence
│   │   │   ├── settings.py              # Settings + SMTP test + audit log
│   │   │   ├── email_templates_route.py # Template CRUD
│   │   │   └── unsubscribe.py           # Public-side leftover (also on public app)
│   │   ├── templates/           # Jinja2
│   │   └── static/              # CSS, JS, favicon
│   └── public/                  # PUBLIC SITE (port 8000)
│       ├── app.py               # Standalone FastAPI app
│       ├── routes.py            # / , /subscribe, /confirm, /unsubscribe
│       ├── templates/           # Cyber-noir landing + thanks/confirmed/unsub
│       └── static/              # Editable CSS, favicon
├── tests/                       # 103 tests, fully offline
└── docs/                        # 12 markdown files — see docs/README.md
```

---

## How it works

### 1. Daily fetch

Triggered by APScheduler at `FETCH_TIME`, or manually via the **Fetch HN**
button on the day curator:

1. Pulls HN top 200 + new 100; filters by `HN_MIN_SCORE`.
2. Fetches every active RSS/Atom feed, capped per-feed by `max_articles`.
3. Dedups by URL against every article ever stored. Editorial notes
   (blank URL) are always allowed.
4. Scores each article via Claude Haiku — per-article messages with
   cache-controlled system prompt for cost efficiency.
5. Reserves the top `HN_POOL_MIN` HN articles, then fills the rest of
   the `MAX_ARTICLES` pool by relevance from leftover HN + RSS.
6. Top `MAX_CURATOR_ARTICLES` are auto-included in the daily; the rest
   stay in the pool for manual promotion.

### 2. Curation

The day curator (`/day/<date>`) is a drag-reorder list with:

- ▲ relevance score and source meta (HN points + comments, or feed name,
  or "editorial note")
- ⓘ tooltip: `Source: <feed/Hacker News/Manual> — <reason>`
- Inline summary editor (Edit / Regenerate / save)
- Toggle Include/Exclude
- **Pin weekly** / **Pin monthly** — flags the article for the upcoming
  digest. Pinning while a digest already exists for that period appends
  the article to the digest's join immediately.

### 3. Digest

A weekly digest is a `newsletters` row with `kind='weekly'`,
`period_start=<Monday>`. It doesn't own articles directly — it
references daily articles through a `digest_articles` join table. So
editing a summary on Tuesday's daily curator updates the weekly digest
automatically.

Auto-seed (first visit to `/week/<monday>`):

1. Every article with `pin_weekly=1` whose source date is in the period.
2. Top-up to `max_curator_articles` with the highest-relevance remaining
   curated articles in the period.

The curator can toggle/remove/reorder; "Refresh selection" re-seeds.

Monthly digests work identically with `period_start=<first-of-month>`.

### 4. Email builder

`/day/<date>?view=builder` (or the equivalent for digests):

- Template select (six built-ins or any custom you've created)
- Subject line + table-of-contents toggle
- Live iframe preview with a sandboxed CSP
- Inline editor for the wrapper HTML and per-article HTML
- Send Test box for one-off verification

### 5. Sending

`mailer.send_newsletter(date, kind)` is **cadence-aware**:

```python
subscribers = db.subscriber_active(cadence=kind)
```

A daily send only reaches `cadence='daily'` subscribers; weekly only
`cadence='weekly'`; monthly only `cadence='monthly'`. The sets are
disjoint by design.

Each delivered email carries that subscriber's unique
`unsubscribe_token` in the URL.

### 6. Public subscribe flow

1. Visitor lands on `/`. Picks daily / weekly / monthly. Submits.
2. POST `/subscribe` rate-limits per-IP, validates email, drops in a
   `confirmed=0, active=0` row with a fresh `confirm_token`.
3. Confirmation email goes out via `_smtp_send`. Body contains
   `<PUBLIC_BASE_URL>/confirm/<token>`.
4. Visitor clicks the link → `subscriber_confirm(token)` atomically
   sets `confirmed=1, active=1, confirm_token=NULL`.
5. From then on, sends matching the chosen cadence reach this
   subscriber.
6. Every email contains an unsubscribe link with a separate per-user
   UUID that never rotates.

The public response is identical for "new signup" vs "already
subscribed" — no enumeration.

---

## Email templates — placeholders

Six built-ins ship; create custom templates from `/email-templates`.
Each template has two parts:

- `html` — the wrapper, with `{articles}` substituted in
- `article_html` — the per-article snippet repeated for each included
  article

| Wrapper placeholder | Substituted with |
|---------------------|------------------|
| `{articles}`        | Concatenated `<tr>` rows from `article_html` |
| `{articles_2col}`   | 2-column grid (used when the wrapper contains this literal) |
| `{date}`            | `newsletter.date` (or `period_start` for digests) |
| `{unsubscribe_url}` | Per-recipient URL |

| Article placeholder | Substituted with |
|---------------------|------------------|
| `{number}`          | 1-indexed position |
| `{title}`           | Article title (HTML-escaped) |
| `{url}`             | Article URL (rejected unless http(s)) |
| `{hn_url}`          | HN discussion URL |
| `{summary}`         | AI-generated summary (HTML-escaped) |
| `{hn_score}`        | HN points |
| `{hn_comments}`     | HN comment count |

Full details in [`docs/email.md`](docs/email.md).

---

## Testing

103 tests, fully offline.

```bash
pytest tests/ -v
```

The suite blanket-stubs SMTP, Anthropic, and httpx (with smart
delegation when an `ASGITransport` is passed for in-process app
testing). The `tmp_db` fixture defangs `PASSWORD_HASH` env-var leakage
so your real `.env` doesn't pollute test state.

Coverage:

- Auth + session + CSRF
- Schema migrations (fresh + legacy upgrade + idempotency)
- Every admin route's GET (smoke)
- The full HN-fetch → curate → daily-send → digest → public-subscribe →
  confirm → unsubscribe pipeline
- Email rendering (escaping, kind-aware send, cadence filter, CRLF
  injection guard)
- Public site flows (DOI, honeypot, rate limits, enumeration defence)
- Per-IP rate-limit bucket bounding
- TLS env resolution + validation
- JS-context XSS defence on `date_str` path params
- `digest_seed` transactional rollback

See [`docs/testing.md`](docs/testing.md) for the fixture catalogue and
how to add tests.

---

## Documentation

| Topic | File |
|-------|------|
| System overview, components, lifecycle of an article | [`docs/architecture.md`](docs/architecture.md) |
| Schema, every column, all migrations, sqlite cheat sheet | [`docs/data-model.md`](docs/data-model.md) |
| Admin auth + CSRF + full route catalogue + curator UIs | [`docs/admin-app.md`](docs/admin-app.md) |
| Public landing page, DOI flow, theme customisation | [`docs/public-site.md`](docs/public-site.md) |
| Fetcher, RSS, scoring, summarizer | [`docs/content-pipeline.md`](docs/content-pipeline.md) |
| Email templates, kind-aware send, cadence filter | [`docs/email.md`](docs/email.md) |
| Every env var + DB-backed config + Settings UI | [`docs/configuration.md`](docs/configuration.md) |
| TLS plumbing, certbot, renewal hooks | [`docs/tls.md`](docs/tls.md) |
| Production deploy: systemd, nginx, ports | [`docs/deployment.md`](docs/deployment.md) |
| Test layout, fixtures, how to add tests | [`docs/testing.md`](docs/testing.md) |
| Symptom-paired troubleshooting guide | [`docs/debugging.md`](docs/debugging.md) |

---

## Contributing

```bash
git clone https://github.com/your-org/secdigest
cd secdigest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env  # set SECRET_KEY, ANTHROPIC_API_KEY, TLS_ENABLED=0
python run.py
```

When adding features:

- **DB changes go in `secdigest/db.py`** — schema first, then a
  migration function, then helper(s). All migrations are idempotent.
- **Admin routes** live under `secdigest/web/routes/` — one file per
  feature. Routers attach `verify_csrf` at the router level.
- **Public routes** stay minimal — every one is an attack surface and
  needs rate-limit analysis. See [`docs/public-site.md`](docs/public-site.md).
- **Templates** are Jinja2 with `autoescape=True`. JS contexts get
  `|tojson`.
- **Tests** in `tests/` — every new feature needs at least a smoke
  test. Use the existing fixtures (`tmp_db`, `stub_smtp`,
  `stub_anthropic`, `stub_httpx`, `admin_client`, `public_client`).
- **Outbound network** must go through `httpx` so `stub_httpx` catches
  it. Don't reach for `requests`.
- **No new dependencies** without strong justification — the project is
  deliberately small (FastAPI + SQLite + Anthropic SDK is most of it).

Open a PR against `main` once tests pass.

---

## License

MIT. See [`LICENSE`](LICENSE) if present.

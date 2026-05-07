# Architecture

SecDigest is two FastAPI applications sharing a single SQLite database. The
two apps run as separate uvicorn instances (own ports, own middleware, own
templates) but share `db.py`, `mailer.py`, and the rest of the
business-logic layer.

```
                 ┌────────────────────┐  ┌────────────────────┐
   admin user →  │  Admin app (8080)  │  │ Public app (8000)  │  ← anyone
                 │  secdigest/web/    │  │  secdigest/public/ │
                 │  password-gated    │  │  no auth           │
                 └─────────┬──────────┘  └─────────┬──────────┘
                           │                       │
                           └─────────┬─────────────┘
                                     │
                                     ▼
                       ┌─────────────────────────────┐
                       │  Shared business layer       │
                       │  • db.py (single sqlite3)   │
                       │  • mailer.py                │
                       │  • fetcher.py / rss.py      │
                       │  • summarizer.py            │
                       │  • scheduler.py (cron)      │
                       └─────────────┬───────────────┘
                                     ▼
                            ┌─────────────────┐
                            │  data/          │
                            │   secdigest.db  │  ← SQLite, WAL mode
                            └─────────────────┘
```

## The two apps

### Admin app — `secdigest/web/`

- **Port 8080** by default
- Password-gated, session cookies, CSRF on every state-changing route
- Owns: curator, email builder, archive, prompts, feeds, settings,
  templates, subscribers, digest curator
- Lives at `secdigest/web/app.py:36` — `app = FastAPI(...)`
- Lifespan (`secdigest/web/app.py:21-33`) refuses to start if
  `SECRET_KEY=="dev-secret-change-me"`. Calls `db.init_db()`,
  `ensure_default_password()`, `sched.start_scheduler()`.

### Public app — `secdigest/public/`

- **Port 8000** by default; only runs when `PUBLIC_SITE_ENABLED=1`
- No auth — surface is `GET /`, `POST /subscribe`, `GET /confirm/{token}`,
  `GET /unsubscribe/{token}`
- Per-IP rate limiting on `/subscribe` (5/hr) and `/unsubscribe` (10/hr)
- Cyber-noir templates at `secdigest/public/templates/`, editable CSS at
  `secdigest/public/static/style.css`
- Lifespan only calls `db.init_db()` — no auth, no scheduler

> **🔧 Why two apps and not one with two prefixes?** The blast radius is
> different. Admin can do anything, public can only enroll/unenroll. Running
> them as separate uvicorn processes means: separate auth posture, separate
> middleware stacks (the public app has *no* session middleware), separate
> log streams, and the operator can put the public app on the open internet
> while keeping the admin on a VPN or tailnet.

## How the apps share state

They share a single `sqlite3` connection at the module level (`db._conn`).
Both apps `import secdigest.db`, both call `db.init_db()` on startup. SQLite
is in WAL mode (`secdigest/db.py:11`) so cross-process readers + writers
don't block each other much.

> **⚠️ Gotcha** — SQLite WAL mode tolerates concurrent readers and a single
> writer. Both apps run in the same Python process by default (the public app
> runs in a thread spawned by `run.py`), so you have one writer in practice.
> If you ever split the apps into separate processes (e.g. two systemd
> units), keep WAL mode on; otherwise serialize writes.

## Lifecycle of a daily article

This is the hot path — knowing it makes everything else clearer.

```
  ┌─────────────────────┐
  │ APScheduler tick    │  scheduler.py — cron at FETCH_TIME
  │ (or admin "Fetch HN")│
  └──────────┬──────────┘
             │
             ▼
  ┌──────────────────────┐  fetcher.run_fetch(date)
  │ HN top 200 + new 100 │     • httpx.AsyncClient
  │ + every active RSS   │     • rss.fetch_all_rss
  └──────────┬───────────┘
             │
             ▼
  ┌──────────────────────┐  fetcher.run_fetch
  │ Dedup by URL against │     • article_all_urls() set
  │ all historical       │     • blank-URL editorial notes always allowed
  └──────────┬───────────┘
             │
             ▼
  ┌──────────────────────┐  fetcher.score_articles
  │ Claude Haiku scores  │     • per-article messages.create()
  │ each 0-10 + reason   │     • cache_control on system prompt
  └──────────┬───────────┘     • keyword fallback if Claude errors
             │
             ▼
  ┌──────────────────────┐  fetcher.run_fetch (lines ~221-247)
  │ Pool selection:      │     1. Reserve top hn_pool_min HN slots
  │  reserve N HN, then  │     2. Fill remainder by relevance, RSS+leftover HN
  │  fill by relevance   │     3. Mark top max_curator articles included=1
  └──────────┬───────────┘
             │
             ▼
  ┌──────────────────────┐  summarizer.summarize_article (per article)
  │ Per-article: fetch   │     • httpx.Client → article HTML → strip tags
  │ body + Claude summ.  │     • Claude with cache_control system prompt
  └──────────┬───────────┘     • Stored in articles.summary column
             │
             ▼
  ┌──────────────────────┐  /day/{date} curator + builder
  │ Admin curates:       │     • toggle include/exclude
  │  edit, reorder, pin  │     • drag-to-reorder via SortableJS
  │  to W/M digests      │     • pin chips set pin_weekly/pin_monthly
  └──────────┬───────────┘
             │
             ▼
  ┌──────────────────────┐  mailer.send_newsletter(date, kind='daily')
  │ Send to subscribers  │     • db.subscriber_active(cadence='daily')
  │  with cadence=daily  │     • per-recipient unsubscribe token in URL
  └──────────────────────┘
```

## Lifecycle of a digest

A weekly digest is a `newsletters` row with `kind='weekly'`. It does not
own articles directly — it references daily articles through a join table:

```
   articles (per-day owners)         digest_articles (join)
   ┌────────────────────┐            ┌──────────────────────┐
   │ id  newsletter_id  │ ◄────────  │ digest_id article_id │
   │ pin_weekly = 0|1   │            │ position included    │
   │ pin_monthly = 0|1  │            └──────────┬───────────┘
   └────────────────────┘                       │
                                                ▼
                                      newsletters (digest row)
                                      ┌──────────────────────┐
                                      │ kind='weekly'        │
                                      │ period_start = Mon   │
                                      │ period_end   = Sun   │
                                      └──────────────────────┘
```

Auto-seed (`db.digest_seed`):

1. Pull every article from daily newsletters whose date falls in the period
   (`articles_in_period`).
2. Take everything `pin_weekly=1` (or `pin_monthly=1`) — those bypass any
   curator gate.
3. Top-up to `max_curator_articles` with the highest-relevance remaining
   articles that the daily curator already kept (`included=1`).

Editing a daily article (e.g. fixing a summary) propagates automatically —
the digest renders from the same article rows via the join.

> **🔧 Why a join table instead of duplicating rows?** Because the same
> article ought to look identical in the daily, weekly, and monthly issues.
> If you fix a typo in the summary on Tuesday's curator page, that fix
> shows up in the weekly digest sent Monday and the monthly sent on the 1st
> — for free.

## Concurrency model

- **Single Python process** by default. Admin runs in the main thread; the
  public app runs in a daemon thread spawned from `run.py:43`.
- **`db._lock`** (a `threading.Lock`) guards every write path through
  `db.py`. Reads are lock-free.
- **APScheduler** runs in its own thread inside the admin process. The
  daily job calls into `fetcher.run_fetch` which is async; APScheduler
  handles the loop integration.
- **For multi-worker uvicorn** (e.g. `--workers 4`) the in-memory
  rate-limiter dicts and the threading lock won't be coherent across
  workers. Prefer `--workers 1` for SecDigest unless you wire up a Redis
  backend for limiters.

## Where each part is documented

- DB-side details (every table, every migration, every helper) →
  [data-model.md](data-model.md)
- Admin route catalogue → [admin-app.md](admin-app.md)
- Public flow + customisation → [public-site.md](public-site.md)
- Fetcher / RSS / scoring / summarizer → [content-pipeline.md](content-pipeline.md)
- Email rendering and sending → [email.md](email.md)
- Every env var → [configuration.md](configuration.md)

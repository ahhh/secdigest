# Logging

SecDigest doesn't use Python's `logging` module. Instead it leans on three
deliberately simple surfaces, each with a different lifetime and audience:

1. **`print()` to stdout** — captured by uvicorn → systemd journal. Ephemeral,
   read in real time, never queryable later.
2. **DB tables** — `llm_audit_log` for every Claude call, `voice_audio.error`
   per newsletter for the ElevenLabs/S3 pipeline. Persistent, queryable.
3. **Sticky `config_kv` keys** — most recent error of a given kind
   (`last_curation_error`), wiped by hand or by the next success. Surfaced
   in the admin UI.

> **🔧 Why no `logging`?** SecDigest is a single-host app. Two uvicorn
> processes, one SQLite file. There's no log aggregator, no levels we want
> to filter at runtime, and no library code that needs to compose with a
> caller's handlers. `print` + the DB cover everything we'd otherwise pay a
> handler/formatter setup tax for. If that ever stops being true, this is
> the doc to revise.

## The three surfaces

### 1. stdout (the `print` channel)

Every module that wants to leave a trace uses `print(f"[modname] ...")` with
a bracketed prefix matching the module name. Find them all with:

```bash
grep -rn "print(\[" secdigest/
```

Where they live:

| Module | What it prints |
|---|---|
| `secdigest/scheduler.py:16,20,29,31,35,57` | Daily-job lifecycle: start, fetch error, summarized count, summarize error, auto-send result, scheduled time at boot |
| `secdigest/fetcher.py:171,194,197,212,218,263` | Curation errors, "already has articles, skipping", candidate counts, scoring progress, stored-count summary |
| `secdigest/summarizer.py:102` | Per-article summarize failure (article id + exception) |
| `secdigest/rss.py:50,71` | Per-feed fetch error and per-feed article count |
| `secdigest/public/routes.py:97` | Confirmation email send failure (subscribe flow) |
| `secdigest/web/auth.py:29-32` | One-time "DEFAULT PASSWORD: secdigest" warning when `ensure_default_password()` seeds the hash |
| `run.py:64` | Public site bind line at startup |

In dev: `python run.py` puts everything on your terminal.

In prod (systemd):

```bash
# Both apps, follow live
journalctl -u secdigest -u secdigest-public -f

# Last 200 lines of the admin app
journalctl -u secdigest -n 200 --no-pager

# Filter to a specific module's prefix
journalctl -u secdigest -f | grep '\[fetcher\]'

# Since boot, scheduler-related only
journalctl -u secdigest -b | grep '\[scheduler\]'

# Since a specific date/time
journalctl -u secdigest --since '2026-05-07 00:00' --until '2026-05-07 06:00'
```

> **⚠️ Gotcha:** uvicorn's own access log lives on the same stream — every
> request shows up as `INFO: 127.0.0.1:... "GET /day/..."`. Filter with
> `grep -v 'INFO:'` if you only want the application traces.

### 2. `llm_audit_log` table (every Claude call)

Schema: `secdigest/db.py:85`. One row written per Claude API call by:

- `secdigest/fetcher.py:147` — `operation='curation'`, one row per article
  scored (so a daily fetch with 30 candidates writes 30 rows).
- `secdigest/summarizer.py:92` — `operation='summary'`, one row per
  article summarized; `article_id` is set so you can join back.

Columns: `timestamp`, `operation`, `model`, `input_tokens`,
`output_tokens`, `cached_tokens`, `article_id`, `result_snippet` (truncated
to 500 chars by `audit_log()` itself, defensive against long Claude
responses).

Read it via the admin UI (Settings page → "LLM Audit Log (last 20)" table,
rendered from `db.audit_recent(20)` in `secdigest/web/routes/settings.py:51`)
or directly:

```bash
sqlite3 data/secdigest.db "
  SELECT timestamp, operation, input_tokens, output_tokens, result_snippet
  FROM llm_audit_log ORDER BY id DESC LIMIT 20;
"

# Per-day token cost for the last week
sqlite3 data/secdigest.db "
  SELECT date(timestamp), operation,
         SUM(input_tokens) AS in_t,
         SUM(cached_tokens) AS cached_t,
         SUM(output_tokens) AS out_t
  FROM llm_audit_log
  WHERE timestamp >= datetime('now', '-7 days')
  GROUP BY date(timestamp), operation
  ORDER BY date(timestamp) DESC;
"
```

This table is append-only by convention. Nothing prunes it — for a busy
deploy, run a periodic `DELETE FROM llm_audit_log WHERE timestamp <
datetime('now', '-90 days')`.

### 3. Sticky error keys

Some errors are worth surfacing in the UI long after they happen. Those
land in `config_kv` (or a per-row `error` column) and are cleared by the
next success or by an explicit "Dismiss" button.

- **`config_kv.last_curation_error`** — written in
  `secdigest/fetcher.py:174`, cleared on the next clean run at
  `:177`. The Settings page reads it and humanises common patterns
  (missing key, 429, quota, network) via
  `_humanize_errors()` at `secdigest/web/routes/settings.py:16-39`.
  Dismiss button posts to `/settings/clear-curation-error`
  (`settings.py:169-174`).

- **`voice_audio.error`** — per-newsletter ElevenLabs/S3 failures,
  written by `voice._generate_pipeline()` at
  `secdigest/voice.py:316-321`. Always passed through `_redact()`
  (`voice.py:58-73`) so credentials never reach the UI even if the API
  burped them back in an error body. Surfaced live by the voice panel
  poller (`secdigest/web/templates/_voice_panel.html`).

## What is NOT a log

A few tables look log-shaped but aren't:

- **`feedback`** — subscriber signal/noise votes. Data, not diagnostics.
- **`audit_event`-style auth events** — we don't have one. There's no
  per-login or per-action audit trail. If you need that, see "Adding a
  new logging surface" below.
- **uvicorn access log** — uvicorn's default formatter, not ours. Lives
  on stdout next to the `print` lines. We don't shape or persist it.

## How to read for specific symptoms

The cookbook is in [debugging.md](debugging.md), but in summary:

| Symptom | First place to look |
|---|---|
| Articles aren't curating | `last_curation_error` in `config_kv`, then `[fetcher]` lines in journalctl |
| Summaries missing | `[summarizer]` lines, then `llm_audit_log WHERE operation='summary'` |
| Voice generation stuck | `voice_audio` row for that newsletter (`status`, `error`) |
| Daily cron didn't fire | `[scheduler]` lines around `fetch_time` |
| Public confirmation email never arrived | `[public]` lines in `secdigest-public` unit |
| 500 in the admin | journalctl tracebacks (FastAPI dumps the exception there) |

## Adding a new logging surface

Pick the surface that matches the lifetime you want.

### Adding a `print` line

No setup. Match the existing pattern so `grep` keeps working:

```python
print(f"[mymodule] {what_happened}: {detail}")
```

Use one stable prefix per module (the bracketed string is the bit
operators grep on in journalctl). Don't put secrets in the message —
stdout goes to the journal, which may be readable by other operators.

### Adding a row to `llm_audit_log`

`db.audit_log()` is currently Claude-specific (the `model` column wants a
model name). If you're calling Claude, use it:

```python
from secdigest import db
db.audit_log(
    operation="my_new_op",
    model=MODEL,
    input_tokens=usage.input_tokens,
    output_tokens=usage.output_tokens,
    cached_tokens=getattr(usage, "cache_read_input_tokens", 0),
    article_id=None,
    result_snippet=summary[:300],
)
```

The helper truncates `result_snippet` to 500 chars itself
(`db.py:1358`); keep the input small anyway so the table stays cheap to
scan.

### Adding a sticky last-error key

Two pieces — write on failure, surface in the UI:

```python
# 1. In the failing code path:
db.cfg_set("last_X_error", str(e))
# ...and on success:
db.cfg_set("last_X_error", "")

# 2. In the relevant routes file, read it:
cfg = db.cfg_all()
errors = cfg.get("last_X_error", "")
```

For credential-sensitive operations, redact before persisting:

```python
from secdigest.voice import _redact
db.cfg_set("last_X_error", _redact(str(e)))
```

`_redact()` strips anything matching `(api_key|access_key|secret|password|token)\s*[=:]\s*\S+`
and caps at 500 chars. The matchers are loose on purpose — false
positives just produce `<redacted>`, false negatives leak a secret.

### Adding a brand-new audit table

If you want, say, a per-login audit trail, add a table next to
`llm_audit_log` in the schema block at `secdigest/db.py:85`, write a
`X_log()` helper next to `audit_log()` (`db.py:1350`), and call it from
the route handler. Mirror the pattern: `INSERT` under the write lock,
`commit()`, and a `X_recent(limit)` reader for the UI.

## Wiring up Python `logging` (if you ever need it)

You don't today, but if a future feature needs leveled logging or a
structured handler, the right hook point is the admin app's lifespan in
`secdigest/web/app.py`:

```python
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
```

Put it before `init_db()` in the lifespan. The existing `print()` calls
keep working — they don't go through the `logging` module, so they won't
double-emit.

For the public app, mirror it in `secdigest/public/app.py`. Both apps
share the same stdout/journal stream in prod, so the format string ends
up unified there.

## Privacy: what we never log

- Subscriber email addresses don't go to stdout. They appear in
  `mailer` error returns (e.g. `f"{sub['email']}: invalid email"`) which
  bubble up as a flash message in the admin UI; they don't get printed
  to the journal.
- SMTP password, ElevenLabs API key, AWS secret access key — all
  encrypted at rest (`secdigest/crypto.py`) and never logged.
- ElevenLabs/S3 error bodies are passed through `_redact()` before
  reaching the journal, the `voice_audio.error` column, or the UI
  poller.
- Article body text is not logged. The summarizer audit row carries the
  first 300 chars of the *summary* (Claude's output), not the input.
- Session cookies / CSRF tokens — never logged anywhere on purpose.

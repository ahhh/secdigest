# Configuration

SecDigest has two configuration layers:

1. **Process-level** — env vars read once at import time in
   `secdigest/config.py`. Auto-loaded from `.env` via `python-dotenv`.
2. **DB-level** — runtime-editable values in the `config_kv` table,
   exposed via the admin `/settings` page.

Most operator-facing knobs live in *both* — the env var seeds the
DB-level value on first run via `DB_CONFIG_DEFAULTS`. After first run,
the DB value wins (and `.env` changes for those keys are ignored).

> **🔧 Why two layers?** Env vars are good for secrets and one-time
> deploy decisions (`SECRET_KEY`, `DB_PATH`). Runtime DB config is good
> for things you want to tweak without restarting (fetch time, article
> count, SMTP creds).

## .env auto-loading

`config.py:9-13` loads `.env` from the project root via `python-dotenv`:

```python
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass
```

`override=False` means real env vars (set via systemd `EnvironmentFile`,
Docker `--env`, shell exports) **win over** `.env` values. Production
deploys can ship a `.env` for defaults and override per-environment.

## The full env var catalogue

### Required for production

| Var                    | Default               | Notes |
|------------------------|-----------------------|-------|
| `SECRET_KEY`           | `dev-secret-change-me` | The admin app refuses to start if this is the default. Used to sign session cookies. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `ANTHROPIC_API_KEY`    | `""`                  | Required for Claude scoring + summarisation. Without it, fetcher falls back to keyword scoring; summarizer fails. |

### TLS

See [tls.md](tls.md) for the full story.

| Var             | Default | Notes |
|-----------------|---------|-------|
| `TLS_ENABLED`   | `1`     | Master switch. `0` for dev or nginx-fronted prod. |
| `TLS_DOMAIN`    | `""`    | e.g. `secdigest.example.com` — auto-resolves to `/etc/letsencrypt/live/<domain>/{fullchain,privkey}.pem`. |
| `TLS_CERTFILE`  | `""`    | Explicit override. |
| `TLS_KEYFILE`   | `""`    | Explicit override. |

### Public site

| Var                      | Default              | Notes |
|--------------------------|----------------------|-------|
| `PUBLIC_SITE_ENABLED`    | `0`                  | Set to `1` to spawn the public app alongside admin. |
| `PUBLIC_HOST`            | `0.0.0.0`            | Public uvicorn bind address. |
| `PUBLIC_PORT`            | `8000`               | Public uvicorn port. |
| `PUBLIC_BASE_URL`        | `""`                 | Public-facing URL inserted into confirm + unsub links. **Set this to the URL your subscribers can actually reach** (e.g. `https://secdigest.example.com`). |

### Pipeline tuning (DB-seeded — change in `/settings` after first run)

| Env var                  | DB key                   | Default | Notes |
|--------------------------|--------------------------|---------|-------|
| `FETCH_TIME`             | `fetch_time`             | `00:00` | 24h local time for the daily auto-fetch (APScheduler). |
| `HN_MIN_SCORE`           | `hn_min_score`           | `50`    | Skip top-stories with fewer points. |
| `HN_POOL_MIN`            | `hn_pool_min`            | `10`    | Reserved HN slots per day (see [content-pipeline.md](content-pipeline.md#orchestrate--run_fetchdate)). |
| `MAX_ARTICLES`           | `max_articles`           | `15`    | Daily article pool size. |
| `MAX_CURATOR_ARTICLES`   | `max_curator_articles`   | `10`    | Top N auto-included in the daily; rest live in the pool. |
| `BASE_URL`               | `base_url`               | `http://localhost:8000` | Used by `mailer.send_newsletter` to build per-recipient unsubscribe URLs (when `PUBLIC_BASE_URL` is unset). |

### SMTP (DB-seeded)

| Env var      | DB key       | Notes |
|--------------|--------------|-------|
| `SMTP_HOST`  | `smtp_host`  | e.g. `smtp.gmail.com`. Required for sending. |
| `SMTP_PORT`  | `smtp_port`  | `587` (STARTTLS) or `465` (SSL). |
| `SMTP_USER`  | `smtp_user`  | Login username. |
| `SMTP_PASS`  | `smtp_pass`  | Encrypted at rest via `crypto.encrypt`. Gmail App Passwords work either with or without the spaces — preserve as-is. |
| `SMTP_FROM`  | `smtp_from`  | e.g. `SecDigest <you@example.com>`. Refused if it contains `example.com`. |

### Auth

| Env var          | DB key         | Notes |
|------------------|----------------|-------|
| `PASSWORD_HASH`  | `password_hash` | bcrypt hash of the admin password. Auto-generated from `secdigest` on first run if unset. |

> **⚠️ Gotcha** — If you put `PASSWORD_HASH=<your hash>` in `.env`, that
> seeds the test DB during `pytest` runs unless you override it. The
> `tmp_db` fixture in `tests/conftest.py` defangs this — see
> [testing.md](testing.md).

## DB_CONFIG_DEFAULTS — the seed dict

`config.py:36-50`:

```python
DB_CONFIG_DEFAULTS = {
    "smtp_host":     os.environ.get("SMTP_HOST", ""),
    "smtp_port":     os.environ.get("SMTP_PORT", "587"),
    "smtp_user":     os.environ.get("SMTP_USER", ""),
    "smtp_pass":     os.environ.get("SMTP_PASS", ""),
    "smtp_from":     os.environ.get("SMTP_FROM", "SecDigest <noreply@example.com>"),
    "fetch_time":    os.environ.get("FETCH_TIME", "00:00"),
    "hn_min_score":  os.environ.get("HN_MIN_SCORE", "50"),
    "hn_pool_min":   os.environ.get("HN_POOL_MIN", "10"),
    "max_articles":  os.environ.get("MAX_ARTICLES", "15"),
    "max_curator_articles": os.environ.get("MAX_CURATOR_ARTICLES", "10"),
    "base_url":      os.environ.get("BASE_URL", "http://localhost:8000"),
    "auto_send":     "0",
    "password_hash": DEFAULT_PASSWORD_HASH,
}
```

`_seed_config()` writes these on first run via `INSERT OR IGNORE` — so
existing values aren't overwritten. **This is why changing
`MAX_ARTICLES` in `.env` doesn't take effect on a DB that's already been
initialised.** Either:

- Edit the value in `/settings` instead, or
- Run `sqlite3 data/secdigest.db "UPDATE config_kv SET value='25' WHERE key='max_articles'"` and restart

## Other config_kv keys (not in DB_CONFIG_DEFAULTS)

These are written by application code, not seeded:

| Key                         | Set by                                     | Notes |
|-----------------------------|---------------------------------------------|-------|
| `tmpl_<newsletter_id>`      | `newsletter_set_template_id`               | Per-newsletter template choice |
| `subject_<newsletter_id>`   | `newsletter_set_subject`                   | Per-newsletter subject override |
| `toc_<newsletter_id>`       | `newsletter_set_toc`                       | "1" or "0" |
| `last_curation_error`       | `fetcher.score_articles` on Claude failure | One-line error; surfaces in the day curator banner; cleared by the dismiss button |

## auto_send

`auto_send=1` makes the daily APScheduler job send the newsletter
automatically after fetching + summarising. Default `0`.

> **⚠️ Gotcha** — If you flip `auto_send=1` and have SMTP misconfigured,
> the scheduler emits a print log and moves on (doesn't crash). Tail
> uvicorn's stdout to spot SMTP failures: `[scheduler] auto-send: SMTP
> error: ...`.

## Inspecting current values

```bash
# All seeded values (with seed defaults applied)
sqlite3 data/secdigest.db "SELECT key, value FROM config_kv ORDER BY key;"

# Just the seedable ones
sqlite3 data/secdigest.db "
  SELECT key, value FROM config_kv
  WHERE key IN ('smtp_host','smtp_port','smtp_user','smtp_from',
                'fetch_time','hn_min_score','hn_pool_min',
                'max_articles','max_curator_articles','base_url',
                'auto_send')
  ORDER BY key;"
```

## Resetting a value

```bash
sqlite3 data/secdigest.db "UPDATE config_kv SET value='8' WHERE key='hn_pool_min';"
```

Most knobs take effect on the next request (no restart needed). Exceptions:

- `fetch_time` — APScheduler reads it at scheduler start; restart admin
  app to apply (or call `scheduler.reschedule(new_time)` from the
  Settings page Save button — `/settings` already does this).
- `password_hash` — takes effect immediately (next login attempt reads
  it).
- `secret_key` — process-level, not in `config_kv`, requires restart.

## .env.example

Lives at the repo root. Treat it as the documented superset of every env
var the project understands. Copy it to `.env`, fill in your values,
done.

```bash
cp .env.example .env
$EDITOR .env
python run.py
```

## Where each setting actually lives

Quick decoder ring for "I changed X but it didn't apply":

| You changed                             | Source of truth at runtime  | How to apply |
|-----------------------------------------|------------------------------|--------------|
| `SECRET_KEY` in .env                    | env var                      | restart admin |
| `MAX_ARTICLES` in .env                  | `config_kv` (seeded once)    | edit in /settings or UPDATE config_kv |
| Anything in /settings                   | `config_kv`                  | next request |
| `SMTP_PASS` in .env                     | `config_kv`, encrypted       | re-save in /settings (it re-encrypts) |
| `TLS_DOMAIN` in .env                    | env var                      | restart `run.py` |
| `PUBLIC_BASE_URL` in .env               | env var                      | restart public app |
| `auto_send` in .env                     | `config_kv` (seeded once)    | toggle in /settings |

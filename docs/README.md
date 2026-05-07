# SecDigest Docs

These docs explain how SecDigest works inside, with a slant toward debugging
and day-to-day development. Treat them as a tour: start with [architecture]
to get the shape of the system, then dive into the part you're touching.

## Tour

1. **[architecture.md](architecture.md)** — Two-app overview (admin on 8080,
   public on 8000), what lives where, how data flows from "HN article" to
   "subscriber inbox."
2. **[data-model.md](data-model.md)** — Every table, every column, why the
   weird ones exist (digest_articles, kind/period_start, source_name), the
   migration story.
3. **[admin-app.md](admin-app.md)** — All admin routes catalogued. Auth, CSRF,
   the day curator vs the digest curator, the email builder iframe.
4. **[public-site.md](public-site.md)** — Landing page, double-opt-in
   subscribe flow, unsubscribe by UUID, honeypot + rate limiting,
   customising the cyber-noir theme.
5. **[content-pipeline.md](content-pipeline.md)** — Fetcher (HN top + new),
   RSS aggregation, dedup, Claude scoring, HN slot reservation, summary
   generation. The full daily cron.
6. **[email.md](email.md)** — Built-in templates, the 1-column vs 2-column
   render path, kind-aware send (daily/weekly/monthly + cadence filter),
   header sanitisation.
7. **[configuration.md](configuration.md)** — Every env var,
   `DB_CONFIG_DEFAULTS` vs `config_kv`, what's runtime-editable in
   `/settings`.
8. **[tls.md](tls.md)** — `TLS_ENABLED` + `TLS_DOMAIN`, certbot recipes,
   permissions, renewal hooks.
9. **[deployment.md](deployment.md)** — Two systemd units, nginx with split
   admin/public hostnames, port allocation.
10. **[testing.md](testing.md)** — Fixture catalogue, the egress-blocking
    stub pattern, how to add a test, running locally vs CI.
11. **[debugging.md](debugging.md)** — "Emails aren't sending," "fetch
    returns nothing," "subscribers aren't getting the daily" — symptoms
    paired with sqlite-inspection commands.
12. **[logging.md](logging.md)** — The three logging surfaces (stdout,
    `llm_audit_log`, sticky last-error keys), where each module logs,
    how to read in dev vs systemd, and how to hook in new traces.

## Conventions used in these docs

- **`file:line`** references are clickable in most editors. They point at the
  code that implements whatever's being described.
- **Code blocks marked `sqlite>`** are ad-hoc queries you can paste into
  `sqlite3 data/secdigest.db` for spelunking.
- **🔧 Why** boxes call out non-obvious design choices — they're load-bearing
  context for anyone refactoring later.
- **⚠️ Gotcha** boxes flag specific footguns that have bitten us. Don't strip
  these out without thinking hard.

## Top-level repo layout

```
secdigest/
├── run.py                       # Dev entry point. Starts admin (+ public if PUBLIC_SITE_ENABLED=1).
├── pytest.ini                   # asyncio_mode=auto, pythonpath=.
├── requirements.txt
├── .env.example                 # Documented superset of env vars.
├── secdigest/                   # Main Python package.
│   ├── config.py                # Env loading, TLS resolution, DB_CONFIG_DEFAULTS.
│   ├── db.py                    # All SQLite. Schema, migrations, every helper.
│   ├── crypto.py                # HMAC stream cipher for SMTP password at rest.
│   ├── periods.py               # ISO-week + month bounds for digest grouping.
│   ├── fetcher.py               # HN top+new pull, dedup, Claude scoring.
│   ├── rss.py                   # RSS/Atom parser + per-feed pull.
│   ├── summarizer.py            # Per-article body fetch + Claude summary.
│   ├── mailer.py                # Email render + send (kind-aware).
│   ├── scheduler.py             # APScheduler daily job.
│   ├── web/                     # ADMIN APP (port 8080)
│   │   ├── app.py
│   │   ├── auth.py              # bcrypt + session helpers
│   │   ├── csrf.py              # token gen + verify_csrf dep
│   │   ├── security.py          # SSRF guards + per-IP rate limiters
│   │   ├── routes/              # one file per feature area
│   │   ├── templates/           # Jinja2
│   │   └── static/              # CSS, JS, favicon
│   └── public/                  # PUBLIC SITE (port 8000)
│       ├── app.py
│       ├── routes.py
│       ├── templates/           # cyber-noir landing
│       └── static/              # editable CSS
├── tests/                       # See testing.md
└── docs/                        # You are here.
```

# Deployment

Production deploy story. Three patterns, in order of preference:

1. **Two systemd units + nginx in front** — recommended.
2. **One systemd unit running `run.py`** — works but couples admin and
   public.
3. **Direct uvicorn-with-TLS, no proxy** — simplest config; loses the
   ability to put admin and public on different hostnames cleanly.

This doc covers (1) end-to-end, then notes how (2) and (3) differ.

## Pattern 1: two systemd units + nginx

```
   internet ─→ nginx (TLS) ─┬─→ 127.0.0.1:8080  → secdigest-admin.service
                            └─→ 127.0.0.1:8000  → secdigest-public.service
```

Both uvicorn instances bind to localhost, plain HTTP. nginx terminates
TLS using letsencrypt certs and proxies to the right backend based on
hostname.

### Filesystem layout

```
/opt/secdigest/                       # repo checkout
├── .env                              # production env vars
├── .venv/                            # the project venv
└── secdigest/, run.py, etc.
```

The service user is whatever you prefer — `www-data` is conventional but
doesn't have to be.

### .env for production

```bash
# Identity
SECRET_KEY=<32-byte random>
ANTHROPIC_API_KEY=sk-ant-...

# SMTP
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@your-domain.com
SMTP_PASS=<16-char Gmail App Password>
SMTP_FROM="SecDigest <you@your-domain.com>"

# Public site
PUBLIC_SITE_ENABLED=1
PUBLIC_HOST=127.0.0.1                 # nginx talks back over loopback
PUBLIC_PORT=8000
PUBLIC_BASE_URL=https://secdigest.example.com

# Admin URL — used by the mailer when PUBLIC_BASE_URL is unset
BASE_URL=https://secdigest.example.com

# TLS — disabled here because nginx handles it
TLS_ENABLED=0
```

### Admin systemd unit

```ini
# /etc/systemd/system/secdigest.service
[Unit]
Description=SecDigest admin
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/secdigest
EnvironmentFile=/opt/secdigest/.env
ExecStart=/opt/secdigest/.venv/bin/uvicorn secdigest.web.app:app \
    --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Public systemd unit

```ini
# /etc/systemd/system/secdigest-public.service
[Unit]
Description=SecDigest public site
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/secdigest
EnvironmentFile=/opt/secdigest/.env
ExecStart=/opt/secdigest/.venv/bin/uvicorn secdigest.public.app:app \
    --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> **🔧 Why two units instead of `python run.py`?** Independent restarts
> (a deploy of admin code shouldn't drop the public site), independent
> log streams (`journalctl -u secdigest-public -f` is cleaner than
> grepping a combined log), and one process per worker matches the
> threading model (the in-memory rate-limit dicts and the `db._lock`
> are coherent within a single process).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now secdigest secdigest-public
sudo systemctl status secdigest secdigest-public
```

### nginx — split admin and public on different hostnames

Recommended pattern: public site on the bare domain, admin on a
subdomain.

```nginx
# /etc/nginx/sites-available/secdigest
upstream secdigest_admin  { server 127.0.0.1:8080; }
upstream secdigest_public { server 127.0.0.1:8000; }

# Public site — what subscribers see
server {
    listen 443 ssl http2;
    server_name secdigest.example.com;

    ssl_certificate     /etc/letsencrypt/live/secdigest.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/secdigest.example.com/privkey.pem;

    location / {
        proxy_pass http://secdigest_public;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Admin — back-office, separate hostname
server {
    listen 443 ssl http2;
    server_name admin.secdigest.example.com;

    ssl_certificate     /etc/letsencrypt/live/admin.secdigest.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/admin.secdigest.example.com/privkey.pem;

    # Bonus hardening: lock admin to your office IP / VPN
    # allow 198.51.100.0/24;
    # deny all;

    location / {
        proxy_pass http://secdigest_admin;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

# Force HTTPS for both
server {
    listen 80;
    server_name secdigest.example.com admin.secdigest.example.com;
    return 301 https://$host$request_uri;
}
```

Bring it up:

```bash
sudo ln -s /etc/nginx/sites-available/secdigest /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

> **⚠️ Gotcha** — `X-Forwarded-Proto $scheme` is important. Without it,
> the admin app's `is_authed` checks may misjudge the protocol when
> generating cookies (Starlette's `https_only` cookie attribute consults
> the scope's scheme). On a TLS-fronted setup, you also want to enable
> `--proxy-headers` on uvicorn so it trusts the forwarded scheme — add
> `--forwarded-allow-ips 127.0.0.1` to the systemd `ExecStart`.

### Hardening the admin

Behind nginx, you can:

- IP-allowlist the admin server block (see commented `allow`/`deny`
  above)
- Add HTTP basic auth as a second factor in front of the SecDigest login
- Put the admin behind a VPN entirely (different listen address, only
  accessible from inside)
- Run admin on a **separate host** from public, and skip the nginx admin
  server block on the public-facing instance

The DB has to be reachable from both, but SQLite + remote = pain. If
you go split-host, switch to Postgres first.

### certbot for both hostnames

```bash
sudo certbot --nginx -d secdigest.example.com -d admin.secdigest.example.com
```

The deploy hook for renewal:

```bash
# /etc/letsencrypt/renewal-hooks/deploy/nginx-reload.sh
#!/bin/sh
systemctl reload nginx
```

(No need to restart SecDigest — nginx loads the certs.)

## Pattern 2: single systemd unit running run.py

If you want it simpler:

```ini
[Service]
ExecStart=/opt/secdigest/.venv/bin/python /opt/secdigest/run.py
EnvironmentFile=/opt/secdigest/.env
```

`run.py` reads `PUBLIC_SITE_ENABLED` and spawns the public app in a
thread. Trade-offs:

- ➕ One process, one log stream
- ➕ No separate systemd unit to manage
- ➖ Restart of admin code drops the public site too
- ➖ Reload mode (uvicorn `reload=True`) is incompatible with threading,
  so dev hot-reload only works with `PUBLIC_SITE_ENABLED=0`

For dev: pattern 2 with `PUBLIC_SITE_ENABLED=0` is what `run.py` is
optimised for. For production, pattern 1.

## Pattern 3: direct uvicorn-with-TLS

Skip nginx entirely. uvicorn serves HTTPS itself via the `TLS_*` env
vars (see [tls.md](tls.md)).

```bash
# .env
TLS_ENABLED=1
TLS_DOMAIN=secdigest.example.com
PUBLIC_SITE_ENABLED=1
PUBLIC_PORT=443
```

systemd unit changes:

```ini
ExecStart=/opt/secdigest/.venv/bin/uvicorn secdigest.public.app:app \
    --host 0.0.0.0 --port 443 \
    --ssl-certfile /etc/letsencrypt/live/secdigest.example.com/fullchain.pem \
    --ssl-keyfile  /etc/letsencrypt/live/secdigest.example.com/privkey.pem
```

Plus a separate unit for admin on `:8443` or whatever.

When this is fine:

- Single hostname (no admin/public split)
- You don't need HTTP redirects, custom headers, or rate-limit at the
  proxy layer
- One operator, no team

When it's painful:

- Cert renewal requires uvicorn restarts, not just nginx reload
- Binding to 443 needs `setcap` or root
- Admin and public share the listening port (different paths instead of
  different hostnames is awkward)

Most production deploys want pattern 1.

## Logs

```bash
# Real-time follow
journalctl -u secdigest -f
journalctl -u secdigest-public -f

# Last hour, both units
journalctl -u secdigest -u secdigest-public --since '1 hour ago'

# Errors only
journalctl -u secdigest -u secdigest-public -p err
```

The application writes to stdout/stderr; systemd captures it. Notable
log markers:

- `[scheduler] daily fetch at HH:MM` — APScheduler started
- `[scheduler] daily job for YYYY-MM-DD` — daily cron firing
- `[fetcher] stored N articles ...` — fetch completed
- `[fetcher] curation error: ...` — Claude failed; fallback ran
- `[public] confirmation email failed for ...` — DOI email send error
- `[scheduler] auto-send: ...` — only if `auto_send=1`

## Database backups

```bash
# Crash-safe SQLite backup
sqlite3 /opt/secdigest/data/secdigest.db ".backup '/var/backups/secdigest-$(date +%F).db'"
```

cron it nightly. The DB stays under a few MB even after years of usage —
articles + summaries dominate, and Claude summaries are 200-400 chars
each.

## Database migrations on deploy

`init_db()` runs every time the app starts. It's idempotent —
re-running on an already-migrated DB is a no-op. So:

```bash
git pull
sudo systemctl restart secdigest secdigest-public
```

…is a complete deploy. No manual migration step.

> **⚠️ Gotcha** — `_migrate_newsletters_kind` is the one migration that
> rebuilds a table (CREATE NEW + INSERT SELECT + DROP OLD + RENAME). It
> fires only when the `kind` column is missing. After the first deploy
> on a legacy DB, it never runs again. Take a backup before that
> particular upgrade. After that, deploys are uneventful.

## Updating Python dependencies

```bash
cd /opt/secdigest
git pull
sudo -u www-data ./.venv/bin/pip install -r requirements.txt
sudo systemctl restart secdigest secdigest-public
```

If you need a Python version bump, rebuild the venv from scratch:

```bash
sudo systemctl stop secdigest secdigest-public
sudo rm -rf /opt/secdigest/.venv
sudo -u www-data python3.13 -m venv /opt/secdigest/.venv
sudo -u www-data /opt/secdigest/.venv/bin/pip install -r /opt/secdigest/requirements.txt
sudo systemctl start secdigest secdigest-public
```

## Health checking

There's no `/health` endpoint. Check liveness with:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/login
# 200 if admin is alive

curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
# 200 if public is alive
```

For monitoring, hit those URLs from your uptime checker. If you want a
proper health endpoint, add a route at `/health` to the admin's
`web/app.py` returning `{"ok": True}` — five lines of code.

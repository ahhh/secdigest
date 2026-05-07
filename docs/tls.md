# TLS

SecDigest can serve HTTPS directly from uvicorn (terminate TLS in the
Python process) **or** sit behind nginx that terminates TLS in front.
Both are supported. This doc covers the direct-uvicorn path; nginx is
in [deployment.md](deployment.md).

## The four env vars

| Var             | Default | Purpose |
|-----------------|---------|---------|
| `TLS_ENABLED`   | `1`     | Master switch. Set `0` for dev or nginx-fronted prod. |
| `TLS_DOMAIN`    | `""`    | Domain name → auto-resolves to the Let's Encrypt layout. |
| `TLS_CERTFILE`  | `""`    | Explicit override. Takes priority over `TLS_DOMAIN`. |
| `TLS_KEYFILE`   | `""`    | Explicit override. |

## Path resolution

`config.resolve_tls_paths()` returns the `(certfile, keyfile)` pair
uvicorn will use:

1. If `TLS_CERTFILE` AND `TLS_KEYFILE` are both set → use them as-is.
2. Else if `TLS_DOMAIN` is set → derive the standard Let's Encrypt
   layout:

   ```
   /etc/letsencrypt/live/<TLS_DOMAIN>/fullchain.pem
   /etc/letsencrypt/live/<TLS_DOMAIN>/privkey.pem
   ```

3. Else → return `("", "")` and let `validate_tls_config` raise.

## Validation

`config.validate_tls_config()` runs at the `run.py` boundary. It either:

- returns `None` when `TLS_ENABLED=0` (skip TLS)
- returns `(cert, key)` when TLS is enabled and the files exist
- raises `RuntimeError` with an actionable message when TLS is enabled
  but misconfigured

The error messages list the three options: set `TLS_DOMAIN`, set the
explicit `TLS_CERTFILE`+`TLS_KEYFILE`, or set `TLS_ENABLED=0`.

> **🔧 Why fail loud instead of falling back to HTTP?** Silent fallback
> means an operator who *thinks* they're running HTTPS could ship plain
> HTTP for weeks without noticing — session cookies in cleartext, no
> certificate warning, nothing. Fail-loud forces the operator to make
> a deliberate choice.

## Setting it up with certbot

Standard recipe for a public-facing deploy:

```bash
# 1. Install certbot
sudo apt install certbot                     # Debian/Ubuntu
# or: sudo dnf install certbot                # Fedora/RHEL

# 2. Get a cert (port 80 must be free for the HTTP-01 challenge)
sudo certbot certonly --standalone -d secdigest.example.com

# 3. Verify the cert pair landed in the standard location
sudo ls /etc/letsencrypt/live/secdigest.example.com/
# fullchain.pem  privkey.pem  cert.pem  chain.pem  README

# 4. Point SecDigest at the domain
echo "TLS_DOMAIN=secdigest.example.com" >> /opt/secdigest/.env

# 5. Restart and verify HTTPS is up
sudo systemctl restart secdigest          # if using systemd
# or just re-run:  python run.py
```

## Permissions on the private key

Let's Encrypt's default permissions:

```
drwx------ root root /etc/letsencrypt/live
drwxr-xr-x root root /etc/letsencrypt/live/<domain>
-rw-r----- root root /etc/letsencrypt/live/<domain>/privkey.pem  (mode 0640, root:root)
```

The Python process needs **read** access to `privkey.pem`. Three options:

### Option A — run as root

Simplest. Drop privileges after binding to ports 443/80 if you want
defence in depth.

### Option B — service user with group access

```bash
# Add a group that can read certs
sudo groupadd ssl-cert
sudo usermod -aG ssl-cert www-data        # or whatever user runs SecDigest

# Make the live/ dir traversable for that group
sudo chgrp -R ssl-cert /etc/letsencrypt/live /etc/letsencrypt/archive
sudo chmod -R g+rX /etc/letsencrypt/live /etc/letsencrypt/archive

# Make sure the private key is group-readable
sudo chmod 0640 /etc/letsencrypt/archive/<domain>/privkey*.pem
```

> **⚠️ Gotcha** — `live/` is a directory of symlinks pointing into
> `archive/`. If you only `chmod` `live/`, the symlinks still resolve to
> root-only files in `archive/` and `privkey.pem` reads fail. Walk both.

### Option C — copy the cert into a SecDigest-readable location

```bash
sudo install -m 0644 /etc/letsencrypt/live/<domain>/fullchain.pem  /opt/secdigest/cert.pem
sudo install -m 0640 -o www-data /etc/letsencrypt/live/<domain>/privkey.pem /opt/secdigest/key.pem
```

Then point `TLS_CERTFILE`/`TLS_KEYFILE` at the copies. You'll need to
re-copy on every renewal (certbot's deploy hook makes this scriptable).

## Renewal

Certbot installs a systemd timer that renews automatically every 60 days.
The cert paths under `/etc/letsencrypt/live/<domain>/` are symlinks that
point at the latest version, so SecDigest doesn't see different paths
across renewals.

But uvicorn loads the cert into memory at startup — **renewals don't
take effect until uvicorn restarts**. Add a deploy hook that bounces
the SecDigest service:

```bash
# /etc/letsencrypt/renewal-hooks/deploy/secdigest-restart.sh
#!/bin/sh
systemctl restart secdigest secdigest-public
```

```bash
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/secdigest-restart.sh
```

Test it:

```bash
sudo certbot renew --dry-run
```

You'll see "Hook executed" lines for the deploy hook.

## Ports

By default, SecDigest runs admin on **8080** and public on **8000**.
Both are unprivileged ports. With `TLS_ENABLED=1` and no other changes,
you'd be running:

- `https://your.host:8080/`  ← admin
- `https://your.host:8000/`  ← public landing

Most subscribers expect `https://your.host/` (port 443). Three ways to
get there:

### Option 1 — Bind directly to 443 (needs CAP_NET_BIND_SERVICE)

```bash
echo "PUBLIC_PORT=443" >> .env
sudo setcap 'cap_net_bind_service=+ep' /opt/secdigest/.venv/bin/python3.13
```

The `setcap` lets the venv's Python bind to <1024 without root. Note
that capabilities are reset on Python upgrades — re-apply after every
upgrade.

### Option 2 — iptables redirect

Keep `PUBLIC_PORT=8000`, redirect 443 → 8000 with iptables:

```bash
sudo iptables -t nat -A PREROUTING -p tcp --dport 443 -j REDIRECT --to-port 8000
```

### Option 3 — nginx in front (recommended for production)

Set `TLS_ENABLED=0` in SecDigest, terminate TLS in nginx instead, and
proxy back to plain HTTP localhost:8000 / 8080. See
[deployment.md](deployment.md) for the full nginx config.

## Common debugging recipes

**`run.py` exits with `RuntimeError: TLS_ENABLED=1 but no certificate paths configured`.**

You set `TLS_ENABLED=1` (or accepted the default) but didn't point at
certs. Pick one:

```bash
echo "TLS_DOMAIN=secdigest.example.com" >> .env  # if using letsencrypt
# OR
echo "TLS_CERTFILE=/path/to/cert.pem" >> .env
echo "TLS_KEYFILE=/path/to/key.pem" >> .env
# OR (for dev / nginx-fronted)
echo "TLS_ENABLED=0" >> .env
```

**`RuntimeError: TLS_CERTFILE not readable at /etc/letsencrypt/...`**

The path is right but the file isn't readable by the running process.
Check permissions (see "Permissions on the private key" above).

```bash
sudo -u www-data ls -l /etc/letsencrypt/live/<domain>/fullchain.pem
# If "Permission denied" or no such file, fix the perms or run as root.
```

**Browser shows `ERR_SSL_PROTOCOL_ERROR` or refuses connection.**

Check uvicorn started with TLS:

```bash
journalctl -u secdigest -n 50
# Look for: "Uvicorn running on https://..."
# If it says "http://", TLS_ENABLED=0 and you're not actually serving HTTPS.
```

**Cert expired but auto-renewal isn't running.**

```bash
sudo systemctl list-timers | grep certbot
# Should show certbot.timer; if missing, install certbot.timer

sudo certbot renew --dry-run
# Should succeed. If it fails, the actual renewal will too.

# Check renewal logs
sudo journalctl -u certbot.service -n 100
```

**TLS works for browsers but not for curl/scripts.**

Almost always the chain. uvicorn needs `fullchain.pem` (cert + chain),
not `cert.pem` (just the leaf). The auto-resolution from `TLS_DOMAIN`
gets this right; explicit overrides are easy to get wrong.

```bash
# Verify what you've configured
sqlite3 data/secdigest.db "SELECT key, value FROM config_kv WHERE key LIKE 'tls%';"
# (No rows — TLS config is env-only, not config_kv)

# So check the env that run.py actually saw
python -c "from secdigest import config; print(config.resolve_tls_paths())"
```

## TLS in tests

`tmp_db` fixture doesn't touch TLS — TLS validation only runs in
`run.py`'s `_ssl_kwargs()`, which tests don't call by default. The
`tests/test_tls_config.py` file directly invokes `validate_tls_config()`
with monkeypatched constants — no real cert files needed (tests create
empty placeholder files via `tmp_path` to satisfy the existence check).

If a future feature ever runs TLS validation at app import time (don't
do this — defer it to actual server start), the conftest will need to
defang `TLS_ENABLED` for tests.

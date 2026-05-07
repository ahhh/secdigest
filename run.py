#!/usr/bin/env python3
"""Development entry point.

Always starts the admin app on port 8080. If PUBLIC_SITE_ENABLED=1 in the
environment (or your .env), also starts the public landing/subscribe site on
PUBLIC_PORT (default 8000) in a background thread.

If TLS_ENABLED=1 (default), both apps serve HTTPS using the configured cert
pair (Let's Encrypt at /etc/letsencrypt/live/<TLS_DOMAIN>/ unless overridden).
TLS termination at nginx is also fine — set TLS_ENABLED=0 in that case.

For production, prefer running each app under its own uvicorn / systemd unit:
    uvicorn secdigest.web.app:app    --host 127.0.0.1 --port 8080 \\
        --ssl-certfile <cert> --ssl-keyfile <key>
    uvicorn secdigest.public.app:app --host 127.0.0.1 --port 8000 \\
        --ssl-certfile <cert> --ssl-keyfile <key>
"""
import threading

import uvicorn

from secdigest import config


def _ssl_kwargs() -> dict:
    """Return the ssl_certfile/ssl_keyfile kwargs uvicorn expects, or an empty
    dict when TLS is disabled. validate_tls_config() raises with an actionable
    message if TLS is enabled but the cert pair is missing or unreadable."""
    paths = config.validate_tls_config()
    if not paths:
        return {}
    cert, key = paths
    return {"ssl_certfile": cert, "ssl_keyfile": key}


def _run_admin(reload: bool, ssl: dict):
    uvicorn.run(
        "secdigest.web.app:app",
        host="0.0.0.0",
        port=8080,
        reload=reload,
        reload_dirs=["secdigest"] if reload else None,
        **ssl,
    )


def _run_public(ssl: dict):
    uvicorn.run(
        "secdigest.public.app:app",
        host=config.PUBLIC_HOST,
        port=config.PUBLIC_PORT,
        # Reload doesn't compose with threading — keep the public app static in dev.
        # Edit + restart `python run.py` to pick up template/CSS changes.
        reload=False,
        **ssl,
    )


if __name__ == "__main__":
    ssl = _ssl_kwargs()
    scheme = "https" if ssl else "http"

    if config.PUBLIC_SITE_ENABLED:
        print(f"[run] public site on {scheme}://{config.PUBLIC_HOST}:{config.PUBLIC_PORT}")
        threading.Thread(target=_run_public, args=(ssl,), daemon=True).start()
        # Disable admin reload too — uvicorn's reload spawns a child process which
        # would orphan the public-site thread on every code change.
        _run_admin(reload=False, ssl=ssl)
    else:
        # Reload is safe in single-app mode and useful for dev. Note that running
        # uvicorn with reload=True AND ssl=... works fine; reload re-execs the
        # whole process, certs reload with it.
        _run_admin(reload=True, ssl=ssl)

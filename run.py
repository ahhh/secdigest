#!/usr/bin/env python3
"""Development entry point.

Always starts the admin app on port 8080. If PUBLIC_SITE_ENABLED=1 in the
environment (or your .env), also starts the public landing/subscribe site on
PUBLIC_PORT (default 8000) in a background thread.

For production, prefer running each app under its own uvicorn / systemd unit:
    uvicorn secdigest.web.app:app    --host 127.0.0.1 --port 8080
    uvicorn secdigest.public.app:app --host 127.0.0.1 --port 8000
"""
import os
import threading

import uvicorn

from secdigest import config


def _run_admin(reload: bool):
    uvicorn.run(
        "secdigest.web.app:app",
        host="0.0.0.0",
        port=8080,
        reload=reload,
        reload_dirs=["secdigest"] if reload else None,
    )


def _run_public():
    uvicorn.run(
        "secdigest.public.app:app",
        host=config.PUBLIC_HOST,
        port=config.PUBLIC_PORT,
        # Reload doesn't compose with threading — keep the public app static in dev.
        # Edit + restart `python run.py` to pick up template/CSS changes.
        reload=False,
    )


if __name__ == "__main__":
    if config.PUBLIC_SITE_ENABLED:
        print(f"[run] public site on http://{config.PUBLIC_HOST}:{config.PUBLIC_PORT}")
        threading.Thread(target=_run_public, daemon=True).start()
        # Disable admin reload too — uvicorn's reload spawns a child process which
        # would orphan the public-site thread on every code change.
        _run_admin(reload=False)
    else:
        _run_admin(reload=True)

"""Auth helpers shared across route modules.

The admin app uses single-password session auth — there's only one
operator. The hash is stored in ``config_kv`` (encrypted at rest by the
crypto layer when written via the settings page) and the session bit is
just ``request.session["authenticated"] = True`` after a successful login.

We use bcrypt rather than a more modern KDF because it's a known quantity
in pure-Python and one operator means we'll never be password-cracking
at scale. ``bcrypt.gensalt()`` defaults to 12 rounds, which is fine here.
"""
from fastapi import Request
from fastapi.responses import RedirectResponse
import bcrypt

from secdigest import db


def hash_password(password: str) -> str:
    """One-way hash for storage. Salt is generated per-call and embedded
    in the output, so two calls with the same password yield different
    hashes (and the same password still verifies against either)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time compare under the hood — bcrypt.checkpw doesn't
    short-circuit on the first mismatched byte the way ``==`` would."""
    return bcrypt.checkpw(password.encode(), hashed.encode())


def is_authed(request: Request) -> bool:
    """Truthy if the session cookie is signed and carries our auth flag.
    Session signing is set up by ``SessionMiddleware`` in ``app.py``."""
    return bool(request.session.get("authenticated"))


def redirect_login() -> RedirectResponse:
    """Used by route handlers that need auth. ``302`` (Found) is the
    conventional "do this GET instead" — preserves browser history
    semantics where ``303`` (See Other) would force a method change."""
    return RedirectResponse("/login", status_code=302)


def ensure_default_password():
    """Write the default password hash on first run if none is configured."""
    # Only seed when nothing's set — otherwise we'd reset the operator's
    # password on every startup, which would be a fun day at the office.
    if not db.cfg_get("password_hash"):
        db.cfg_set("password_hash", hash_password("secdigest"))
        # Loud banner in the startup logs so the admin notices and changes
        # it before exposing the app. ``is_default_password()`` below
        # powers a similar in-UI banner.
        print("\n" + "!" * 60)
        print("  DEFAULT PASSWORD: secdigest")
        print("  Change it immediately at /settings")
        print("!" * 60 + "\n")


def is_default_password() -> bool:
    """True if the stored password hash matches the default 'secdigest' password."""
    # Re-import locally to avoid the import-order pitfall of grabbing
    # ``db`` at module-load time before ``init_db()`` has run.
    from secdigest import db
    ph = db.cfg_get("password_hash")
    if not ph:
        return False
    return verify_password("secdigest", ph)

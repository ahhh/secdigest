"""Auth helpers shared across route modules."""
from fastapi import Request
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext

from secdigest import db

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def is_authed(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=302)


def ensure_default_password():
    """Write the default password hash on first run if none is configured."""
    if not db.cfg_get("password_hash"):
        db.cfg_set("password_hash", pwd_ctx.hash("secdigest"))
        print("\n" + "!" * 60)
        print("  DEFAULT PASSWORD: secdigest")
        print("  Change it immediately at /settings")
        print("!" * 60 + "\n")

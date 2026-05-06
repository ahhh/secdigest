"""CSRF token generation and validation."""
import secrets
from fastapi import HTTPException, Request
from markupsafe import Markup


CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"


def get_or_create_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def csrf_input(request: Request) -> Markup:
    token = get_or_create_token(request)
    return Markup(f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{token}">')


def csrf_token_value(request: Request) -> str:
    return get_or_create_token(request)


async def verify_csrf(request: Request) -> None:
    """Dependency: raises HTTPException(403) if the request lacks a valid CSRF token.
    Skips GET/HEAD/OPTIONS."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf", "")
    if not expected:
        raise HTTPException(status_code=403, detail="CSRF token not initialized")

    token = request.headers.get(CSRF_HEADER, "")
    if not token:
        try:
            form = await request.form()
            token = form.get(CSRF_FORM_FIELD, "") or ""
        except Exception:
            token = ""

    if not secrets.compare_digest(expected, str(token)):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

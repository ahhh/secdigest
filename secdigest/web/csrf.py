"""CSRF token generation and validation.

Pattern: the "synchronizer token" / "double-submit" hybrid. We store one
random token per session in ``request.session["csrf"]`` and require every
state-changing request to echo it back via either a hidden form field
(POST forms) or an ``X-CSRF-Token`` header (fetch/AJAX). A real attacker
can't read the session cookie cross-origin, so they can't fish out the
token to mirror — and without a matching token, the request is rejected.

Why not the cookie-only "double-submit" variant? We already have signed
sessions via SessionMiddleware, so storing the token in the session is
strictly stronger and avoids subdomain-based cookie tossing attacks.
"""
import secrets
from fastapi import HTTPException, Request
from markupsafe import Markup


# Header name for the AJAX path. Any HTTP-non-safelisted header forces a
# CORS preflight on cross-origin requests, which is itself a CSRF guard.
CSRF_HEADER = "X-CSRF-Token"
# Hidden field name for HTML forms.
CSRF_FORM_FIELD = "csrf_token"


def get_or_create_token(request: Request) -> str:
    """Mint a new token on first use, then keep returning the same value
    for the lifetime of the session. Tokens are 32 random bytes encoded
    as urlsafe base64 — long enough to be unguessable in practice."""
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


def csrf_input(request: Request) -> Markup:
    """Jinja helper: drop a hidden ``<input>`` into a form. Wrapped in
    ``Markup`` so Jinja's autoescape leaves the HTML alone."""
    token = get_or_create_token(request)
    return Markup(f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{token}">')


def csrf_token_value(request: Request) -> str:
    """Bare-string helper for templates that build their own form/AJAX
    machinery (e.g., a fetch() call needs the value as a header)."""
    return get_or_create_token(request)


async def verify_csrf(request: Request) -> None:
    """Dependency: raises HTTPException(403) if the request lacks a valid CSRF token.
    Skips GET/HEAD/OPTIONS."""
    # Safe methods are exempt — they shouldn't have side effects, so a
    # CSRF'd GET can't mutate state.
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf", "")
    # No session token = no protection possible. Refuse rather than letting
    # the request through "because there's nothing to compare against".
    if not expected:
        raise HTTPException(status_code=403, detail="CSRF token not initialized")

    # Header takes precedence (AJAX path), then fall back to form body.
    # The body-parse is wrapped in try/except because non-form bodies
    # (e.g., JSON) raise on .form() and we don't want a request format
    # mismatch to crash the dependency.
    token = request.headers.get(CSRF_HEADER, "")
    if not token:
        try:
            form = await request.form()
            token = form.get(CSRF_FORM_FIELD, "") or ""
        except Exception:
            token = ""

    # Constant-time compare to avoid timing side-channels — even with a
    # 32-byte random token, this is the right habit to keep.
    if not secrets.compare_digest(expected, str(token)):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

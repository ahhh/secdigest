"""Public unsubscribe route — no auth required.

The canonical home for this is the public app (secdigest/public/), but the admin app
keeps a copy so unsubscribe links from emails sent before the public site existed
still work. Both share the rate limiter from secdigest.web.security."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from secdigest import db
from secdigest.web.security import unsubscribe_allowed, unsubscribe_record

router = APIRouter()

_PAGE = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>SecDigest — Unsubscribe</title>
<style>
  body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}}
  .box{{text-align:center;max-width:420px;padding:0 20px;}}
  .brand{{color:#39ff14;font-family:monospace;font-size:1.6em;font-weight:700;margin-bottom:20px;}}
  p{{color:#8b949e;line-height:1.6;}}
</style></head>
<body><div class="box">
<div class="brand">SecDigest</div>
<p>{message}</p>
</div></body></html>"""


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(request: Request, token: str):
    if not unsubscribe_allowed(request):
        return HTMLResponse(
            _PAGE.format(message="Too many attempts. Try again later."),
            status_code=429,
        )
    unsubscribe_record(request)
    sub = db.subscriber_get_by_token(token)
    if sub and sub.get("active"):
        db.subscriber_unsubscribe_by_token(token)
        message = "You've been unsubscribed from SecDigest and will no longer receive emails."
    elif sub:
        message = "You're already unsubscribed from SecDigest."
    else:
        message = "This unsubscribe link is invalid or has already been used."
    return HTMLResponse(_PAGE.format(message=message))

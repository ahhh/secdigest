# Public Site

The public site — `secdigest/public/` — runs on its own port (default
8000) and serves the marketing landing page, the subscribe flow, and the
unsubscribe links emailed to recipients. It has **no auth** and shares
the SQLite DB with the admin app.

> **🔧 Why separate from the admin?** Different blast radius. Admin can
> do anything; public can only enroll/unenroll. Two apps means two log
> streams, two middleware stacks (the public app has no
> SessionMiddleware), and the operator can put the public app on the open
> internet while keeping admin behind a VPN/tailnet. See
> [architecture.md](architecture.md) for the wider picture.

## Files

```
secdigest/public/
├── __init__.py
├── app.py                       # FastAPI app definition
├── routes.py                    # / , /subscribe, /confirm, /unsubscribe
├── templates/
│   ├── landing.html             # the form
│   ├── thanks.html              # "check your inbox"
│   ├── confirmed.html           # "you're in" / "link expired"
│   └── unsubscribed.html        # "off the wire"
└── static/
    ├── style.css                # cyber-noir theme — edit freely
    └── favicon.svg
```

## Running it

Set in `.env`:

```bash
PUBLIC_SITE_ENABLED=1
PUBLIC_HOST=0.0.0.0
PUBLIC_PORT=8000
PUBLIC_BASE_URL=https://secdigest.example.com
```

`python run.py` then spawns the public uvicorn in a background thread
alongside the admin. For production, prefer two systemd units — see
[deployment.md](deployment.md).

`PUBLIC_BASE_URL` is the URL inserted into confirm + unsubscribe links in
emails. **Set it correctly or every confirmation link is broken.** The
public site itself doesn't read it back; it's purely for outbound URL
construction.

## The four routes

### `GET /` — landing page

Renders `landing.html`. Optional `?msg=...&status=...` query string is
displayed as a flash message at the top of the form (used for redirected
errors).

### `POST /subscribe`

```python
# secdigest/public/routes.py
@router.post("/subscribe", response_class=HTMLResponse)
async def subscribe(request: Request,
                    email: str = Form(...),
                    cadence: str = Form("daily"),
                    website: str = Form("")):
```

Flow:

1. **Honeypot** — `website` field is a hidden text input. Real browsers
   leave it empty; bots fill anything labelled "Website". A non-empty
   value silently returns a thanks page without touching the DB.
2. **Rate limit** — `subscribe_allowed(request)` checks per-IP usage; 5
   per hour. 429 on overflow.
3. **Validation** — email format via regex; cadence clamped to one of
   `daily | weekly | monthly` (anything else falls back to `daily`).
4. **Branch on subscriber state**:
   - `confirmed=1, active=1` → render `thanks.html` with no DB change
     (security: identical response so an attacker can't enumerate)
   - existing pending → rotate `confirm_token`, send a fresh confirm email
   - new email → `subscriber_create_pending(email, cadence, token)` then
     send the confirm email
5. **Send confirmation email** via `mailer.send_confirmation_email`. On
   SMTP failure, log the error to stdout and surface a generic "couldn't
   send" page (don't echo SMTP error text — info leak).

> **🔧 Why double-opt-in?** Anyone can post to /subscribe with any email.
> Without DOI, an attacker enrolls `victim@example.com` and SecDigest
> spams them. DOI puts the proof-of-control burden on the recipient: only
> someone with access to the inbox can click the confirm link.

### `GET /confirm/{token}`

```python
sub = db.subscriber_confirm(token)
```

`subscriber_confirm` (in `db.py`) atomically sets
`confirmed=1, active=1, confirm_token=NULL` for the row matching the
token. Returns the row on success, `None` if the token's already used
(or never existed). The template branches on the boolean.

> **⚠️ Gotcha** — The token is single-use. If a subscriber clicks the
> link twice (e.g. browser back button), the second click sees the
> "expired" page even though the first activated them. They're still
> subscribed — the page is misleading. Acceptable trade-off for not
> needing to handle replays explicitly.

### `GET /unsubscribe/{token}`

Per-IP rate limited (10/hr). Looks up the `unsubscribe_token` (which is
**not** the same as `confirm_token` — it never rotates). Sets
`active=0`. Always renders `unsubscribed.html` with one of three
messages:

- "You've been unsubscribed" — first time
- "You're already unsubscribed" — token valid, row already inactive
- "Invalid or expired link" — token not found

Returns 200 in every case (no 404 for missing tokens) — exposing
existence/non-existence would leak whether the token is valid for some
attacker probing the endpoint.

## The cyber-noir theme

`static/style.css` is editable as plain CSS. The top of the file is a
`:root` block of CSS variables — change those and the whole theme shifts:

```css
:root {
  --bg-0:     #04060a;          /* background */
  --bg-1:     #0a0f1a;
  --fg:       #c5d6e0;
  --muted:    #5a6b7a;
  --cyan:     #00e5ff;          /* primary accent */
  --magenta:  #ff2e7a;          /* secondary accent (sparingly) */
  --green:    #5fffa6;          /* success */
  --error:    #ff6b6b;
}
```

Atmospherics — these are deliberately subtle so they read as ambience
rather than parody:

- **Scanlines** (`.scanlines`) — fixed `position` overlay with a
  `repeating-linear-gradient` at ~0.008 opacity. Bump to `0.015-0.02` if
  you want more visible CRT vibes.
- **Grid** (`.grid-bg`) — fixed background with a 48px cyan grid + a
  radial vignette toward the corners.
- **Glitch** — only on the landing `<h1>`. Two `::before/::after` pseudo
  elements at 0.28 opacity, ±1px translate, `mix-blend-mode: screen` for
  a hairline chromatic-fringe shimmer. `prefers-reduced-motion` removes
  it entirely.

> **⚠️ Gotcha** — Don't push `.glitch::before/::after` opacity above ~0.5
> or text becomes hard to read. Keep `±2px` as the upper translate bound
> for the same reason.

The "terminal panel" (`.terminal` + `.terminal-bar`) wraps the
subscribe form. The fake titlebar dots are decorative.

## Customising the templates

The user picked **option (b)** for theme customisation: edit the HTML and
CSS files directly, no admin UI knobs.

```
secdigest/public/templates/landing.html       — hero copy, cadence radios
secdigest/public/templates/thanks.html        — post-submit page
secdigest/public/templates/confirmed.html     — post-confirm page
secdigest/public/templates/unsubscribed.html  — post-unsubscribe page
secdigest/public/static/style.css             — all visual styling
```

Every template extends nothing — they're standalone HTML, easier to read
than a base + extends layout. They share styles via the single CSS file.

The lead copy in `landing.html`:

```html
<p class="lead">
  we pull security stories from the corners of the internet,
  score them for what actually matters, and ship the good stuff to your inbox.
  no listicles. no thinkpieces. just the wire.
</p>
```

Lowercase by convention — matches the cyberpunk voice. If you change the
voice, also update the cadence radio descriptions (`<em>` blocks) and the
fineprint at the bottom of the form.

## Rate limiting

In-memory per-IP buckets, defined in `secdigest/web/security.py`:

| Route               | Window | Max | Bucket dict             |
|---------------------|--------|-----|-------------------------|
| `/subscribe`        | 1 hr   | 5   | `_SUBSCRIBE_ATTEMPTS`   |
| `/unsubscribe/...`  | 1 hr   | 10  | `_UNSUBSCRIBE_ATTEMPTS` |
| `/login` (admin)    | 15 min | 10  | `_LOGIN_ATTEMPTS`       |

Buckets evict empty keys after the window passes (memory-bounded under
normal traffic). At 10 000 unique IPs in any one bucket, a forced sweep
runs to drop stale entries — protection against IP-spray attempts.

> **⚠️ Gotcha** — IP detection (`_client_ip`) honours `X-Forwarded-For`
> unconditionally. **Behind any reverse proxy that doesn't strip
> client-supplied XFF headers, an attacker can spoof IPs and bypass the
> limit.** This is tracked as M1 in [security.md] (deferred). Until then:
> if you're behind nginx, configure it with
> `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` and don't
> expose the uvicorn port directly.

## Sharing the unsubscribe route

Both the public app and the admin app serve `/unsubscribe/{token}`.

- The public version is canonical for new emails — it points at
  `PUBLIC_BASE_URL/unsubscribe/<token>`.
- The admin version (`secdigest/web/routes/unsubscribe.py`) is kept for
  backward compatibility with old emails generated before the public
  site existed. Same DB write, same rate limiter.

If you rotate `PUBLIC_BASE_URL`, old emails still work via whichever
host still resolves to the admin app. To deprecate the admin route
entirely, remove `app.include_router(unsubscribe.router)` from
`secdigest/web/app.py:77`.

## Adding new public pages

Don't. The public surface is intentionally minimal — every route is an
attack surface and every endpoint needs rate-limiting analysis. If you
need to add functionality:

1. Can it live behind admin auth instead? Almost always yes.
2. If genuinely public, add a rate limiter for it in `web/security.py`
   following the existing `subscribe_allowed/record` pattern.
3. Update `tests/test_public_site.py` with cases that prove the limiter
   triggers and the page doesn't leak DB state.

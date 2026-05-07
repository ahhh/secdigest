# Admin App

The admin app — `secdigest/web/` — is a password-gated FastAPI application
that runs on port 8080. It owns the curator UIs, email builder, archive,
prompts, feeds, settings, and template management.

## Auth model

### Sessions

`SessionMiddleware` (Starlette) is registered with `same_site="strict"` and
`max_age=86400 * 7` (one week). The session cookie carries `authenticated`
(boolean) and `csrf` (per-session token).

```python
# secdigest/web/app.py
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    max_age=86400 * 7,
    same_site="strict",
    https_only=False,  # set to True in production behind TLS
)
```

> **⚠️ Gotcha** — `https_only=False` is the dev default. In production you
> want this `True`, but the simplest fix is to terminate TLS in front of
> uvicorn (nginx) and let the cookie be Secure on its way back to the
> browser. See [tls.md](tls.md) for the direct-uvicorn TLS path and
> [deployment.md](deployment.md) for the nginx pattern.

### Login flow

1. `GET /login` (`web/app.py:83`) — renders `login.html`. No auth needed.
2. `POST /login` (`web/app.py:88`) — checks the password against
   `db.cfg_get("password_hash")` via `bcrypt`. On success: sets
   `request.session["authenticated"] = True`, redirects to `/`.
3. The login limiter (`web/security.py`) tracks failed attempts per IP —
   10 failures within 15 minutes returns 429.

### Force-default-password middleware

`web/app.py:44` registers a middleware that redirects every authed
non-allowlisted request to `/forced-password-change` if the current
password hash matches "secdigest". The allowlist is `{"/login",
"/logout", "/forced-password-change"}` plus `/static/*` and
`/unsubscribe/*`.

> **🔧 Why** — first-run UX. The default password is `secdigest`. We don't
> want anyone running the app for two weeks before realising they never
> changed it. The middleware nags them on every page load until they fix it.

### CSRF

Every state-changing route uses `Depends(verify_csrf)`, applied at the
**router** level so it covers everything in the file:

```python
# secdigest/web/routes/newsletter.py
router = APIRouter(dependencies=[Depends(verify_csrf)])
```

Tokens come from `secdigest/web/csrf.py`:

- `get_or_create_token(request)` — lazily mints a per-session token
- `csrf_input(request)` — Jinja helper that emits `<input type="hidden"
  name="csrf_token" value="...">`
- `verify_csrf(request)` — dependency. Skips GET/HEAD/OPTIONS. For other
  methods, checks the `X-CSRF-Token` header *or* a `csrf_token` form field
  against the session token via `secrets.compare_digest`.

> **⚠️ Gotcha** — TestClient that POSTs to admin routes without a CSRF
> token gets 403. The `tests/conftest.py::get_csrf` helper extracts a
> token from any GET that renders a template; use it before any POST.

## Route catalogue

The admin app composes seven routers:

| Router file                              | Purpose                              |
|------------------------------------------|--------------------------------------|
| `routes/newsletter.py`                   | Day curator + archive + article ops  |
| `routes/digest.py`                       | Weekly + monthly curator/builder/send |
| `routes/prompts.py`                      | Prompt CRUD                           |
| `routes/subscribers.py`                  | Subscriber list + cadence            |
| `routes/feeds.py`                        | RSS feed CRUD + HN pool min          |
| `routes/email_templates_route.py`        | Template CRUD                         |
| `routes/settings.py`                     | Settings + SMTP test + audit log     |
| `routes/unsubscribe.py`                  | Public-side leftover (also on public app) |

Plus the auth-and-utility routes defined directly on `app`:

- `GET /login`, `POST /login`, `POST /logout`
- `GET /forced-password-change`, `POST /forced-password-change`

### Day curator routes

```
GET  /                                              → redirect /day/<today>
GET  /archive                                       → grouped Month → Week → Day
GET  /day/{date_str}                                → curator (default) or builder (?view=builder)
POST /day/{date_str}/fetch                          → kick off fetcher.run_fetch
POST /day/{date_str}/summarize                      → run summarizer over the day
POST /day/{date_str}/send                           → send daily to active+cadence='daily' subscribers
POST /day/{date_str}/send-test                      → single-recipient test
GET  /day/{date_str}/preview?template_id=&include_toc= → iframe HTML for the builder
POST /day/{date_str}/set-template                   → save template/subject/TOC choice
POST /day/{date_str}/auto-select                    → reset included flags from top-N relevance
GET  /day/{date_str}/pool                           → all articles for the day, sorted by score
POST /day/{date_str}/article/add                    → manual add (URL or editorial note)
POST /day/{date_str}/article/{id}/summary           → save edited summary
POST /day/{date_str}/article/{id}/regenerate        → re-run summarizer for one article
GET  /day/{date_str}/article/{id}/json              → polled by the regenerate spinner
POST /day/{date_str}/article/{id}/toggle            → flip included
POST /day/{date_str}/article/{id}/pin/{period}      → flip pin_weekly or pin_monthly
POST /day/{date_str}/reorder                        → drag-and-drop order
```

`date_str` is validated against `^\d{4}-\d{2}-\d{2}$` at the route boundary
(via `_validate_date()`) — malformed dates 404 before reaching templates.

### Digest curator routes

The digest curator at `web/routes/digest.py` is structurally a simpler
mirror of the day curator. Each route exists in `/week/{date_str}/...` and
`/month/{date_str}/...` flavours; the inner handler dispatches on `kind`:

```
GET  /{week,month}/{date_str}                       → digest curator (auto-seeds if empty)
POST /{week,month}/{date_str}/auto-select           → re-seed from pinned + top-relevance
POST /{week,month}/{date_str}/article/{id}/toggle   → flip included in digest_articles
POST /{week,month}/{date_str}/article/{id}/remove   → drop the article from this digest
POST /{week,month}/{date_str}/reorder
POST /{week,month}/{date_str}/set-template
GET  /{week,month}/{date_str}/preview
POST /{week,month}/{date_str}/send                  → cadence-filtered send
POST /{week,month}/{date_str}/send-test
```

Notable differences from the day curator:

- Articles come from `db.digest_article_list(digest_id)` which joins
  `digest_articles → articles → newsletters`, with `position` and
  `included` overlaid from the join.
- "Remove" deletes the join row (not the article). Pinning the article on
  its source day-curator page would re-add it; clicking auto-select also
  re-adds it.
- Send routes call `mailer.send_newsletter(period_start, kind=...)` —
  filters subscribers by matching cadence.

## The day curator UI

`secdigest/web/templates/day.html` is the largest template in the project.
Two view modes share the same URL:

- `/day/<date>` (default) — the **curator**: drag-to-reorder list, pin
  chips, edit/regenerate summaries, toggle inclusion.
- `/day/<date>?view=builder` — the **email builder**: template picker on
  the left, live iframe preview on the right.

### Curator pane

Each article card has:
- Relevance score (▲) with the source meta (HN points, RSS feed name, or
  "editorial note")
- ⓘ tooltip — `Source: <Hacker News | feed name | Manual> — <reason>`
  (defined in `day.html:362-376`)
- Title link
- Inline summary editor (textarea, opens via JS toggle)
- Action buttons: **Edit · Regenerate · Exclude/Include · Pin weekly · Pin monthly**

The pin buttons toggle between two visual states:

| state    | class           | text             |
|----------|-----------------|------------------|
| off      | `btn-ghost`     | `⧉ Pin weekly`   |
| on       | `btn-primary`   | `✓ Weekly`       |

Same pattern for monthly. State derives from `a.pin_weekly` /
`a.pin_monthly` columns.

> **🔧 Why a full button instead of a chip?** The original W/M chips were
> ~14×18px and didn't visibly indicate on/off state. Promoting to full
> buttons in the actions row matches the existing Exclude/Include button
> language and makes the pin state unmistakable.

### Builder pane

- **Template select** — populated from `email_template_list()`.
- **Subject + TOC** — saved to `config_kv` as `subject_<id>` and
  `toc_<id>`.
- **Send Test** — `POST /day/<date>/send-test` with a single email
  address.
- **Inline template editor** — collapsed by default. Opens to expose
  `name`, `html`, `article_html` fields; "Save" overwrites the template,
  "Save as New" creates a new one.
- **Live preview iframe** — points at `/day/<date>/preview` with a
  sandboxed `Content-Security-Policy: sandbox; default-src 'none'; ...`
  header so the rendered email can't poke at the parent page.

## Archive grouping

`/archive` (`routes/newsletter.py:31`) walks all daily newsletters, groups
them by Month → ISO Week → Day. Weekly and monthly digest rows are looked
up by their `period_start` and either rendered as a status badge ("weekly
digest · 7 sent") or a "+ build week" prompt.

The grouping is computed in Python (not SQL) — see
`routes/newsletter.py:archive` for the dict-of-dicts construction. It's
O(n) over the daily count, fine for 365 days.

## Subscribers UI

`/subscribers` (`routes/subscribers.py`):

- Add form (email + optional name) — admin-add bypasses double-opt-in by
  setting `confirmed=1`.
- Filter input — client-side wildcard matcher (`*` and `?`).
- Per-row dropdown for cadence (auto-submits on change). Pause/Resume
  flips `active`. Delete removes the row.

> **⚠️ Gotcha** — When the admin adds a subscriber, they receive emails
> immediately because `confirmed=1, active=1` from the start. No
> confirmation email is sent. If you want to confirm-via-email an
> admin-added subscriber, do it manually by writing a `confirm_token`
> and sending the link.

## Settings UI

`/settings` saves config to `config_kv`. The page wraps form fields with
explanatory subtext (see `templates/settings.html`). The two pool-size
settings are deliberately split:

- **Daily article pool size** (`max_articles`) — total articles stored
  per day after scoring.
- **Articles auto-included in the daily newsletter**
  (`max_curator_articles`) — top N from the pool that are pre-included.

`/settings/test-smtp` does a connect-only check with the current SMTP
config; surfaces the exception text on failure so you can debug auth /
TLS issues without sending mail.

## Static assets

CSS at `secdigest/web/static/style.css`. Two JS deps loaded from CDN:

- HTMX (currently unused — left in case future routes want partial
  updates)
- SortableJS — powers drag-to-reorder on the curator and digest pages

Custom JS for the builder lives inline in `day.html` and `digest.html`.

## A note on background tasks

`/day/<date>/fetch` and `/day/<date>/summarize` and `/day/<date>/article/<id>/regenerate`
all kick off `asyncio.create_task(...)` and return 302 immediately. The
admin page polls `GET /day/<date>/article/<id>/json` for live updates on
regenerate.

> **⚠️ Gotcha** — Background tasks run in the same process as the web
> server. If you `kill -9` the admin process mid-fetch, you'll see a
> half-populated newsletter. Subsequent `/day/<date>/fetch` is idempotent
> via `db.article_count()` — it skips if any articles exist for the day.
> If you want to retry a botched fetch, delete the rows manually:
>
> ```sql
> DELETE FROM articles WHERE newsletter_id = (
>   SELECT id FROM newsletters WHERE kind='daily' AND date='2026-05-04'
> );
> ```

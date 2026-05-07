# Email

Everything related to producing and sending email lives in
`secdigest/mailer.py`. Three concerns:

1. **Render** — combine a template + a list of articles into HTML + plain
   text (`render_email_html`, `_render_text`).
2. **Send** — open SMTP, push messages out (`send_newsletter`,
   `send_test_email`, `send_confirmation_email`).
3. **Sanitise** — strip CR/LF from headers (`_sanitize_header`).

## Templates

Six built-in templates seed on first run (see [data-model.md](data-model.md#email_templates)):

| Name           | Style                              | Best for           |
|----------------|------------------------------------|--------------------|
| Dark Terminal  | Black bg, green-mono SecDigest brand | Default; admin aesthetic |
| Clean Light    | White card, blue accents           | Sans-serif readers |
| Minimal        | Plain serif on white               | Newsletter purists |
| 2-Column Grid  | Dark, two cards per row            | Short summaries    |
| Mobile Dark    | Dark, single column, fluid width   | Gmail iOS / mobile |
| Mobile Light   | Light, single column, fluid width  | Gmail iOS / mobile |

Each template has two parts in the DB:

- `html` — the wrapper, with `{articles}` (or `{articles_2col}`) substituted
  in.
- `article_html` — a single `<tr>...</tr>` (or `<td>` for 2-col)
  per-article snippet.

### Wrapper placeholders

| Placeholder        | Substituted with                     |
|--------------------|--------------------------------------|
| `{articles}`       | Concatenated `<tr>` rows from `article_html` |
| `{articles_2col}`  | Concatenated `<td>` cells in `<tr>` pairs (only used when the wrapper contains `{articles_2col}` literally) |
| `{date}`           | `newsletter.date` (period_start for digests) |
| `{unsubscribe_url}` | Per-recipient URL — empty string in preview |

### Article-snippet placeholders

| Placeholder    | Source                         |
|----------------|--------------------------------|
| `{number}`     | 1-indexed position             |
| `{title}`      | `articles.title` (HTML-escaped) |
| `{url}`        | `articles.url` (rejected unless http/https) |
| `{hn_url}`     | `articles.hn_url` (rejected unless http/https) |
| `{summary}`    | `articles.summary` (HTML-escaped) |
| `{hn_score}`   | `articles.hn_score`            |
| `{hn_comments}`| `articles.hn_comments`         |

> **🔧 Why HTML-escape title/summary but not the placeholder names?**
> The placeholder substitution is straight `str.replace`. Titles and
> summaries come from external sources (HN, RSS, Claude) and may contain
> `<script>` etc. We `html.escape(value, quote=True)` before substitution
> so the rendered email can't be scripted. The placeholder names
> themselves are constants, no escaping needed.

> **⚠️ Gotcha** — URLs go through a stricter filter: if `url` doesn't
> start with `http://` or `https://`, the substitution emits an empty
> string. So `javascript:alert(1)` in an article URL becomes
> `<a href="">{title}</a>` — a dead link. This is the one case where we
> deliberately break editor-style behaviour to prevent XSS in mail clients.

### TOC (table of contents)

Optional per-newsletter setting (`config_kv` key `toc_<id>`). When
enabled, `_render_toc` prepends a list of anchor links to the article
section. Each article's first `<tr>` (or `<td>` in 2-col) gets an
`id="article-N"` attribute injected.

```html
<a href="#article-1" style="...">#1 CVE-2026-1234: heap UAF in libfoo</a>
```

> **⚠️ Gotcha** — The TOC styling is hardcoded dark (cyan link, gray
> contents header). On the **Clean Light** / **Mobile Light** templates
> it'll render as a dark bar inside the white card. Known issue, low
> priority — most operators stick to one theme family.

## Rendering — `render_email_html`

Signature:

```python
def render_email_html(newsletter: dict, articles: list[dict],
                      template_id: int | None = None,
                      unsubscribe_url: str = "",
                      include_toc: bool = False) -> str:
```

Resolution order for which template to use:

1. Explicit `template_id` arg, if passed
2. `db.newsletter_get_template_id(newsletter["id"])` — per-newsletter
   override stored in `config_kv` as `tmpl_<id>`
3. `db.email_template_default()` — first template by ID

The function builds `rows_1col` (always) and `rows_2col` (always — only
substituted if the wrapper template contains the `{articles_2col}`
literal). The 2-col logic walks pairs of articles, emitting a `<tr>` per
pair with one or two `<td>` cells.

Articles with `included=0` are skipped — they exist for the pool view
but don't appear in the email.

## Plain-text body — `_render_text`

A simple text-version generator for the multipart `text/plain` part.
Most email clients display the HTML; the plain text is for terminal
mail readers and as a spam-classifier signal (legit bulk mail has both
parts).

Format:

```
SecDigest — 2026-05-04
========================================

1. CVE-2026-1234: heap UAF in libfoo
   https://example.com/cve-1234
   Use-after-free in libfoo allows RCE; patched in 2.3.1.

2. ...
```

## Sending — `send_newsletter`

```python
def send_newsletter(date_str: str, kind: str = "daily") -> tuple[bool, str]:
```

`kind` argument selects the data path:

- `kind='daily'` → articles via `db.article_list(newsletter_id)`
- `kind in ('weekly','monthly')` → articles via
  `db.digest_article_list(newsletter_id)` which joins through
  `digest_articles`

Subscriber filter:

```python
subscribers = db.subscriber_active(cadence=kind)
```

So a `kind='weekly'` send only reaches `subscribers WHERE active=1 AND
cadence='weekly'`. The set is disjoint across cadences.

Subject resolution:

1. `db.newsletter_get_subject(id)` — admin-edited override (config_kv
   key `subject_<id>`)
2. `template["subject"]` — the template's default subject string
3. `_default_subject_for(kind)` — fallback constants:
   - `'SecDigest — {date}'` for daily
   - `'SecDigest Weekly — {date}'` for weekly
   - `'SecDigest Monthly — {date}'` for monthly

After resolution, `{date}` is replaced with `date_str`, then the result
is passed through `_sanitize_header`.

### Per-recipient unsubscribe URL

The send loop iterates subscribers and rebuilds `unsubscribe_url` per
recipient using their `unsubscribe_token`. So each delivered email has a
unique URL — clicking it unsubscribes only that one address, not anyone
else's.

```python
for sub in subscribers:
    unsub_url = f"{base_url}/unsubscribe/{sub['unsubscribe_token']}"
    html_body = render_email_html(newsletter, articles,
                                   unsubscribe_url=unsub_url, ...)
    ...
    server.send_message(msg)
```

> **🔧 Why per-recipient render?** `render_email_html` only differs by
> the `{unsubscribe_url}` substitution. Re-rendering ~5KB of HTML per
> recipient is cheap. The alternative — string-replace
> `{unsubscribe_url}` after rendering once — would mean adding a token
> to every URL placeholder in the wrapper, which is more error-prone.

## Sending — `send_test_email`

Same render path, but:

- Single hard-coded recipient (the value passed in)
- Unsubscribe URL is `<base_url>/unsubscribe/test-preview` — a sentinel
  token that won't match any DB row, so clicking it shows the "invalid
  link" page (correctly — there's no row to deactivate)

The output is byte-identical to a production send with respect to template,
subject, TOC setting, and HTML rendering. Only the recipient and the
unsubscribe link differ.

## Sending — `send_confirmation_email`

Used by the public site's DOI flow. Different from the bulk path:

- Goes through `_smtp_send()` (the helper) instead of opening SMTP
  directly. One connection per call.
- Subject and body are hardcoded — no template resolution.
- Returns `(ok, msg)` — caller logs `msg` on failure but never echoes
  it to the user (info leak).

```python
def send_confirmation_email(to_email: str, confirm_url: str) -> tuple[bool, str]:
    # html_body, text_body built inline with the confirm_url substituted
    return _smtp_send(to_email, "Confirm your SecDigest subscription",
                      html_body, text_body)
```

## Header sanitisation

```python
def _sanitize_header(value: str) -> str:
    """Strip CR/LF from any string about to land in an SMTP header.
    SMTP headers are CRLF-terminated; smuggling \r\n into Subject or From
    lets an attacker inject arbitrary headers (BCC, Reply-To, etc.)."""
    if value is None:
        return ""
    return str(value).replace("\r", "").replace("\n", "")
```

Applied to:

- Recipient addresses (subscriber email + test recipient)
- Subject (after `{date}` substitution)
- From address (from `cfg.smtp_from`)

Centralising the strip in one helper is the M4 fix from the security
review — the previous scattered `.replace("\r", "").replace("\n", "")`
calls were correct but fragile (a future edit could re-introduce CRLF
after the strip).

## SMTP configuration

All in `config_kv`, editable in `/settings`:

| Key         | Example                            | Notes |
|-------------|------------------------------------|-------|
| `smtp_host` | `smtp.gmail.com`                   | Required |
| `smtp_port` | `587` (STARTTLS) or `465` (SSL)    | |
| `smtp_user` | `you@gmail.com`                    | |
| `smtp_pass` | encrypted via `crypto.encrypt()`   | At-rest encryption |
| `smtp_from` | `SecDigest <you@gmail.com>`        | Refused if contains `example.com` |

The password is encrypted at rest using a custom HMAC-SHA256 stream
cipher (`secdigest/crypto.py`). It's not Fernet-grade — the cipher is
designed to make the SQLite file less interesting to a casual viewer,
not to defeat someone with the host. The encryption key is derived from
`SECRET_KEY`; rotate that and existing encrypted blobs become unreadable.

### Gmail caveats

Gmail rejects normal account passwords. You **must** use an App Password
(16 chars, generated at <https://myaccount.google.com/apppasswords>).
Spaces in App Passwords are intentional — Gmail accepts them either with
or without. We preserve them as-is on save.

## Connection lifecycle

`send_newsletter` opens **one** SMTP connection and reuses it for every
recipient via `server.send_message(msg)`. Errors on individual recipients
are caught per-iteration and accumulated in an `errors` list — one bad
email address doesn't abort the rest of the send.

```python
with _server as server:
    server.ehlo()
    if port != 465: server.starttls(context=tls_context)
    server.ehlo()
    if smtp_user: server.login(smtp_user, crypto.decrypt(smtp_pass))
    for sub in subscribers:
        try:
            server.send_message(msg)
            sent += 1
        except Exception as e:
            errors.append(f"{sub['email']}: {e}")
```

The return message is `"Sent to N subscribers"` plus an
`. Errors: ...` suffix if any individual sends failed.

## Testing

Three layers of mock — see [testing.md](testing.md):

- `stub_smtp` fixture replaces `mailer._smtp_send` AND
  `smtplib.SMTP/SMTP_SSL`. Tests that send mail get a list of captured
  messages instead of real SMTP traffic.
- `tests/test_mailer_smoke.py` covers rendering escapes, kind-aware send
  routing, cadence filtering, header sanitisation.
- `tests/test_full_pipeline.py` covers the end-to-end "fetch → curate →
  send" chain with everything mocked.

## Common debugging recipes

**Send returns "No active <kind> subscribers".**

The cadence filter is doing its job. Check:

```sql
sqlite> SELECT email, active, cadence, confirmed FROM subscribers;
```

For a `kind='daily'` send, you need rows with `active=1 AND cadence='daily'`.
Adjust cadence in the admin's `/subscribers` page.

**Send returns "From address is not configured (still using example.com)".**

The default `smtp_from` is `SecDigest <noreply@example.com>` — guarded
against accidentally shipping with that. Set a real From in `/settings`.

**Send returns "SMTP error: 535 5.7.8 BadCredentials".**

Auth fail. For Gmail: regenerate the App Password and paste it directly
(spaces or no spaces both fine). Re-verify in `/settings/test-smtp`.

**Email renders but Subject is empty.**

`subject` got CRLF-stripped to nothing? Check the template's `subject`
field for embedded newlines:

```sql
sqlite> SELECT name, length(subject), subject FROM email_templates WHERE id=1;
```

**Subscribers receive duplicate emails.**

The cadence filter is disjoint by design — a subscriber with
`cadence='daily'` doesn't get the weekly. If they're getting both, you
probably have two rows with the same email (case-mismatched), or you've
manually added someone via the admin who also signed up via public.
Dedup:

```sql
sqlite> SELECT lower(email), COUNT(*) FROM subscribers
        GROUP BY lower(email) HAVING COUNT(*) > 1;
```

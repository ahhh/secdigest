# Debugging

A symptom-paired troubleshooting guide. Each section: what you see, what's
likely, how to inspect, how to fix.

## Quick orientation

Three files have most of the diagnostic surface:

```bash
# Real-time application logs
journalctl -u secdigest -u secdigest-public -f

# DB state
sqlite3 /path/to/data/secdigest.db

# Recent Claude calls
sqlite3 data/secdigest.db "
  SELECT timestamp, operation, input_tokens, output_tokens, result_snippet
  FROM llm_audit_log ORDER BY id DESC LIMIT 20;
"
```

Most config is in `config_kv`. Inspect everything at once:

```bash
sqlite3 data/secdigest.db "SELECT key, value FROM config_kv ORDER BY key;"
```

## Symptom: I can't log in

**Variations:** `401`, "Wrong password" banner, redirect loop to `/login`.

```bash
# Inspect the stored hash
sqlite3 data/secdigest.db "SELECT value FROM config_kv WHERE key='password_hash';"
```

Branch on what you see:

- **No row** — `ensure_default_password()` didn't run. Restart the
  admin app; it runs in lifespan.
- **Empty value** — same diagnosis.
- **Starts with `$2b$`** — bcrypt is fine. The password you're typing
  doesn't match the hash. If you forgot it, write a new one:

  ```python
  python3 -c "import bcrypt; print(bcrypt.hashpw(b'newpw', bcrypt.gensalt()).decode())"
  # Copy the output, then:
  sqlite3 data/secdigest.db "UPDATE config_kv SET value='$2b$...' WHERE key='password_hash';"
  ```

- **Anything else** — your `.env` has `PASSWORD_HASH=` set to a non-bcrypt
  value. Either set it to a real bcrypt hash, or unset and let
  `ensure_default_password` populate it on next startup.

**If `/login` itself returns 429:** the rate limiter saw 10 failures in
the last 15 minutes. Wait, or restart the admin app (clears in-memory
buckets), or check if your `.env` has a typo'd `PASSWORD_HASH` that's
making every attempt fail.

## Symptom: redirect loop to /forced-password-change

The middleware thinks you're using the default password "secdigest".

```bash
sqlite3 data/secdigest.db \
  "SELECT value FROM config_kv WHERE key='password_hash';"
```

Run that hash through verify-against-secdigest:

```python
import bcrypt
ph = "<paste hash>"
print(bcrypt.checkpw(b"secdigest", ph.encode()))
# True → middleware is correct, you really did set it to default
# False → middleware shouldn't redirect; possible bug
```

If True, change your password via `/forced-password-change`. If False
and you still get redirected, check whether the session is stale —
clear cookies and log in again.

## Symptom: "Fetch HN" button does nothing / no articles appear

Possible causes:

1. **Already-fetched** — `run_fetch` is idempotent. If
   `db.article_count(newsletter_id) > 0` for today, it returns early.
   Confirm:

   ```bash
   sqlite3 data/secdigest.db "
     SELECT n.date, COUNT(a.id) FROM newsletters n
     LEFT JOIN articles a ON a.newsletter_id = n.id
     WHERE n.date = date('now') GROUP BY n.id;
   "
   ```

2. **Anthropic API failing** — check `last_curation_error`:

   ```bash
   sqlite3 data/secdigest.db "
     SELECT value FROM config_kv WHERE key='last_curation_error';
   "
   ```

   Non-empty → Claude failed → the keyword fallback ran. Common causes:
   missing `ANTHROPIC_API_KEY`, invalid key, rate limit, network out.
   The dismiss button on the day curator clears this.

3. **All articles scored < 5.0** — Claude judged everything irrelevant.
   Check the audit log:

   ```bash
   sqlite3 data/secdigest.db "
     SELECT timestamp, result_snippet FROM llm_audit_log
     WHERE timestamp >= datetime('now', '-1 hour')
     ORDER BY id DESC LIMIT 30;
   "
   ```

   If everything's `[2.0/10]` or similar, your curation prompt may be
   too strict. Edit it in `/prompts`.

4. **HN min score too high** — articles with HN score < `HN_MIN_SCORE`
   (default 50) are filtered before Claude even sees them. Quiet news
   day = empty pool. Lower temporarily:

   ```bash
   sqlite3 data/secdigest.db "
     UPDATE config_kv SET value='10' WHERE key='hn_min_score';
   "
   ```

5. **No `ANTHROPIC_API_KEY` at all** — check `.env`. Fallback gives
   keyword-only scores; everything ends up scored 1.0–7.0 based on
   regex matches.

## Symptom: articles have no summaries

```bash
sqlite3 data/secdigest.db "
  SELECT id, title, length(summary) FROM articles
  WHERE newsletter_id = (
    SELECT id FROM newsletters WHERE date='2026-05-04' AND kind='daily'
  );
"
```

If `length(summary)` is 0 or NULL across the board:

- **Summarizer never ran.** The button on the curator triggers
  `summarize_newsletter` via background task. Watch logs:

  ```bash
  journalctl -u secdigest -f | grep -i summar
  ```

  If you see no output after clicking, the background task crashed —
  look for tracebacks higher up.

- **Per-article failures** — try regenerate (↺) on one. Watch the live
  spinner; the `/article/<id>/json` endpoint is polled every 2 seconds.
  If the spinner spins forever, `summarize_article` raised an unhandled
  exception. Check uvicorn stdout.

- **Article body fetch failed but Claude got title-only** — summary
  should still be present (just less detailed). If totally empty,
  Claude's response was malformed.

## Symptom: the daily digest send goes to nobody

```bash
sqlite3 data/secdigest.db "
  SELECT email, active, cadence, confirmed FROM subscribers ORDER BY id;
"
```

Check that you have rows where:
- `active = 1`
- `cadence = 'daily'`
- `confirmed = 1`

If no rows match: that's the issue. Either:
- The subscribers all signed up via public and never confirmed
  (`confirmed=0`)
- They all chose weekly/monthly cadence
- They unsubscribed (`active=0`)

```bash
# Subscribers who haven't confirmed
sqlite3 data/secdigest.db "
  SELECT email, created_at FROM subscribers WHERE confirmed = 0;
"

# Cadence breakdown of active subscribers
sqlite3 data/secdigest.db "
  SELECT cadence, COUNT(*) FROM subscribers
  WHERE active = 1 GROUP BY cadence;
"
```

## Symptom: subscribers got the same email twice

**Scenario A: same email signed up twice, case-mismatched.**

```bash
sqlite3 data/secdigest.db "
  SELECT lower(email), COUNT(*) FROM subscribers
  GROUP BY lower(email) HAVING COUNT(*) > 1;
"
```

If results, dedupe manually. The public `/subscribe` route lowercases
emails, but admin-added rows don't necessarily.

**Scenario B: two admin instances both fired the daily send.**

Check `sent_at` in newsletters:

```bash
sqlite3 data/secdigest.db "
  SELECT date, status, sent_at FROM newsletters
  WHERE date >= date('now', '-7 days') AND kind='daily';
"
```

Multiple `sent_at` updates on the same row would mean the send ran
twice. Possible if you ran two `python run.py` instances simultaneously
or hit the Send button twice fast.

## Symptom: SMTP errors

**`535 5.7.8 BadCredentials`** — Gmail. The App Password is wrong.
Generate a new one at <https://myaccount.google.com/apppasswords>; spaces
or no spaces both work, paste exactly as Gmail shows it.

**`530 5.7.0 Must issue a STARTTLS command first`** — port mismatch.
Gmail wants 587 (STARTTLS) or 465 (SSL). You're set to 465 with code
that does STARTTLS, or vice versa. The `mailer._smtp_send` chooses
based on `port == 465 → SMTP_SSL`.

**`SMTP not configured — set smtp_host in Settings`** — `cfg.smtp_host`
is empty. Set it in `/settings`.

**`From address is not configured (still using example.com)`** — guard
against shipping with the default `noreply@example.com`. Set
`SMTP_FROM` in `/settings`.

**Connection times out** — firewall? Try `telnet smtp.gmail.com 587`
from the host. Cloud providers sometimes block outbound 25/465/587 by
default; you may need a special form to lift it.

**SSL cert verification fails** — your Python's CA bundle is stale.
Update `certifi` in the venv: `pip install --upgrade certifi`.

## Symptom: digest is empty / has wrong articles

**Auto-seed pulled the wrong dates** — verify the period bounds:

```python
from secdigest.periods import iso_week_bounds, month_bounds
print(iso_week_bounds("2026-05-04"))
# ('2026-05-04', '2026-05-10')
print(month_bounds("2026-05-04"))
# ('2026-05-01', '2026-05-31')
```

Then check what's in the DB for that range:

```bash
sqlite3 data/secdigest.db "
  SELECT n.date, COUNT(a.id) FROM newsletters n
  LEFT JOIN articles a ON a.newsletter_id = n.id
  WHERE n.kind='daily' AND n.date >= '2026-05-04' AND n.date <= '2026-05-10'
  GROUP BY n.id ORDER BY n.date;
"
```

Empty = nothing to seed from. Check whether your daily fetches actually
ran on those days.

**Pinned articles missing** — did the pin happen *before* or *after*
the digest was first opened? If after, the digest is already seeded
without them. Click "↺ Refresh selection" on the digest page to
re-seed.

**Articles you didn't pin showed up** — `digest_seed` fills the
remainder by relevance score. Top-N daily articles in the period get
auto-included regardless of pinning. Disable by removing them with the
"✕ Remove" button on the digest curator.

## Symptom: `python run.py` exits with `RuntimeError: TLS_ENABLED=1 but no certificate paths configured`

Pick one:

```bash
echo "TLS_DOMAIN=secdigest.example.com" >> .env       # use letsencrypt
# OR
echo "TLS_CERTFILE=/path/to/cert.pem" >> .env         # explicit
echo "TLS_KEYFILE=/path/to/key.pem" >> .env
# OR
echo "TLS_ENABLED=0" >> .env                           # plain HTTP (dev/nginx)
```

See [tls.md](tls.md) for the full story.

## Symptom: tests fail with `ModuleNotFoundError: No module named 'secdigest'`

`pytest.ini` should have `pythonpath = .` already; if it doesn't, add it.
Or run via `python -m pytest tests/` (which adds cwd to sys.path).

## Symptom: `assert 401 == 302` in `test_login_with_default_password`

Your `.env` has `PASSWORD_HASH=<your production hash>` and that's
leaking into the test DB via `_seed_config`. The `tmp_db` fixture
defangs this — make sure you're running the latest version of
`tests/conftest.py`.

```bash
git diff main tests/conftest.py
# If you don't see the monkeypatch.setenv("PASSWORD_HASH", ""), pull.
```

## Symptom: scheduler isn't firing daily

```bash
journalctl -u secdigest -n 50 | grep scheduler
# Should show:  [scheduler] daily fetch at HH:MM
```

If you don't see that line at all, the scheduler isn't starting. Check
the lifespan in `secdigest/web/app.py:21-33` — it calls
`sched.start_scheduler()` after `init_db`. If the lifespan errored
earlier (e.g. `SECRET_KEY=='dev-secret-change-me'`), the scheduler
never started.

Verify the scheduled time:

```bash
sqlite3 data/secdigest.db "
  SELECT key, value FROM config_kv WHERE key='fetch_time';
"
# 00:00 means midnight local time
```

After changing `fetch_time`, you have to **restart the admin app** for
APScheduler to pick up the new time (or call `scheduler.reschedule(t)`
from `/settings` Save, which the route does already).

## Symptom: 500 errors in the admin

```bash
journalctl -u secdigest -f --no-pager
```

Tracebacks land here. Most common causes:

- **Template errors** — Jinja syntax. Test loads templates without
  rendering: `jinja2.Environment(...).get_template("foo.html")` will
  catch syntax errors.
- **DB schema mismatch** — you pulled new code that expects a column
  that doesn't exist on your DB. Restart triggers `init_db()` which
  runs migrations.
- **Missing CSRF token on a state-changing route** — 403, not 500. The
  detail says "CSRF validation failed".

## Symptom: emails render fine in preview but mangled in Gmail

Gmail strips `<style>` blocks. Inline styles only. The built-in
templates are written this way. If you've edited a template:

- Move every `style="..."` to inline attributes (no `<style>` tags)
- Test with the **Mobile Dark** or **Mobile Light** template — they're
  tuned for Gmail iOS specifically (preheader, format-detection meta,
  fluid widths, big tap targets)
- Use the "Send Test" button in the email builder to send to your own
  inbox, then "View original" in Gmail to see the actual rendered HTML

## Useful one-liners

```bash
# Find newsletters that were never sent
sqlite3 data/secdigest.db "
  SELECT date, kind, status FROM newsletters
  WHERE sent_at IS NULL ORDER BY date DESC LIMIT 20;
"

# Articles per source over the last week
sqlite3 data/secdigest.db "
  SELECT source, source_name, COUNT(*) FROM articles a
  JOIN newsletters n ON n.id = a.newsletter_id
  WHERE n.date >= date('now', '-7 days') AND n.kind = 'daily'
  GROUP BY source, source_name ORDER BY COUNT(*) DESC;
"

# Find articles pinned but not yet in any digest
sqlite3 data/secdigest.db "
  SELECT a.id, a.title FROM articles a
  WHERE a.pin_weekly = 1
  AND NOT EXISTS (
    SELECT 1 FROM digest_articles da
    JOIN newsletters n ON n.id = da.digest_id
    WHERE da.article_id = a.id AND n.kind = 'weekly'
  );
"

# Per-day cost tracking from llm_audit_log
sqlite3 data/secdigest.db "
  SELECT date(timestamp), operation,
         SUM(input_tokens) AS in_t,
         SUM(cached_tokens) AS cached_t,
         SUM(output_tokens) AS out_t
  FROM llm_audit_log
  WHERE timestamp >= datetime('now', '-7 days')
  GROUP BY date(timestamp), operation
  ORDER BY date(timestamp) DESC;
"
```

## When all else fails

```bash
# Take a backup before nuking anything
cp data/secdigest.db data/secdigest.db.bak

# Drop today's articles to retry the fetch
sqlite3 data/secdigest.db "
  DELETE FROM articles WHERE newsletter_id = (
    SELECT id FROM newsletters WHERE date = date('now') AND kind='daily'
  );
"
# Then: Fetch button on the day curator
```

```bash
# Reset every config_kv to env defaults
sqlite3 data/secdigest.db "DELETE FROM config_kv WHERE key NOT LIKE 'tmpl_%' AND key NOT LIKE 'subject_%' AND key NOT LIKE 'toc_%';"
# Restart — _seed_config runs again with current env values
sudo systemctl restart secdigest
```

```bash
# Full reinit (loses everything but newsletters + articles)
sqlite3 data/secdigest.db "
  DELETE FROM config_kv;
  DELETE FROM email_templates;
  DELETE FROM prompts;
"
sudo systemctl restart secdigest
# init_db re-seeds defaults; you'll need to set SMTP and password from scratch
```

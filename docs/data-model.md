# Data Model

All persistence is SQLite. The schema lives in `secdigest/db.py` as the
`SCHEMA` constant — a single `executescript()` block run on every
`init_db()` call. Migrations are separate functions called after the
schema, each one idempotent.

> **🔧 Why hand-rolled SQL instead of an ORM?** The app is small and the
> queries are simple. An ORM would add ~10MB of dependencies and make every
> migration four times longer. The trade-off is that you have to write
> parameterised queries by hand — see [the SQL injection rule](#sql-injection-discipline).

## Tables at a glance

```
┌───────────────────┐      ┌───────────────────┐
│  newsletters      │ ◄────│  articles         │
│  • daily          │      │  pin_weekly       │
│  • weekly digest  │      │  pin_monthly      │
│  • monthly digest │      │  source_name      │
└───────┬───────────┘      └─────────┬─────────┘
        │                            │
        │       ┌────────────────────┘
        │       │
        ▼       ▼
   ┌──────────────────┐
   │ digest_articles  │ join table — only used by weekly/monthly newsletters
   │ digest_id        │
   │ article_id       │
   │ position         │
   │ included         │
   └──────────────────┘

┌───────────────────┐    ┌───────────────────┐
│  subscribers      │    │  rss_feeds        │
│  cadence          │    │  url, name        │
│  confirmed        │    │  active           │
│  confirm_token    │    │  max_articles     │
│  unsubscribe_token│    └───────────────────┘
└───────────────────┘

┌───────────────────┐    ┌───────────────────┐
│  prompts          │    │  email_templates  │
│  type: curation|  │    │  html             │
│        summary    │    │  article_html     │
└───────────────────┘    │  is_builtin       │
                         └───────────────────┘

┌───────────────────┐    ┌───────────────────┐
│  config_kv        │    │  llm_audit_log    │
│  free-form        │    │  one row per Claude
│  key/value        │    │  call             │
└───────────────────┘    └───────────────────┘
```

## newsletters

A unified table for daily issues, weekly digests, and monthly digests.

```sql
CREATE TABLE newsletters (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL DEFAULT 'daily',  -- 'daily' | 'weekly' | 'monthly'
    date         TEXT    NOT NULL,
    period_start TEXT    NOT NULL,
    period_end   TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'draft',  -- 'draft' | 'sent' | ...
    sent_at      TIMESTAMP,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind, period_start)
);
```

| kind     | period_start         | period_end         | URL                                   |
|----------|----------------------|--------------------|---------------------------------------|
| daily    | 2026-05-04           | 2026-05-04         | /day/2026-05-04                       |
| weekly   | 2026-05-04 (Monday)  | 2026-05-10 (Sun.)  | /week/2026-05-04                      |
| monthly  | 2026-05-01           | 2026-05-31         | /month/2026-05-01                     |

The unique constraint is `(kind, period_start)` — that lets a daily and a
weekly share the same anchor date when the daily happens to fall on a
Monday, while still preventing two weekly digests for the same week.

> **⚠️ Gotcha** — `period_start` for a weekly is **always the Monday**;
> for a monthly it's **always the 1st**. The `periods.iso_week_bounds()`
> and `periods.month_bounds()` helpers compute these — don't roll your own.

## articles

```sql
CREATE TABLE articles (
    id              INTEGER PRIMARY KEY,
    newsletter_id   INTEGER NOT NULL REFERENCES newsletters(id),
    hn_id           INTEGER,            -- HN item id, NULL for RSS or manual
    title           TEXT NOT NULL,
    url             TEXT,               -- empty for editorial notes
    hn_url          TEXT,               -- derived from hn_id at insert time
    hn_score        INTEGER DEFAULT 0,
    hn_comments     INTEGER DEFAULT 0,
    relevance_score REAL    DEFAULT 0,  -- Claude's 0-10 score
    relevance_reason TEXT,              -- one-sentence justification
    summary         TEXT,
    position        INTEGER DEFAULT 0,
    included        INTEGER DEFAULT 1,  -- 1 = in the daily newsletter, 0 = pool only
    source          TEXT    DEFAULT 'hn',  -- 'hn' | 'rss' | 'manual'
    source_name     TEXT,               -- RSS feed display name; NULL for HN/manual
    pin_weekly      INTEGER DEFAULT 0,  -- pinned to the week's digest
    pin_monthly     INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Important columns and their roles:

- `included` — 0/1 flag for "is this in the daily newsletter". Articles
  past `max_curator_articles` get `included=0` and live in the **pool**
  view (`/day/<date>/pool`) for manual promotion.
- `pin_weekly` / `pin_monthly` — flagged in the day curator. When you open
  the corresponding digest, pinned articles auto-include with `included=1`
  in the join.
- `source_name` — for RSS articles, the feed's display name (e.g. "Krebs
  on Security"). Surfaces in the day curator's ⓘ tooltip and the source
  badge.
- `position` — drag-to-reorder writes here for the daily. The digest's
  per-article position is in `digest_articles.position`, not here.

## digest_articles

```sql
CREATE TABLE digest_articles (
    digest_id  INTEGER NOT NULL REFERENCES newsletters(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id)    ON DELETE CASCADE,
    position   INTEGER DEFAULT 0,
    included   INTEGER DEFAULT 1,
    PRIMARY KEY (digest_id, article_id)
);
```

Used only by `kind != 'daily'` newsletters. The join carries
**digest-specific** position and included flags so toggling an article off
in the weekly doesn't affect the daily.

`digest_seed()` wraps the DELETE+INSERT in a single transaction
(`with conn:`) — see `db.py:digest_seed`.

## subscribers

```sql
CREATE TABLE subscribers (
    id                INTEGER PRIMARY KEY,
    email             TEXT UNIQUE NOT NULL,
    name              TEXT DEFAULT '',
    active            INTEGER DEFAULT 1,
    cadence           TEXT NOT NULL DEFAULT 'daily',   -- 'daily' | 'weekly' | 'monthly'
    confirmed         INTEGER DEFAULT 0,               -- DOI flag
    confirm_token     TEXT,                             -- single-use UUID
    unsubscribe_token TEXT,                             -- per-subscriber UUID
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Lifecycle:

| how added           | active | confirmed | tokens                                   |
|---------------------|:------:|:---------:|------------------------------------------|
| Admin /subscribers  | 1      | 1         | unsubscribe_token only                   |
| Public /subscribe   | 0      | 0         | both confirm_token and unsubscribe_token |
| Public /confirm     | →1     | →1        | confirm_token cleared on success         |
| Public /unsubscribe | →0     | (kept)    | tokens kept (so re-subscribe knows it)   |

> **🔧 Why two tokens?** `confirm_token` is single-use (cleared after
> confirmation). `unsubscribe_token` is per-subscriber and never rotates —
> we put it in every email. Mixing them would mean either the unsubscribe
> link rotates (bad UX, breaks old emails) or the confirm token sticks
> around forever (bad security).

> **⚠️ Gotcha** — `subscriber_create()` (the admin path) sets
> `confirmed=1` because the admin trusts itself.
> `subscriber_create_pending()` (the public path) sets `confirmed=0`.
> Don't conflate them.

## rss_feeds

```sql
CREATE TABLE rss_feeds (
    id           INTEGER PRIMARY KEY,
    url          TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    active       INTEGER DEFAULT 1,
    max_articles INTEGER DEFAULT 5,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

`max_articles` is the per-fetch cap — `rss.fetch_feed()` only takes the
first N items from the feed. The admin sets this via `/feeds`.

## prompts

```sql
CREATE TABLE prompts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,              -- 'curation' | 'summary'
    content     TEXT NOT NULL,
    active      INTEGER DEFAULT 1,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

- `type='curation'` — appended into the system prompt for `_score_article`
  in `fetcher.py`. The seed prompt names "Security Relevance Filter".
- `type='summary'` — appended into the summarizer's system prompt. The
  seed prompt names "Technical Summary Style".
- Multiple active prompts of the same type get concatenated with `\n\n`.

## email_templates

```sql
CREATE TABLE email_templates (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT DEFAULT '',
    subject      TEXT NOT NULL DEFAULT 'SecDigest — {date}',
    html         TEXT NOT NULL,           -- the wrapper
    article_html TEXT NOT NULL,           -- per-article snippet
    is_builtin   INTEGER DEFAULT 0,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Six built-in templates seed on first run: Dark Terminal, Clean Light,
Minimal, 2-Column Grid, Mobile Dark, Mobile Light. They're flagged
`is_builtin=1` — the admin UI lets you edit them, but `email_template_delete`
refuses to drop built-ins.

`html` contains placeholders like `{articles}` (rendered article rows)
and `{date}` / `{unsubscribe_url}` (substituted at send time). `article_html`
contains per-article placeholders like `{title}`, `{url}`, `{summary}`.

See [email.md](email.md#templates) for the full placeholder catalogue.

## config_kv

```sql
CREATE TABLE config_kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
```

The grab bag for runtime-editable config. Settings UI writes here. Also
where per-newsletter overrides live as keys like:

- `tmpl_<newsletter_id>` → email_templates.id (the chosen template for that issue)
- `subject_<newsletter_id>` → custom subject override
- `toc_<newsletter_id>` → "1" or "0" for the table of contents toggle
- `password_hash` → bcrypt hash of admin password
- `last_curation_error` → most recent Claude error, displayed in the day curator

See [configuration.md](configuration.md) for the canonical list of keys.

## llm_audit_log

```sql
CREATE TABLE llm_audit_log (
    id              INTEGER PRIMARY KEY,
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    operation       TEXT NOT NULL,           -- 'curation' or 'summary'
    model           TEXT NOT NULL,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cached_tokens   INTEGER DEFAULT 0,
    article_id      INTEGER,
    result_snippet  TEXT
);
```

One row per Claude API call. Surfaces in `/settings` as a recent-activity
log. `cached_tokens` reflects the prompt-cache hit rate when the system
prompt has `cache_control: ephemeral`.

## Migrations

`init_db()` runs the schema then a series of idempotent migration
functions. Each one checks "is the work already done?" before doing it —
running `init_db()` twice is a no-op.

Order (`secdigest/db.py:377-396`):

1. `_seed_config` — INSERT OR IGNORE the DB_CONFIG_DEFAULTS entries
2. `_seed_prompts` — only if the table is empty
3. `_seed_email_templates` — only if the table is empty
4. `_migrate_subscriber_tokens` — adds `unsubscribe_token` column, backfills UUIDs
5. `_migrate_article_source` — adds `source` column, marks blank-URL rows as 'manual'
6. `_migrate_builtin_template_unsubscribe` — adds `{unsubscribe_url}` link to old built-in templates
7. `_migrate_summary_prompt` — replaces the old summary prompt with the new "always produce a summary" version
8. `_migrate_builtin_remove_hn_links` — strips `<a href="{hn_url}">` from built-in article templates
9. `_migrate_add_grid_template` — inserts the 2-Column Grid built-in
10. `_migrate_add_mobile_templates` — inserts Mobile Dark + Mobile Light
11. `_migrate_builtin_remove_hn_points` — strips "HN N pts" text from built-in templates
12. `_migrate_newsletters_kind` — **table rebuild** to add kind/period_start/period_end and drop the old UNIQUE(date)
13. `_migrate_article_pins` — adds `pin_weekly` + `pin_monthly`
14. `_migrate_article_source_name` — adds `source_name` for RSS feed origin
15. `_migrate_subscriber_cadence` — adds `cadence`
16. `_migrate_subscriber_confirmation` — adds `confirmed` + `confirm_token`, backfills existing rows to confirmed=1

> **⚠️ Gotcha** — `_migrate_newsletters_kind` is the only one that does a
> full table rebuild (CREATE NEW + INSERT SELECT + DROP OLD + RENAME). It
> exists because SQLite `ALTER TABLE` can't drop a column-level UNIQUE
> constraint. The function turns FKs off for the duration. If you're
> debugging a corrupted newsletters table, this is the migration to study.

## SQL injection discipline

Every `db.py` function that takes user-controlled values uses parameterised
queries (`?` placeholders). The handful of f-string SQL paths interpolate
**only** server-side identifiers from a whitelist:

```python
# article_set_pin — period is validated against {'weekly', 'monthly'} first,
# THEN used to build the column name
if period not in ("weekly", "monthly"):
    raise ValueError(...)
col = f"pin_{period}"
conn.execute(f"UPDATE articles SET {col}=? WHERE id=?", ...)
```

The `*_update(**kwargs)` family (`article_update`, `subscriber_update`,
`newsletter_update`, etc.) all check `kwargs` keys against an `allowed`
set before building the `SET` clause — so a malicious key like
`"; DROP TABLE; --` raises `ValueError` rather than landing in SQL.

If you add a new helper that interpolates anything from user input into
the SQL itself: stop and use a `?` placeholder.

## Common queries (cheat sheet for `sqlite3 data/secdigest.db`)

```sql
-- Today's newsletter and its articles, by relevance
SELECT a.title, a.relevance_score, a.included, a.source
FROM articles a
JOIN newsletters n ON n.id = a.newsletter_id
WHERE n.kind = 'daily' AND n.date = date('now')
ORDER BY a.relevance_score DESC;

-- Subscribers grouped by cadence
SELECT cadence, COUNT(*) AS active
FROM subscribers
WHERE active = 1
GROUP BY cadence;

-- Last 5 Claude calls and their token usage
SELECT timestamp, operation, input_tokens, output_tokens, cached_tokens, result_snippet
FROM llm_audit_log
ORDER BY id DESC LIMIT 5;

-- Find the weekly digest for the current week and what's in it
WITH wk AS (
    SELECT id FROM newsletters WHERE kind='weekly'
    AND period_start = date('now', 'weekday 0', '-6 days')  -- last Monday
)
SELECT da.position, a.title, a.relevance_score, da.included
FROM digest_articles da
JOIN articles a ON a.id = da.article_id
WHERE da.digest_id = (SELECT id FROM wk)
ORDER BY da.position;

-- Articles pinned to the next monthly digest
SELECT n.date, a.title FROM articles a
JOIN newsletters n ON n.id = a.newsletter_id
WHERE a.pin_monthly = 1
ORDER BY n.date DESC;
```

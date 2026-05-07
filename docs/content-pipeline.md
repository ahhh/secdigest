# Content Pipeline

Three modules collaborate to turn "raw HN + RSS firehose" into "scored,
summarised articles in the curator":

```
   HN top + new ──┐                              ┌── articles.summary
                  │                              │
     RSS feeds ───┤── fetcher.run_fetch ────────┼── articles.relevance_score
                  │                              │
                  └── stored as articles rows ──┘── articles.relevance_reason
                  ▲                              ▲
                  │                              │
                  fetcher.score_articles         summarizer.summarize_article
                  (Claude curation)              (Claude per-article)
```

## fetcher.py — the HN side

Lives at `secdigest/fetcher.py`. Three responsibilities:

1. **Pull** — `fetch_all_candidates()` → top 200 + new 100 from HN's
   Firebase API
2. **Score** — `score_articles()` → per-article Claude call, returns 0-10
3. **Orchestrate** — `run_fetch(date)` → ties HN + RSS + scoring +
   storage + the HN slot reservation policy

### Pull

```python
async def _fetch_feed(client, endpoint, limit, min_score):
    ids = await _fetch_json(client, f"{HN_BASE}/{endpoint}.json")
    items = await asyncio.gather(*[fetch_one(sid) for sid in ids[:limit]])
    return [normalised dicts...]
```

`fetch_all_candidates()` issues two fetches in parallel (`topstories.json`
+ `newstories.json`), then merges them by ID. `min_score` is 50 for top
stories (tunable via `HN_MIN_SCORE`) and a generous 5 for new stories
(latest stuff hasn't accumulated upvotes yet).

> **🔧 Why pull "new" too?** Top-200 lags by hours — a fast-moving
> 0-day story may not have accumulated enough votes by the morning fetch.
> Pulling new-100 catches those.

### Score

`score_articles()` iterates the candidate list and calls
`_score_article(article, custom_instructions)` per article.

```python
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    system=[{"type": "text", "text": CURATION_SYSTEM,
             "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": user_prompt}],
)
```

The `cache_control: ephemeral` on the system prompt enables Anthropic's
prompt cache. Subsequent articles in the same fetch reuse the cached
prefix → ~80-90% input-token cost reduction. The cache hit rate is
recorded in `llm_audit_log.cached_tokens`.

> **🔧 Why per-article and not batched?** We tried batching in early
> versions — passing 25 articles in one message and expecting a JSON array
> back. Claude's outputs got truncated past about 20 articles even at
> max_tokens=4096, producing `Unterminated string` JSON errors. Per-article
> calls fail-soft (one article gets a keyword-fallback score; the others
> are unaffected) and prompt caching makes the cost roughly equivalent.

### Curation prompt

The system prompt template lives in `fetcher.py:14` (`CURATION_SYSTEM`).
The variable bit — appended via `messages` — comes from the **active**
`prompts` rows of `type='curation'`. Concatenated with `\n\n`. Edit them
in the admin's `/prompts` page.

If Claude errors out for any reason (rate limit, network, malformed
response), `_keyword_score()` falls back to regex matching against
hardcoded high/medium-relevance security keywords. This is a noisy
backstop — keyword scoring averages 1-2 points worse on real articles —
but it keeps the day's newsletter populated.

### Orchestrate — `run_fetch(date)`

This is the function APScheduler calls at `FETCH_TIME`, and the route
behind the "Fetch HN" button on the day curator.

```
1. newsletter = newsletter_get_or_create(date)
2. if newsletter already has articles → skip (idempotent)
3. candidates = HN top + new (mocked dedup by ID)
4. rss_candidates = rss.fetch_all_rss()
5. dedup against article_all_urls() — every URL we've ever stored
   (blank-URL editorial notes always allowed)
6. score every candidate (Claude → in-place mutation)
7. drop anything scoring < 5.0
8. RESERVE top hn_pool_min HN slots from the relevance ranking
9. fill remaining max_articles slots from a pure-relevance ranking of
   leftover HN + RSS
10. mark top max_curator articles as included=1; rest are pool
```

The reservation step (step 8) is the answer to "RSS-heavy days were
crowding HN out of the pool." Without it, RSS articles with high
relevance scores would beat low-relevance HN articles, and a quiet HN day
+ a noisy RSS day → no HN in the pool. The `HN_POOL_MIN` setting (default
10) carves out a guaranteed minimum.

```python
hn_count = sum(1 for s in relevant if s.get("source", "hn") == "hn")
hn_target = min(hn_pool_min, hn_count, max_articles)
reserved, remainder = [], []
for s in relevant:
    if s.get("source", "hn") == "hn" and len(reserved) < hn_target:
        reserved.append(s)
    else:
        remainder.append(s)
remainder.sort(key=lambda x: x["relevance_score"], reverse=True)
final = reserved + remainder[: max(0, max_articles - len(reserved))]
```

Edge cases this handles:

- **Fewer HN candidates than `hn_pool_min`** → `hn_target` clamps; doesn't
  steal slots that aren't there.
- **`hn_pool_min=0`** → no reservation, pure relevance fill (you can do
  this from the Feeds page).
- **No RSS feeds active** → behaviour unchanged from pre-RSS days; HN
  fills as it always did.

## rss.py — the RSS side

Lives at `secdigest/rss.py`. Plain stdlib `xml.etree.ElementTree`
parsing — no `feedparser` dep. Handles RSS 2.0 and Atom in
`fetch_feed(url, max_articles)`.

```python
def fetch_all_rss() -> list[dict]:
    feeds = db.rss_feed_active()
    all_articles = []
    for feed in feeds:
        articles = fetch_feed(feed['url'], feed.get('max_articles', 5))
        feed_label = feed.get('name') or feed['url']
        for a in articles:
            a.update({'id': None, 'score': 0, 'comments': 0,
                      'source': 'rss', 'source_name': feed_label})
        all_articles.extend(articles)
    return all_articles
```

The per-article `source_name` is the feed's display name — surfaces in
the curator's ⓘ tooltip and the meta line.

> **🔧 Why no `feedparser`?** It's a 60kLOC dependency that's optimised
> for handling 20+ years of malformed XML in the wild. We have a small
> allowlist of feeds the operator added themselves; if one breaks, fix
> the parser or drop the feed. The stdlib version is ~60 lines.

### SSRF guards

`fetch_feed` calls `is_safe_external_url(url)` before requesting. That
helper (in `web/security.py`) rejects:

- non-`http`/`https` schemes
- private IPs (RFC 1918, loopback, link-local, multicast, reserved)
- DNS resolution failures

So a feed URL pointing at `169.254.169.254` (cloud metadata service) gets
rejected at registration time *and* on every fetch.

> **⚠️ Gotcha** — `is_safe_external_url` does its own DNS resolution; the
> actual `httpx` request resolves again. A DNS-rebind attacker (different
> answers to back-to-back queries) could slip through. Mitigation: short
> timeouts (10s) + `follow_redirects=False`. Tracked as L4 in security
> review (deferred).

## summarizer.py — per-article summaries

Lives at `secdigest/summarizer.py`. Two-step:

1. **Fetch the article body** — `httpx.Client.get(url)` with redirects
   off and SSRF guard. Strip HTML tags with a regex; trim to ~1500
   characters.
2. **Send to Claude Haiku** — same `cache_control: ephemeral` system
   prompt pattern as the curator. Output goes into `articles.summary`.

```python
def summarize_article(article_id: int) -> bool:
    article = db.article_get(article_id)
    body = _fetch_article_body(article["url"])  # may be empty
    summary = _claude_summarize(article, body)
    db.article_update(article_id, summary=summary)
    return True
```

If the body fetch fails (timeout, 404, SSRF reject), Claude still gets
the title + URL and produces a summary from those alone. The summary
prompt (`prompts` table, type=`summary`) is explicit about **never
refusing** — early versions had Claude returning "I cannot access this
URL" which surfaced as the user-visible summary. The current prompt
forces Claude to write something useful from whatever's available.

### When summaries run

Three triggers:

1. `POST /day/<date>/summarize` — bulk: iterate every article in the day
2. `POST /day/<date>/article/<id>/regenerate` — single article (for the
   "↺ Regenerate" button in the curator)
3. After the daily fetch — `scheduler.daily_job` calls
   `summarizer.summarize_newsletter(newsletter_id)` once `run_fetch`
   completes

All three use `asyncio.to_thread(...)` since `summarize_article` is
synchronous.

## llm_audit_log

Every Claude call writes a row:

```sql
SELECT timestamp, operation, model,
       input_tokens, output_tokens, cached_tokens,
       result_snippet
FROM llm_audit_log
ORDER BY id DESC LIMIT 10;
```

`result_snippet` is a one-line preview — `[8.0/10] CVE-2026-1234 ... —
Critical RCE in libfoo`. Useful for spot-checking the curator's reasoning.

The `/settings` page renders the last 50 rows in a table with totals at
the top: total input tokens, total output tokens, cached-tokens
percentage. If the cached percentage drops below ~70%, your prompt cache
isn't being reused effectively (e.g. the system prompt changed between
calls within a fetch).

## Tuning

| Knob                     | Default | Effect                                    |
|--------------------------|---------|-------------------------------------------|
| `HN_MIN_SCORE`           | 50      | Skip top-stories below this many points   |
| `HN_POOL_MIN`            | 10      | Reserved HN slots in the daily pool       |
| `MAX_ARTICLES`           | 15      | Pool size per day after scoring           |
| `MAX_CURATOR_ARTICLES`   | 10      | How many of the pool are auto-included    |
| `FETCH_TIME`             | 00:00   | Local time for the daily cron             |

These are env vars seeded into `config_kv` on first run; afterwards the
`/settings` UI is the source of truth. See [configuration.md](configuration.md).

## Common debugging recipes

**The fetcher returned no articles.**

```sql
sqlite> SELECT COUNT(*) FROM articles
        WHERE newsletter_id = (SELECT id FROM newsletters WHERE date = date('now'));
```

If 0:

- Check `last_curation_error` in config_kv:
  ```sql
  sqlite> SELECT value FROM config_kv WHERE key='last_curation_error';
  ```
  If non-empty, Claude fell over → fallback ran → check `relevance_reason`
  for "keyword match" entries.
- Check that HN_MIN_SCORE isn't filtering everything out. Bump down to
  10 temporarily and refetch.
- Check `rss_feeds.active` — if you turned them all off, RSS contributes
  zero.

**Curator scores look wrong.**

Check the `llm_audit_log` for the affected fetch window:

```sql
sqlite> SELECT timestamp, result_snippet
        FROM llm_audit_log
        WHERE timestamp >= datetime('now', '-1 hour')
        ORDER BY id;
```

If you see "keyword match" reasons, Claude isn't being called — the
fallback ran. Likely cause: missing/expired `ANTHROPIC_API_KEY`.

**One article has no summary.**

Click "↺ Regenerate" on the curator. If still blank, check the article's
URL — if it's behind paywall/Cloudflare, the body fetch fails silently
and Claude gets only title+URL, but it should still produce *something*.
A truly empty summary likely means the request errored — check uvicorn's
stdout for the summarizer's print logs.

"""Fetch HN top stories and score them for security relevance via Claude.

This is the heart of the daily pipeline. The high-level flow:

    HN top + HN new + active RSS feeds
        ─►  merge & dedup against every URL ever stored
        ─►  Claude Haiku scores each new story 0–10 for security relevance
        ─►  reserve a configurable minimum number of HN slots so RSS heavy
            days don't crowd HN out
        ─►  insert top-N into the day's "pool" with the first M flagged
            ``included=1`` for the curator's default selection

A few design choices worth knowing about as you read:
- **Re-runnable**: the dedup is global (against ``article_all_urls``), so
  re-clicking "fetch" never produces duplicates. Mid-pipeline timeouts
  also leave the DB in a sane state — anything inserted before the
  cancellation is already committed.
- **Wall-clock guarded**: we wrap the network-bound work in
  ``asyncio.wait_for(...)`` so a slow upstream can't pin a worker for
  minutes. Past incident notes above the limits explain the tuning.
- **Dual-source uniform shape**: HN items and RSS items both come out as
  dicts with the same keys, so ``score_articles`` doesn't care where they
  came from.
"""
import asyncio
import json
import re
import httpx
import anthropic
from datetime import date as dt_date

from secdigest import config, db

# HN's read-only Firebase API. Every endpoint returns JSON; we hit
# ``topstories``, ``newstories``, and per-item ``item/<id>``.
HN_BASE = "https://hacker-news.firebaseio.com/v0"
# Haiku is cheap, fast, and accurate enough for a 0–10 ranking task.
MODEL = "claude-haiku-4-5-20251001"

# Network-tuning knobs. Lowered from earlier (200/100/Sem(20)/10s) after a
# stuck fetch on a slow HN day waited ~3 minutes before the operator gave up.
# At Sem(10) and limit=100, the absolute worst case is 100/10 * 5s = 50s per
# feed if every item times out — a bound the outer wall-clock guard keeps
# the whole pipeline under. See _run_fetch_pipeline / run_fetch.
_HN_TOP_LIMIT = 100
_HN_NEW_LIMIT = 50
_HN_FETCH_CONCURRENCY = 10
_HTTP_TIMEOUT_SECONDS = 5
_RUN_FETCH_WALLCLOCK_SECONDS = 120

CURATION_SYSTEM = """\
You are a security news curator. Score Hacker News articles for relevance to security
professionals. You will receive a batch of articles and must return a JSON array with
a score (0-10) and brief reason for each.

Scoring guide:
9-10: Direct security impact — active exploits, critical CVEs, major breaches, novel attack research
7-8:  Important security news — new vulns, security tools, threat intel, malware analysis
5-6:  Relevant but indirect — privacy news, security policy, interesting research
3-4:  Tangentially related — general infosec, tech news with security implications
0-2:  Not security-relevant — general tech, business, politics, entertainment

Respond with valid JSON only. No markdown fences."""


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | list | None:
    """Single HN API GET. Any failure (network, non-200, bad JSON) returns
    None so callers can treat partial outages as "skipped item" rather than
    aborting the whole feed."""
    try:
        r = await client.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def _fetch_feed(client: httpx.AsyncClient, endpoint: str,
                      limit: int, min_score: int) -> list[dict]:
    """Fetch and filter stories from a single HN feed endpoint."""
    # HN's list endpoints return just an array of item IDs; the actual
    # story details live behind one HTTP call per id.
    ids = await _fetch_json(client, f"{HN_BASE}/{endpoint}.json")
    if not ids:
        return []

    # Cap concurrency so we don't open `limit` sockets at once. With
    # _HN_FETCH_CONCURRENCY=10 the full batch trickles through in a few
    # seconds while staying polite to HN's API.
    sem = asyncio.Semaphore(_HN_FETCH_CONCURRENCY)

    async def fetch_one(sid):
        async with sem:
            return await _fetch_json(client, f"{HN_BASE}/item/{sid}.json")

    items = await asyncio.gather(*[fetch_one(sid) for sid in ids[:limit]])

    # Project HN's wire shape into the dict shape the rest of the
    # pipeline uses (note: HN's "descendants" → our "comments").
    # Filtering rules:
    #   - drop deleted/dead and non-story items (Ask HN, Show HN are still type=story; jobs/polls are filtered out)
    #   - drop anything below min_score so noise stays out of the LLM step
    #   - synthesize an HN-comments URL when the story has no off-site URL
    return [
        {
            "id":       item["id"],
            "title":    item.get("title", ""),
            "url":      item.get("url", f"https://news.ycombinator.com/item?id={item['id']}"),
            "score":    item.get("score", 0),
            "comments": item.get("descendants", 0),
        }
        for item in items
        if item
        and item.get("type") == "story"
        and not item.get("dead")
        and not item.get("deleted")
        and (item.get("score") or 0) >= min_score
    ]


async def fetch_all_candidates() -> list[dict]:
    """Fetch top and new HN stories, merged and deduplicated by ID."""
    # ``hn_min_score`` is operator-tunable: e.g., 100 for high-signal-only
    # days, 25 for slow news days. The "new" feed uses a much lower
    # threshold (5) since brand-new items haven't had time to accrete
    # upvotes yet — we'd otherwise miss fresh CVE/0-day posts.
    min_score = int(db.cfg_get("hn_min_score") or 50)
    async with httpx.AsyncClient() as client:
        top, new = await asyncio.gather(
            _fetch_feed(client, "topstories", _HN_TOP_LIMIT, min_score),
            _fetch_feed(client, "newstories", _HN_NEW_LIMIT, 5),
        )

    # An item can appear in both feeds; first-seen wins. We could use a
    # set comprehension but a manual loop preserves order (top before new).
    seen: set[int] = set()
    combined: list[dict] = []
    for story in top + new:
        if story["id"] not in seen:
            seen.add(story["id"])
            combined.append(story)

    return combined


# Keyword tables for the offline fallback path. ``_KW_HIGH`` matches
# words that strongly imply a security story (CVE, exploit, ransomware,
# etc.) and gets a 7.0 score; ``_KW_MED`` is the broader infosec
# vocabulary at 5.0. Anything that matches neither lands at 1.0 so it
# can still be sorted/considered, just very low priority.
_KW_HIGH = re.compile(
    r'\b(cve|exploit|exploited|exploiting|vulnerabilit\w+|breach|breached|malware|'
    r'ransomware|zero.day|0.day|backdoor|rce|remote.code.execution|xss|sql.injection|'
    r'injection|attack\w*|hack\w*|compromis\w+|critical|threat\w*|patch\w*|'
    r'zero.day|trojan|rootkit|keylogger|spyware|botnet|apt|phish\w+|ddos)\b',
    re.IGNORECASE,
)
_KW_MED = re.compile(
    r'\b(security|secur\w+|privacy|authenti\w+|encrypt\w+|ssl|tls|firewall|'
    r'pentest|infosec|cryptograph\w+|password|token|oauth|certif\w+|mitm|'
    r'surveillance|worm|supply.chain|credential\w*)\b',
    re.IGNORECASE,
)


def _keyword_score(articles: list[dict]) -> None:
    """Keyword fallback — scores articles in-place that don't already have relevance_score."""
    # Only fills holes — if the LLM scored *some* articles before erroring
    # mid-batch, those are kept and we keyword-score the remainder.
    for a in articles:
        if "relevance_score" not in a:
            title = a["title"]
            if _KW_HIGH.search(title):
                a["relevance_score"], a["relevance_reason"] = 7.0, "keyword match (high)"
            elif _KW_MED.search(title):
                a["relevance_score"], a["relevance_reason"] = 5.0, "keyword match (medium)"
            else:
                a["relevance_score"], a["relevance_reason"] = 1.0, "no security keywords"


def _score_article(article: dict, custom_instructions: str) -> tuple[float, str]:
    """Call Claude to score a single article. Returns (score, reason)."""
    # Per-call client construction so settings-page key rotations take
    # effect on the next fetch without a process restart.
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)

    # The model is asked to return JSON literally — system prompt forbids
    # markdown fences. We still strip a stray ``` block below as a safety
    # net because some prompts confuse newer models into wrapping output.
    user_prompt = (
        f"{custom_instructions}\n\n"
        f"Title: {article['title']}\n"
        f"URL: {article.get('url', '')}\n\n"
        f'Return JSON: {{"score": <0-10>, "reason": "<one sentence>"}}'
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=256,
        # Cache breakpoint: the system prompt is identical across calls,
        # so this is the prompt-cache hot path that drives the 80% input
        # token discount on bulk daily runs.
        system=[{"type": "text", "text": CURATION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Strip an accidental ``` fence if the model returned one. The slice
    # drops the opening fence line and (when present) the closing one.
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])

    # If JSON parsing fails the exception bubbles up to score_articles and
    # the article gets keyword-scored as part of the LLM-error fallback.
    result = json.loads(text)
    score = float(result.get("score", 0))
    reason = result.get("reason", "")
    snippet = f"[{score}/10] {article.get('title', '')[:70]} — {reason}"

    # Audit row per call gives us cost/cache visibility on the curation page.
    usage = resp.usage
    db.audit_log(
        operation="curation", model=MODEL,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cached_tokens=getattr(usage, "cache_read_input_tokens", 0),
        article_id=None, result_snippet=snippet,
    )

    return score, reason


def score_articles(articles: list[dict]) -> list[dict]:
    """Score each article individually via Claude. Modifies articles in-place."""
    # Operator-supplied curation tweaks (from the prompts table) get
    # appended to each user message. Default fallback is a generic
    # nudge to use the scoring guide in the system prompt.
    prompts = db.prompt_list(type_filter="curation")
    custom = "\n\n".join(p["content"] for p in prompts if p["active"]) or "Use the scoring guide."

    # Single-error sentinel: any failed call flips us into "fallback mode"
    # for unscored articles, and we surface the message in the settings
    # page so the operator knows the LLM step degraded.
    llm_error: str | None = None

    for article in articles:
        try:
            score, reason = _score_article(article, custom)
            article["relevance_score"] = score
            article["relevance_reason"] = reason
        except Exception as e:
            llm_error = str(e)
            print(f"[fetcher] curation error: {e}")

    if llm_error:
        # Persist the most recent error so the curator UI can show it.
        # ``_keyword_score`` only fills articles missing a score, so any
        # successful LLM scores from before the failure are preserved.
        db.cfg_set("last_curation_error", llm_error)
        _keyword_score(articles)
    else:
        # Clear stale error on a fully successful run.
        db.cfg_set("last_curation_error", "")

    # Belt-and-suspenders: guarantee every article has the keys downstream
    # code expects, even if both LLM and keyword paths somehow missed one.
    for a in articles:
        a.setdefault("relevance_score", 0.0)
        a.setdefault("relevance_reason", "")

    return articles


def _format_fetch_summary(hn: int, rss: int, new_count: int,
                          stored: int, included: int, *,
                          pool_full: bool = False) -> str:
    """Compose the human-facing one-liner shown in the day-curator banner.

    Branches by *why* a fetch produced 0 stored articles, because the operator
    can act on each cause differently:
      • feeds returned nothing → maybe HN_MIN_SCORE is too high, or RSS feeds are dead
      • dedup ate everything → normal on a re-fetch of an already-seen day
      • pool already at max_articles → drop articles or raise the cap
      • scoring rejected everything → curation prompt may be too strict
    """
    if hn == 0 and rss == 0:
        return "Pulled 0 HN + 0 RSS — feeds returned nothing"
    if new_count == 0:
        return f"Pulled {hn} HN + {rss} RSS → 0 new (all already seen)"
    if stored == 0:
        if pool_full:
            return (f"Pulled {hn} HN + {rss} RSS → {new_count} new "
                    f"but pool already at max_articles cap")
        return (f"Pulled {hn} HN + {rss} RSS → {new_count} new "
                f"but all below relevance threshold")
    return f"Pulled {hn} HN + {rss} RSS → {stored} stored, {included} included"


def _record_fetch_summary(date_str: str, hn: int, rss: int,
                          new_count: int, stored: int, included: int,
                          *, pool_full: bool = False) -> None:
    """Persist the latest fetch outcome to config_kv. Read by the day-curator
    route to render a banner — the date stamp lets the route show the banner
    only on the page that was actually fetched, not on every day view."""
    db.cfg_set("last_fetch_summary", _format_fetch_summary(
        hn, rss, new_count, stored, included, pool_full=pool_full))
    db.cfg_set("last_fetch_summary_date", date_str)


async def run_fetch(date_str: str | None = None) -> dict:
    """Full pipeline entry point. Wraps the network-bound work in a wall-clock
    guard so a slow HN day or a hung RSS feed can't pin the worker for
    minutes — past incident: fetcher hung ~3 minutes on a slow HN day with
    the prior 10s/200/100 settings, which led to a manual systemd stop.

    Idempotent on URLs (dedup runs against every URL we've ever stored), so
    a timeout that aborts mid-pipeline is safe — re-clicking picks up
    whatever wasn't stored on the prior run.
    """
    if date_str is None:
        date_str = dt_date.today().isoformat()

    newsletter = db.newsletter_get_or_create(date_str)

    # Pool-full short-circuit: if there's no room for new articles, skip the
    # network entirely. Distinct from the post-fetch pool_full case because
    # this one didn't even attempt to pull — which is the action we want
    # when the day's been topped up already.
    existing_count = db.article_count(newsletter["id"])
    max_articles = int(db.cfg_get("max_articles") or 15)
    if existing_count >= max_articles:
        print(f"[fetcher] {date_str} pool already at cap "
              f"({existing_count}/{max_articles}) — skipping")
        db.cfg_set(
            "last_fetch_summary",
            f"Pool already at max_articles cap "
            f"({existing_count}/{max_articles}) — skipped fetch",
        )
        db.cfg_set("last_fetch_summary_date", date_str)
        return newsletter

    try:
        return await asyncio.wait_for(
            _run_fetch_pipeline(newsletter, date_str, max_articles),
            timeout=_RUN_FETCH_WALLCLOCK_SECONDS,
        )
    except asyncio.TimeoutError:
        # The inner coroutine is cancelled mid-await; whatever articles it
        # had inserted up to that point are already committed (SQLite WAL +
        # per-insert commit). Surface the timeout in the banner so the
        # operator knows why nothing finished — re-clicking is safe.
        print(f"[fetcher] {date_str} fetch timed out after "
              f"{_RUN_FETCH_WALLCLOCK_SECONDS}s")
        db.cfg_set(
            "last_fetch_summary",
            f"Fetch timed out after {_RUN_FETCH_WALLCLOCK_SECONDS}s — "
            f"HN or an RSS feed may be slow. Re-click to resume.",
        )
        db.cfg_set("last_fetch_summary_date", date_str)
        return newsletter


async def _run_fetch_pipeline(newsletter: dict, date_str: str,
                              max_articles: int) -> dict:
    """The network-bound pipeline body. Pulled out so run_fetch can wrap it
    in asyncio.wait_for without losing the early-return paths."""
    # Snapshot the existing pool so re-fetches APPEND rather than reset:
    #   • position picks up where the existing pool ends
    #   • remaining caps come off the per-day totals (max_articles, max_curator)
    #   • HN reservation only kicks in if existing HN count is below hn_pool_min
    existing_articles = db.article_list(newsletter["id"])
    existing_count = len(existing_articles)
    existing_included = sum(1 for a in existing_articles if a.get("included", 1))
    existing_hn = sum(1 for a in existing_articles if a.get("source", "hn") == "hn")
    # ``position`` is monotonic per newsletter; new inserts continue from
    # max+1 so old-and-new ordering remains stable in the curator UI.
    base_position = max((a["position"] for a in existing_articles), default=-1) + 1

    print(f"[fetcher] fetching HN top + new for {date_str} "
          f"(existing pool: {existing_count})")
    candidates = await fetch_all_candidates()

    # RSS uses a sync httpx client — running it on a worker thread keeps
    # the event loop free and lets HN+RSS overlap. Local import dodges a
    # circular (rss → web.security → some flask-ish stuff at import time).
    from secdigest import rss as rss_module
    rss_candidates = await asyncio.to_thread(rss_module.fetch_all_rss)
    hn_count, rss_count = len(candidates), len(rss_candidates)

    # Global URL dedup against everything we've ever seen (across all days).
    # ``seen_urls`` also collapses duplicates inside this batch (e.g., the
    # same article appearing in both HN top and an RSS feed).
    historical_urls = db.article_all_urls()
    seen_urls: set[str] = set()
    new_stories: list[dict] = []
    for s in candidates + rss_candidates:
        url = s.get("url", "")
        if (not url or url not in historical_urls) and url not in seen_urls:
            seen_urls.add(url)
            new_stories.append(s)

    print(f"[fetcher] {hn_count} HN + {rss_count} RSS candidates, "
          f"{len(new_stories)} after dedup")

    # Early-return: nothing new today. We still record a fetch summary so
    # the operator can tell the difference between "didn't run" and "ran,
    # nothing new" on the day-curator banner.
    if not new_stories:
        _record_fetch_summary(date_str, hn=hn_count, rss=rss_count,
                              new_count=0, stored=0, included=0)
        return newsletter

    # Caps that govern this fetch's contribution to the day:
    #   max_curator: how many articles default to included=1 in total
    #   hn_pool_min: floor of HN articles per day to reserve before RSS competes
    max_curator = int(db.cfg_get("max_curator_articles") or 10)
    hn_pool_min = int(db.cfg_get("hn_pool_min") or 10)

    remaining_pool = max(0, max_articles - existing_count)
    remaining_curator = max(0, max_curator - existing_included)

    if remaining_pool == 0:
        # Race-condition fallback: the pre-check in run_fetch would normally
        # catch this, but a concurrent fetch could fill the pool while this
        # one was waiting on the network. Same banner message either way.
        _record_fetch_summary(date_str, hn=hn_count, rss=rss_count,
                              new_count=len(new_stories),
                              stored=0, included=0, pool_full=True)
        return newsletter

    print(f"[fetcher] scoring {len(new_stories)} stories...")
    scored = score_articles(new_stories)

    # Filter to "actually relevant" (>=5/10) and rank by a blended signal:
    #   relevance_score * sqrt(hn_score)
    # The sqrt damps the influence of mega-viral HN posts so a 1000-point
    # generic story doesn't outrank a 100-point but-actually-relevant CVE.
    # RSS items with no hn score act as score=0, putting them after HN ties.
    relevant = sorted(
        [s for s in scored if s["relevance_score"] >= 5.0],
        key=lambda x: x["relevance_score"] * ((x.get("score") or 0) ** 0.5),
        reverse=True,
    )

    # Reserve HN slots so RSS-heavy days don't crowd HN out. The reservation
    # runs against the day's *cumulative* HN count: if the existing pool
    # already has plenty of HN, no fresh slots get reserved this fetch.
    relevant_hn = sum(1 for s in relevant if s.get("source", "hn") == "hn")
    hn_shortfall = max(0, hn_pool_min - existing_hn)
    # Take the min of: how many we still need, how many HN items are
    # actually available in this batch, and how many open pool slots exist.
    hn_target = min(hn_shortfall, relevant_hn, remaining_pool)
    # Walk relevant in rank order, sending the first ``hn_target`` HN
    # items into the reserved bucket and everything else into remainder.
    reserved: list[dict] = []
    remainder: list[dict] = []
    for s in relevant:
        if s.get("source", "hn") == "hn" and len(reserved) < hn_target:
            reserved.append(s)
        else:
            remainder.append(s)
    # Re-sort the remainder by raw relevance (the blended sort above gave
    # a slight edge to high-HN items, but those have already been peeled
    # off into ``reserved``; for the remainder, pure relevance is fairer).
    remainder.sort(key=lambda x: x["relevance_score"], reverse=True)
    final = reserved + remainder[: max(0, remaining_pool - len(reserved))]

    # Insert into the DB in rank order. The first ``remaining_curator``
    # picks default to included=1 so the curator opens to a sensible
    # pre-built day; the rest sit in the pool awaiting manual promotion.
    for pos, story in enumerate(final):
        included = 1 if pos < remaining_curator else 0
        db.article_insert(
            newsletter_id=newsletter["id"],
            # ``hn_id`` is HN-only; RSS rows leave it NULL so we can tell
            # them apart even after ``source`` migrations.
            hn_id=story.get("id") if story.get("source", "hn") == "hn" else None,
            title=story["title"],
            url=story.get("url", ""),
            hn_score=story.get("score", 0),
            hn_comments=story.get("comments", 0),
            relevance_score=story["relevance_score"],
            relevance_reason=story["relevance_reason"],
            position=base_position + pos,
            included=included,
            source=story.get("source", "hn"),
            source_name=story.get("source_name"),
        )

    included_count = min(len(final), remaining_curator)
    print(f"[fetcher] stored {len(final)} articles "
          f"({len(reserved)} HN reserved, {len(final) - len(reserved)} other), "
          f"{included_count} included; pool now {existing_count + len(final)}")
    _record_fetch_summary(date_str, hn=hn_count, rss=rss_count,
                          new_count=len(new_stories),
                          stored=len(final), included=included_count)
    # Re-read the newsletter row so callers see any side-effects (e.g.,
    # the ``last_fetched_at`` column updated by trigger).
    return db.newsletter_get(date_str)

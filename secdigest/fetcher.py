"""Fetch HN top stories and score them for security relevance via Claude."""
import asyncio
import json
import re
import httpx
import anthropic
from datetime import date as dt_date

from secdigest import config, db

HN_BASE = "https://hacker-news.firebaseio.com/v0"
MODEL = "claude-haiku-4-5-20251001"

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
    try:
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def _fetch_feed(client: httpx.AsyncClient, endpoint: str,
                      limit: int, min_score: int) -> list[dict]:
    """Fetch and filter stories from a single HN feed endpoint."""
    ids = await _fetch_json(client, f"{HN_BASE}/{endpoint}.json")
    if not ids:
        return []

    sem = asyncio.Semaphore(20)

    async def fetch_one(sid):
        async with sem:
            return await _fetch_json(client, f"{HN_BASE}/item/{sid}.json")

    items = await asyncio.gather(*[fetch_one(sid) for sid in ids[:limit]])

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
    min_score = int(db.cfg_get("hn_min_score") or 50)
    async with httpx.AsyncClient() as client:
        top, new = await asyncio.gather(
            _fetch_feed(client, "topstories", 200, min_score),
            _fetch_feed(client, "newstories", 100, 5),
        )

    seen: set[int] = set()
    combined: list[dict] = []
    for story in top + new:
        if story["id"] not in seen:
            seen.add(story["id"])
            combined.append(story)

    return combined


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
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)

    user_prompt = (
        f"{custom_instructions}\n\n"
        f"Title: {article['title']}\n"
        f"URL: {article.get('url', '')}\n\n"
        f'Return JSON: {{"score": <0-10>, "reason": "<one sentence>"}}'
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=[{"type": "text", "text": CURATION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])

    result = json.loads(text)
    score = float(result.get("score", 0))
    reason = result.get("reason", "")
    snippet = f"[{score}/10] {article.get('title', '')[:70]} — {reason}"

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
    prompts = db.prompt_list(type_filter="curation")
    custom = "\n\n".join(p["content"] for p in prompts if p["active"]) or "Use the scoring guide."

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
        db.cfg_set("last_curation_error", llm_error)
        _keyword_score(articles)
    else:
        db.cfg_set("last_curation_error", "")

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
    """Full pipeline: fetch HN + RSS → dedup → score → append unique articles
    into the day's pool. Idempotent on URLs (the dedup runs against every URL
    we've ever stored), so re-running on the same day is safe — it tops up
    the pool with whatever is freshly available rather than short-circuiting.

    Returns the newsletter dict.
    """
    if date_str is None:
        date_str = dt_date.today().isoformat()

    newsletter = db.newsletter_get_or_create(date_str)

    # Snapshot the existing pool so re-fetches APPEND rather than reset:
    #   • position picks up where the existing pool ends
    #   • remaining caps come off the per-day totals (max_articles, max_curator)
    #   • HN reservation only kicks in if existing HN count is below hn_pool_min
    existing_articles = db.article_list(newsletter["id"])
    existing_count = len(existing_articles)
    existing_included = sum(1 for a in existing_articles if a.get("included", 1))
    existing_hn = sum(1 for a in existing_articles if a.get("source", "hn") == "hn")
    base_position = max((a["position"] for a in existing_articles), default=-1) + 1

    print(f"[fetcher] fetching HN top + new for {date_str} "
          f"(existing pool: {existing_count})")
    candidates = await fetch_all_candidates()

    from secdigest import rss as rss_module
    rss_candidates = await asyncio.to_thread(rss_module.fetch_all_rss)
    hn_count, rss_count = len(candidates), len(rss_candidates)

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

    if not new_stories:
        _record_fetch_summary(date_str, hn=hn_count, rss=rss_count,
                              new_count=0, stored=0, included=0)
        return newsletter

    max_articles = int(db.cfg_get("max_articles") or 15)
    max_curator = int(db.cfg_get("max_curator_articles") or 10)
    hn_pool_min = int(db.cfg_get("hn_pool_min") or 10)

    remaining_pool = max(0, max_articles - existing_count)
    remaining_curator = max(0, max_curator - existing_included)

    if remaining_pool == 0:
        # Pool already at the per-day cap. Distinct from "all already seen" —
        # we *did* find new candidates, but there's no room. Operator action:
        # bump max_articles or drop something from the pool.
        _record_fetch_summary(date_str, hn=hn_count, rss=rss_count,
                              new_count=len(new_stories),
                              stored=0, included=0, pool_full=True)
        return newsletter

    print(f"[fetcher] scoring {len(new_stories)} stories...")
    scored = score_articles(new_stories)

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
    hn_target = min(hn_shortfall, relevant_hn, remaining_pool)
    reserved: list[dict] = []
    remainder: list[dict] = []
    for s in relevant:
        if s.get("source", "hn") == "hn" and len(reserved) < hn_target:
            reserved.append(s)
        else:
            remainder.append(s)
    remainder.sort(key=lambda x: x["relevance_score"], reverse=True)
    final = reserved + remainder[: max(0, remaining_pool - len(reserved))]

    for pos, story in enumerate(final):
        # New articles default to included=1 only while curator slots remain;
        # the rest land in the pool for manual promotion.
        included = 1 if pos < remaining_curator else 0
        db.article_insert(
            newsletter_id=newsletter["id"],
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
    return db.newsletter_get(date_str)

"""Fetch HN top stories and score for security relevance via Claude."""
import asyncio
import json
import httpx
import anthropic
from datetime import date as dt_date

import config
import db

HN_BASE = "https://hacker-news.firebaseio.com/v0"
MODEL = "claude-haiku-4-5-20251001"

CURATION_SYSTEM = """\
You are a security news curator. Your job is to score Hacker News articles for relevance
to security professionals. You will receive a batch of articles and must return a JSON array
with a score (0-10) and brief reason for each.

Scoring guide:
9-10: Direct security impact — active exploits, critical CVEs, major breaches, novel attack research
7-8:  Important security news — new vulns, security tools, threat intel, malware analysis
5-6:  Relevant but indirect — privacy news, security policy, interesting research
3-4:  Tangentially related — general infosec, tech news with security implications
0-2:  Not security-relevant — general tech, business, politics, entertainment

Respond with valid JSON only. No markdown. No explanation outside the JSON array."""


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | list | None:
    try:
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def fetch_top_stories(limit: int = 200) -> list[dict]:
    """Fetch top N HN stories meeting the minimum score threshold."""
    min_score = int(db.cfg_get("hn_min_score") or 50)
    async with httpx.AsyncClient() as client:
        ids = await _fetch_json(client, f"{HN_BASE}/topstories.json")
        if not ids:
            return []
        ids = ids[:limit]

        sem = asyncio.Semaphore(20)

        async def fetch_one(sid):
            async with sem:
                return await _fetch_json(client, f"{HN_BASE}/item/{sid}.json")

        items = await asyncio.gather(*[fetch_one(sid) for sid in ids])

    stories = []
    for item in items:
        if not item:
            continue
        if item.get("type") != "story":
            continue
        if item.get("dead") or item.get("deleted"):
            continue
        if (item.get("score") or 0) < min_score:
            continue
        stories.append({
            "id":       item["id"],
            "title":    item.get("title", ""),
            "url":      item.get("url", f"https://news.ycombinator.com/item?id={item['id']}"),
            "score":    item.get("score", 0),
            "comments": item.get("descendants", 0),
        })

    return stories


def _score_batch(articles: list[dict], custom_instructions: str) -> list[dict]:
    """Call Claude to score a batch of articles. Returns list with id/score/reason."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    article_list = [
        {"id": a["id"], "title": a["title"], "url": a.get("url", "")}
        for a in articles
    ]

    user_prompt = (
        f"{custom_instructions}\n\n"
        f"Articles to score:\n{json.dumps(article_list, indent=2)}\n\n"
        "Return a JSON array: "
        '[{"id": <hn_id>, "score": <0-10>, "reason": "<one sentence>"}]'
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": CURATION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    usage = resp.usage
    db.audit_log(
        operation="curation",
        model=MODEL,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=getattr(usage, "cache_read_input_tokens", 0),
        article_id=None,
        result_snippet=resp.content[0].text[:300],
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])

    return json.loads(text)


def score_articles(articles: list[dict]) -> list[dict]:
    """Score all articles in batches of 25. Returns articles with relevance_score/reason added."""
    prompts = db.prompt_list(type_filter="curation")
    custom = "\n\n".join(p["content"] for p in prompts if p["active"])
    if not custom:
        custom = "Use the scoring guide above."

    results_map: dict[int, dict] = {}
    batch_size = 25
    for i in range(0, len(articles), batch_size):
        batch = articles[i : i + batch_size]
        try:
            scored = _score_batch(batch, custom)
            for item in scored:
                results_map[item["id"]] = item
        except Exception as e:
            print(f"[fetcher] curation batch {i//batch_size} error: {e}")

    enriched = []
    for a in articles:
        scored = results_map.get(a["id"], {})
        a["relevance_score"] = float(scored.get("score", 0))
        a["relevance_reason"] = scored.get("reason", "")
        enriched.append(a)

    return enriched


async def run_fetch(date_str: str | None = None) -> dict:
    """Full pipeline: fetch → score → insert into DB. Returns newsletter dict."""
    if date_str is None:
        date_str = dt_date.today().isoformat()

    newsletter = db.newsletter_get_or_create(date_str)
    existing_ids = db.article_hn_ids(newsletter["id"])

    print(f"[fetcher] fetching HN top stories for {date_str}...")
    stories = await fetch_top_stories()
    new_stories = [s for s in stories if s["id"] not in existing_ids]
    print(f"[fetcher] {len(stories)} stories, {len(new_stories)} new")

    if not new_stories:
        return newsletter

    print(f"[fetcher] scoring {len(new_stories)} stories for security relevance...")
    scored = score_articles(new_stories)

    threshold = 5.0
    relevant = [s for s in scored if s["relevance_score"] >= threshold]
    relevant.sort(key=lambda x: x["relevance_score"] * (x["score"] ** 0.5), reverse=True)

    max_articles = int(db.cfg_get("max_articles") or 15)
    to_insert = relevant[:max_articles]

    for pos, story in enumerate(to_insert):
        db.article_insert(
            newsletter_id=newsletter["id"],
            hn_id=story["id"],
            title=story["title"],
            url=story["url"],
            hn_score=story["score"],
            hn_comments=story["comments"],
            relevance_score=story["relevance_score"],
            relevance_reason=story["relevance_reason"],
            position=pos,
        )

    print(f"[fetcher] inserted {len(to_insert)} articles")
    return db.newsletter_get(date_str)

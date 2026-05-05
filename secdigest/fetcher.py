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


async def fetch_top_stories(limit: int = 200) -> list[dict]:
    """Return top HN stories meeting the configured minimum score."""
    min_score = int(db.cfg_get("hn_min_score") or 50)
    async with httpx.AsyncClient() as client:
        ids = await _fetch_json(client, f"{HN_BASE}/topstories.json")
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


def _keyword_score(articles: list[dict]) -> list[dict]:
    """Simple keyword fallback when Claude is unavailable."""
    results = []
    for a in articles:
        title = a["title"]
        if _KW_HIGH.search(title):
            score, reason = 7.0, "keyword match (high)"
        elif _KW_MED.search(title):
            score, reason = 5.0, "keyword match (medium)"
        else:
            score, reason = 1.0, "no security keywords"
        results.append({"id": a["id"], "score": score, "reason": reason})
    return results


def _score_batch(articles: list[dict], custom_instructions: str) -> list[dict]:
    """Call Claude to score one batch of articles. Returns [{id, score, reason}]."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    article_list = [
        {"id": a["id"], "title": a["title"], "url": a.get("url", "")}
        for a in articles
    ]
    user_prompt = (
        f"{custom_instructions}\n\n"
        f"Articles:\n{json.dumps(article_list, indent=2)}\n\n"
        'Return JSON array: [{"id": <hn_id>, "score": <0-10>, "reason": "<one sentence>"}]'
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": CURATION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    usage = resp.usage
    db.audit_log(
        operation="curation", model=MODEL,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cached_tokens=getattr(usage, "cache_read_input_tokens", 0),
        article_id=None, result_snippet=resp.content[0].text[:300],
    )

    text = resp.content[0].text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])

    return json.loads(text)


def score_articles(articles: list[dict]) -> list[dict]:
    """Score all articles in batches of 25. Adds relevance_score/reason to each."""
    prompts = db.prompt_list(type_filter="curation")
    custom = "\n\n".join(p["content"] for p in prompts if p["active"]) or "Use the scoring guide."

    scores: dict[int, dict] = {}
    llm_error: str | None = None

    for i in range(0, len(articles), 25):
        try:
            for item in _score_batch(articles[i:i + 25], custom):
                scores[item["id"]] = item
        except Exception as e:
            llm_error = str(e)
            print(f"[fetcher] curation batch error: {e}")

    if llm_error:
        db.cfg_set("last_curation_error", llm_error)
        unscored = [a for a in articles if a["id"] not in scores]
        print(f"[fetcher] falling back to keyword scoring for {len(unscored)} articles")
        for item in _keyword_score(unscored):
            scores[item["id"]] = item
    else:
        db.cfg_set("last_curation_error", "")

    for a in articles:
        s = scores.get(a["id"], {})
        a["relevance_score"] = float(s.get("score", 0))
        a["relevance_reason"] = s.get("reason", "")

    return articles


async def run_fetch(date_str: str | None = None) -> dict:
    """Full pipeline: fetch HN → score → store. Returns the newsletter dict."""
    if date_str is None:
        date_str = dt_date.today().isoformat()

    newsletter = db.newsletter_get_or_create(date_str)
    existing_ids = db.article_hn_ids(newsletter["id"])

    if existing_ids:
        print(f"[fetcher] {date_str} already has {len(existing_ids)} articles, skipping")
        return newsletter

    print(f"[fetcher] fetching HN for {date_str}...")
    stories = await fetch_top_stories()
    new_stories = [s for s in stories if s["id"] not in existing_ids]
    print(f"[fetcher] {len(stories)} stories, {len(new_stories)} new")

    if not new_stories:
        return newsletter

    print(f"[fetcher] scoring {len(new_stories)} stories...")
    scored = score_articles(new_stories)

    relevant = sorted(
        [s for s in scored if s["relevance_score"] >= 5.0],
        key=lambda x: x["relevance_score"] * (x["score"] ** 0.5),
        reverse=True,
    )

    max_articles = int(db.cfg_get("max_articles") or 15)
    for pos, story in enumerate(relevant[:max_articles]):
        db.article_insert(
            newsletter_id=newsletter["id"],
            hn_id=story["id"], title=story["title"], url=story["url"],
            hn_score=story["score"], hn_comments=story["comments"],
            relevance_score=story["relevance_score"],
            relevance_reason=story["relevance_reason"],
            position=pos,
        )

    print(f"[fetcher] inserted {min(len(relevant), max_articles)} articles")
    return db.newsletter_get(date_str)

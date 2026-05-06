"""Generate Claude summaries for newsletter articles."""
import re
import httpx
import anthropic
from secdigest import config, db
from secdigest.web.security import is_safe_external_url

MODEL = "claude-haiku-4-5-20251001"

SUMMARY_SYSTEM = """\
You are writing summaries for a daily security newsletter read by security professionals.
You MUST always write a summary — never refuse, never say you cannot access a URL.
Article text is fetched for you and provided below. If no text is available, write from the title alone.
Every article gets exactly 2-3 sentences. Adapt to the content type:
- Vulnerability / CVE: what it is, who is affected, severity, CVE ID and mitigations if known
- Tool / research: what it does, the key technical insight, and why it matters
- Opinion / discussion: the core argument, its security relevance, and the key takeaway
- Compliance / policy: what changed, who it affects, and the practical implication
Be precise and direct. No marketing language, no hedging, no preamble.
Respond with the summary text only."""


_CONTENT_TAGS = re.compile(
    r'<(article|main|div[^>]*(?:content|post|entry|body)[^>]*)[\s>]',
    re.IGNORECASE,
)

def _fetch_article_text(url: str, max_chars: int = 1500) -> str:
    """Fetch article text. Tries to start from the main content block; caps at max_chars."""
    if not url or "news.ycombinator.com" in url or not is_safe_external_url(url):
        return ""
    try:
        with httpx.Client(follow_redirects=False, timeout=8,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; SecDigest/1.0)"}) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return ""
        ct = resp.headers.get("content-type", "")
        if "text/html" not in ct and "text/plain" not in ct:
            return ""
        html = resp.text

        # Start from the first recognisable content block if present
        m = _CONTENT_TAGS.search(html)
        if m:
            html = html[m.start():]

        html = re.sub(r'<(script|style|nav|header|footer)[^>]*>.*?</\1>', ' ',
                      html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<[^>]+>', ' ', html)
        html = re.sub(r'&[a-zA-Z]+;', ' ', html)
        return re.sub(r'\s+', ' ', html).strip()[:max_chars]
    except Exception:
        return ""


def _summary_instructions() -> str:
    prompts = db.prompt_list(type_filter="summary")
    active = [p["content"] for p in prompts if p["active"]]
    return "\n\n".join(active) if active else ""


def summarize_article(article_id: int) -> str | None:
    """Generate (or regenerate) a Claude summary for one article. Returns text or None."""
    article = db.article_get(article_id)
    if not article:
        return None

    article_text = _fetch_article_text(article.get("url", ""))

    instructions = _summary_instructions()
    user_prompt = "\n".join(filter(None, [
        instructions,
        f"Title: {article['title']}",
        f"URL: {article.get('url', '')}",
        f"HN score: {article.get('hn_score', 0)} | Comments: {article.get('hn_comments', 0)}",
        f"\nArticle content:\n{article_text}" if article_text
            else "\n(Article text could not be fetched — summarise from title and context.)",
    ]))

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=[{"type": "text", "text": SUMMARY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_prompt}],
        )
        usage = resp.usage
        summary = resp.content[0].text.strip()

        db.audit_log(
            operation="summary", model=MODEL,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0),
            article_id=article_id, result_snippet=summary[:300],
        )
        db.article_update(article_id, summary=summary)
        return summary

    except Exception as e:
        print(f"[summarizer] error on article {article_id}: {e}")
        return None


def summarize_newsletter(newsletter_id: int) -> int:
    """Summarize all unsummarized articles in a newsletter. Returns count generated."""
    count = 0
    for article in db.article_list(newsletter_id):
        if not article.get("summary"):
            if summarize_article(article["id"]):
                count += 1
    return count

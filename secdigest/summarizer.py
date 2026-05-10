"""Generate Claude summaries for newsletter articles.

Two-step pipeline per article:
1. Fetch the source page over HTTP and reduce it to a chunk of plain text
   (we strip scripts/nav/footer chrome and start from the first content
   block we recognise). Capped at ~1500 chars to keep token costs down.
2. Send that text plus title/URL to Claude Haiku and store the resulting
   2-3 sentence summary back on the article row.

Why a "system prompt + cache_control" pattern? The system prompt is the
same for every call, so marking it as an ephemeral cache breakpoint lets
Anthropic's prompt-cache hit on the prefix and bill input tokens at a
heavy discount on bulk runs (≈80%). See ``audit_log`` in db.py for how
we track the cache_read counts.
"""
import re
from urllib.parse import urljoin

import httpx
import anthropic
from secdigest import config, db
from secdigest.web.security import is_safe_external_url

# Haiku is fast + cheap and sufficient for short summaries. Pinned to a
# specific snapshot to keep summary style stable across releases.
MODEL = "claude-haiku-4-5-20251001"

# Cap on hops we'll walk through 30x responses before giving up. Five
# covers the common publisher patterns (canonical-URL redirect, consent
# bounce, country-TLD swap, scheme upgrade) without letting a redirect
# loop run away.
_MAX_REDIRECTS = 5

# The system prompt does the heavy lifting on output shape. Constraining
# format up front (2-3 sentences, no preamble) is what keeps the daily
# newsletter looking consistent across hundreds of summaries. Operators
# can append extra rules at runtime via the prompts table — see
# ``_summary_instructions`` below.
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


# Heuristic for finding "where the actual article begins" in raw HTML —
# matches the opening tag of <article>, <main>, or any <div> whose attrs
# contain content/post/entry/body. We use the first match's offset to
# trim away the page chrome that sits above the article body.
_CONTENT_TAGS = re.compile(
    r'<(article|main|div[^>]*(?:content|post|entry|body)[^>]*)[\s>]',
    re.IGNORECASE,
)


def _fetch_article_text(url: str, max_chars: int = 1500) -> str:
    """Fetch article text. Tries to start from the main content block; caps at max_chars."""
    # Skip cases where there's nothing useful to fetch:
    #  - missing URL
    #  - HN comment-page URLs (no real article content there)
    #  - URLs that fail the SSRF allow-list (private/loopback/file://)
    if not url or "news.ycombinator.com" in url or not is_safe_external_url(url):
        return ""
    try:
        # Follow redirects manually so the SSRF guard runs on EVERY hop —
        # bare follow_redirects=True would let a 302 → internal IP through.
        # Past incident: blogspot posts produced "Unable to retrieve article
        # content" because Blogger issues 30x for canonical/consent flows
        # and the prior follow_redirects=False silently dropped the response.
        with httpx.Client(follow_redirects=False, timeout=8,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; SecDigest/1.0)"}) as client:
            current = url
            resp = None
            for _ in range(_MAX_REDIRECTS + 1):
                resp = client.get(current)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = resp.headers.get("location", "")
                if not loc:
                    print(f"[summarizer] {url}: {resp.status_code} with no Location header")
                    return ""
                # Resolve relative redirects against the URL we just fetched,
                # not the original — matters for `Location: /canonical/x`.
                next_url = urljoin(current, loc)
                if not is_safe_external_url(next_url):
                    print(f"[summarizer] {url}: redirect to unsafe URL blocked ({next_url})")
                    return ""
                current = next_url
            else:
                print(f"[summarizer] {url}: too many redirects (>{_MAX_REDIRECTS})")
                return ""
        if resp.status_code != 200:
            print(f"[summarizer] {url}: HTTP {resp.status_code}")
            return ""
        # Defensive content-type check: the parser below assumes textual
        # input. PDFs, images, videos etc. are skipped.
        ct = resp.headers.get("content-type", "")
        if "text/html" not in ct and "text/plain" not in ct:
            print(f"[summarizer] {url}: skipping content-type {ct!r}")
            return ""
        html = resp.text

        # If we recognise an article container, advance past the page
        # header/nav so the LLM sees mostly body text. If not, we fall
        # back to processing the whole document.
        m = _CONTENT_TAGS.search(html)
        if m:
            html = html[m.start():]

        # 4-step reduction: drop noise tags + their inner content,
        # strip remaining tags, replace HTML entities, collapse
        # whitespace. This isn't a real HTML parser — it's a "good
        # enough for prose extraction" filter. We accept that some
        # entities and edge cases will be mangled.
        html = re.sub(r'<(script|style|nav|header|footer)[^>]*>.*?</\1>', ' ',
                      html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<[^>]+>', ' ', html)
        html = re.sub(r'&[a-zA-Z]+;', ' ', html)
        return re.sub(r'\s+', ' ', html).strip()[:max_chars]
    except Exception as e:
        # Any network/parse failure: degrade gracefully to "no body text" —
        # the model still gets the title + HN context. Log so the operator
        # can see WHY the body was missing instead of guessing from the
        # "Unable to retrieve article content" summary text.
        print(f"[summarizer] {url}: fetch error: {e}")
        return ""


def _summary_instructions() -> str:
    """Pull operator-edited summary guidelines from the DB and concat them.
    Lets you tweak summary style on the Settings page without redeploying."""
    prompts = db.prompt_list(type_filter="summary")
    active = [p["content"] for p in prompts if p["active"]]
    return "\n\n".join(active) if active else ""


def summarize_article(article_id: int) -> str | None:
    """Generate (or regenerate) a Claude summary for one article. Returns text or None."""
    article = db.article_get(article_id)
    if not article:
        return None

    # Body text is best-effort — None just means we'll prompt with title only.
    article_text = _fetch_article_text(article.get("url", ""))

    # Compose the user message: operator instructions first, then a
    # compact factbox (title/URL/HN signals), then the body if we have one.
    # ``filter(None, ...)`` drops the body line entirely when it's empty,
    # avoiding a stray "Article content:" header with nothing under it.
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
        # Construct the client per-call so we always pick up the *current*
        # ANTHROPIC_API_KEY (the key may be supplied via env on first run
        # but rotated later — anthropic.Anthropic() doesn't auto-refresh).
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            # ``cache_control: ephemeral`` marks this block as a cache
            # breakpoint. Subsequent calls with the same system prompt
            # hit the prompt cache, billing those tokens at the cache-read rate.
            system=[{"type": "text", "text": SUMMARY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_prompt}],
        )
        usage = resp.usage
        summary = resp.content[0].text.strip()

        # Persist usage stats so the operator can see input/output/cache
        # token counts per article and tally cost. ``getattr`` with a
        # default keeps us robust to older SDK versions without the field.
        db.audit_log(
            operation="summary", model=MODEL,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0),
            article_id=article_id, result_snippet=summary[:300],
        )
        db.article_update(article_id, summary=summary)
        return summary

    except Exception as e:
        # API errors, rate limits, network blips: log and return None.
        # The caller (``summarize_newsletter``) just skips this article;
        # the curator can hit "regenerate" from the UI later.
        print(f"[summarizer] error on article {article_id}: {e}")
        return None


def summarize_newsletter(newsletter_id: int) -> int:
    """Summarize all unsummarized articles in a newsletter. Returns count generated."""
    # Idempotent: existing summaries are left alone, so this is safe to
    # re-run after a partial failure or to fill in late additions.
    count = 0
    for article in db.article_list(newsletter_id):
        if not article.get("summary"):
            if summarize_article(article["id"]):
                count += 1
    return count

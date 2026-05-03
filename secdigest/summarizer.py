"""Generate Claude summaries for newsletter articles."""
import anthropic
from secdigest import config, db

MODEL = "claude-haiku-4-5-20251001"

SUMMARY_SYSTEM = """\
You are writing summaries for a daily security newsletter read by security professionals.
Each summary must be 2-3 sentences. Be precise, factual, and technical.
Include CVE IDs, affected versions, severity, and mitigations when available.
Never use marketing language or hedging phrases like "it seems" or "reportedly".
Respond with the summary text only — no labels, no preamble."""


def _summary_instructions() -> str:
    prompts = db.prompt_list(type_filter="summary")
    active = [p["content"] for p in prompts if p["active"]]
    return "\n\n".join(active) if active else ""


def summarize_article(article_id: int) -> str | None:
    """Generate (or regenerate) a Claude summary for one article. Returns text or None."""
    article = db.article_get(article_id)
    if not article:
        return None

    instructions = _summary_instructions()
    user_prompt = "\n".join(filter(None, [
        instructions,
        f"Article title: {article['title']}",
        f"URL: {article.get('url', '')}",
        f"HN discussion: {article.get('hn_url', '')}",
        f"HN score: {article.get('hn_score', 0)} | Comments: {article.get('hn_comments', 0)}",
    ]))

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
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

"""Send newsletter emails via SMTP."""
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from secdigest import db


def render_email_html(newsletter: dict, articles: list[dict], template_id: int | None = None) -> str:
    """Render the newsletter as HTML using the specified or configured template."""
    template = db.email_template_get(template_id) if template_id else None
    if not template:
        tid = db.newsletter_get_template_id(newsletter["id"])
        template = db.email_template_get(tid) if tid else None
    if not template:
        template = db.email_template_default()
    if not template:
        return "<html><body><p>No email template configured.</p></body></html>"

    rows = ""
    n = 0
    for a in articles:
        if not a.get("included", 1):
            continue
        n += 1
        summary = a.get("summary") or "<em>No summary generated.</em>"
        url = a.get("url") or a.get("hn_url", "#")
        row = template["article_html"]
        row = row.replace("{number}", str(n))
        row = row.replace("{title}", a.get("title", ""))
        row = row.replace("{url}", url)
        row = row.replace("{hn_url}", a.get("hn_url", "#"))
        row = row.replace("{summary}", summary)
        row = row.replace("{hn_score}", str(a.get("hn_score", 0)))
        row = row.replace("{hn_comments}", str(a.get("hn_comments", 0)))
        rows += row

    html = template["html"]
    html = html.replace("{articles}", rows)
    html = html.replace("{date}", newsletter["date"])
    return html


def _render_text(newsletter: dict, articles: list[dict]) -> str:
    lines = [f"SecDigest — {newsletter['date']}", "=" * 40, ""]
    for i, a in enumerate(articles):
        if not a.get("included", 1):
            continue
        lines += [
            f"{i+1}. {a['title']}",
            f"   {a.get('url') or a.get('hn_url', '')}",
            f"   {a.get('summary', 'No summary.')}",
            f"   HN: {a.get('hn_url','')} ({a.get('hn_score',0)} pts)",
            "",
        ]
    return "\n".join(lines)


def send_newsletter(date_str: str) -> tuple[bool, str]:
    """Send the newsletter for date_str to all active subscribers."""
    newsletter = db.newsletter_get(date_str)
    if not newsletter:
        return False, f"No newsletter found for {date_str}"

    articles = db.article_list(newsletter["id"])
    if not any(a.get("included", 1) for a in articles):
        return False, "No included articles to send"

    subscribers = db.subscriber_active()
    if not subscribers:
        return False, "No active subscribers"

    cfg = db.cfg_all()
    smtp_host = cfg.get("smtp_host", "")
    if not smtp_host:
        return False, "SMTP not configured — set smtp_host in Settings"

    html_body = render_email_html(newsletter, articles)
    text_body = _render_text(newsletter, articles)

    subject_override = db.newsletter_get_subject(newsletter["id"])
    template = db.email_template_get(db.newsletter_get_template_id(newsletter["id"]) or 0)
    default_subject = template["subject"] if template else "SecDigest — {date}"
    subject = (subject_override or default_subject).replace("{date}", date_str)

    smtp_from = cfg.get("smtp_from", "SecDigest <noreply@example.com>")

    sent, errors = 0, []
    try:
        with smtplib.SMTP(smtp_host, int(cfg.get("smtp_port", 587))) as server:
            server.starttls()
            if cfg.get("smtp_user"):
                server.login(cfg["smtp_user"], cfg.get("smtp_pass", ""))
            for sub in subscribers:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = smtp_from
                msg["To"] = sub["email"]
                msg.attach(MIMEText(text_body, "plain"))
                msg.attach(MIMEText(html_body, "html"))
                try:
                    server.send_message(msg)
                    sent += 1
                except Exception as e:
                    errors.append(f"{sub['email']}: {e}")
    except Exception as e:
        return False, f"SMTP connection failed: {e}"

    db.newsletter_update(newsletter["id"], status="sent", sent_at=datetime.utcnow().isoformat())
    msg = f"Sent to {sent} subscribers"
    if errors:
        msg += f". Errors: {'; '.join(errors)}"
    return True, msg

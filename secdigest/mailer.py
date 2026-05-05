"""Send newsletter emails via SMTP."""
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from secdigest import db


def _render_article(article_html: str, a: dict, n: int) -> str:
    url = a.get("url") or a.get("hn_url") or ""
    summary = a.get("summary") or "<em>No summary generated.</em>"
    s = article_html
    s = s.replace("{number}", str(n))
    s = s.replace("{title}", a.get("title", ""))
    s = s.replace("{url}", url)
    s = s.replace("{hn_url}", a.get("hn_url") or "")
    s = s.replace("{summary}", summary)
    s = s.replace("{hn_score}", str(a.get("hn_score", 0)))
    s = s.replace("{hn_comments}", str(a.get("hn_comments", 0)))
    return s


def render_email_html(newsletter: dict, articles: list[dict],
                      template_id: int | None = None,
                      unsubscribe_url: str = "") -> str:
    """Render the newsletter as HTML using the specified or configured template."""
    template = db.email_template_get(template_id) if template_id else None
    if not template:
        tid = db.newsletter_get_template_id(newsletter["id"])
        template = db.email_template_get(tid) if tid else None
    if not template:
        template = db.email_template_default()
    if not template:
        return "<html><body><p>No email template configured.</p></body></html>"

    included = [a for a in articles if a.get("included", 1)]
    art_html = template["article_html"]

    # Standard single-column list
    rows_1col = "".join(_render_article(art_html, a, i + 1) for i, a in enumerate(included))

    # 2-column grid — pairs of <td> cells wrapped in <tr>s
    rows_2col = ""
    for i in range(0, len(included), 2):
        pair = included[i:i + 2]
        rows_2col += '<tr valign="top">'
        for j, a in enumerate(pair):
            rows_2col += _render_article(art_html, a, i + j + 1)
        if len(pair) == 1:
            rows_2col += '<td style="width:50%;padding:6px;"></td>'
        rows_2col += "</tr>"
        if i + 2 < len(included):
            rows_2col += '<tr><td colspan="2" style="height:6px;"></td></tr>'

    html = template["html"]
    html = html.replace("{articles}", rows_1col)
    html = html.replace("{articles_2col}", rows_2col)
    html = html.replace("{date}", newsletter["date"])
    html = html.replace("{unsubscribe_url}", unsubscribe_url)
    return html


def _render_text(newsletter: dict, articles: list[dict], unsubscribe_url: str = "") -> str:
    lines = [f"SecDigest — {newsletter['date']}", "=" * 40, ""]
    for i, a in enumerate(articles):
        if not a.get("included", 1):
            continue
        lines += [
            f"{i+1}. {a['title']}",
            f"   {a.get('url') or a.get('hn_url', '')}",
            f"   {a.get('summary', 'No summary.')}",
        ]
        if a.get("hn_url"):
            lines.append(f"   HN: {a['hn_url']} ({a.get('hn_score', 0)} pts)")
        lines.append("")
    if unsubscribe_url:
        lines += ["", f"Unsubscribe: {unsubscribe_url}"]
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

    subject_override = db.newsletter_get_subject(newsletter["id"])
    template = db.email_template_get(db.newsletter_get_template_id(newsletter["id"]) or 0)
    default_subject = template["subject"] if template else "SecDigest — {date}"
    subject = (subject_override or default_subject).replace("{date}", date_str)

    smtp_from = cfg.get("smtp_from", "SecDigest <noreply@example.com>")
    base_url = cfg.get("base_url", "http://localhost:8000").rstrip("/")

    sent, errors = 0, []
    try:
        with smtplib.SMTP(smtp_host, int(cfg.get("smtp_port", 587))) as server:
            server.starttls()
            if cfg.get("smtp_user"):
                server.login(cfg["smtp_user"], cfg.get("smtp_pass", ""))
            for sub in subscribers:
                token = sub.get("unsubscribe_token", "")
                unsub_url = f"{base_url}/unsubscribe/{token}" if token else ""
                html_body = render_email_html(newsletter, articles, unsubscribe_url=unsub_url)
                text_body = _render_text(newsletter, articles, unsubscribe_url=unsub_url)
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

"""Send newsletter emails via SMTP."""
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from secdigest import db


def _render_toc(included: list[dict], is_2col: bool = False) -> str:
    """Return a table-of-contents <tr> block with anchor links to each article."""
    items = "".join(
        f'<div style="margin-bottom:5px;">'
        f'<a href="#article-{i+1}" style="color:#58a6ff;text-decoration:none;font-size:.85em;line-height:1.4;">'
        f'<span style="font-family:monospace;color:#6e7681;margin-right:6px;">#{i+1}</span>'
        f'{a.get("title", "")}'
        f'</a></div>'
        for i, a in enumerate(included)
    )
    col = ' colspan="2"' if is_2col else ''
    return (
        f'<tr><td{col} style="padding:14px 0 20px;border-bottom:1px solid #30363d;margin-bottom:4px;">'
        f'<div style="font-size:.7em;text-transform:uppercase;letter-spacing:.08em;'
        f'color:#6e7681;font-family:monospace;margin-bottom:10px;">Contents</div>'
        f'{items}'
        f'</td></tr>'
        f'<tr><td{col} style="height:8px;"></td></tr>'
    )


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
                      unsubscribe_url: str = "",
                      include_toc: bool = False) -> str:
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
    is_2col = "{articles_2col}" in template["html"]

    # Standard single-column list
    rows_1col = ""
    for i, a in enumerate(included):
        rendered = _render_article(art_html, a, i + 1)
        if include_toc:
            rendered = rendered.replace("<tr", f'<tr id="article-{i+1}"', 1)
        rows_1col += rendered

    # 2-column grid — pairs of <td> cells wrapped in <tr>s
    rows_2col = ""
    n = 0
    for i in range(0, len(included), 2):
        pair = included[i:i + 2]
        rows_2col += '<tr valign="top">'
        for j, a in enumerate(pair):
            n += 1
            rendered = _render_article(art_html, a, n)
            if include_toc:
                rendered = rendered.replace("<td", f'<td id="article-{n}"', 1)
            rows_2col += rendered
        if len(pair) == 1:
            rows_2col += '<td style="width:50%;padding:6px;"></td>'
        rows_2col += "</tr>"
        if i + 2 < len(included):
            rows_2col += '<tr><td colspan="2" style="height:6px;"></td></tr>'

    if include_toc and included:
        toc = _render_toc(included, is_2col=is_2col)
        rows_1col = toc + rows_1col
        rows_2col = toc + rows_2col

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
    include_toc = db.newsletter_get_toc(newsletter["id"])

    port = int(cfg.get("smtp_port", 587))
    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_pass", "")
    tls_context = ssl.create_default_context()

    sent, errors = 0, []
    try:
        if port == 465:
            _server = smtplib.SMTP_SSL(smtp_host, port, context=tls_context)
        else:
            _server = smtplib.SMTP(smtp_host, port)

        with _server as server:
            server.ehlo()
            if port != 465:
                server.starttls(context=tls_context)
                server.ehlo()
            if smtp_user:
                server.login(smtp_user, smtp_pass)
            for sub in subscribers:
                token = sub.get("unsubscribe_token", "")
                unsub_url = f"{base_url}/unsubscribe/{token}" if token else ""
                html_body = render_email_html(newsletter, articles, unsubscribe_url=unsub_url, include_toc=include_toc)
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

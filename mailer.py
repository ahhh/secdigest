"""Send newsletter emails via SMTP."""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import db


def _render_html(newsletter: dict, articles: list[dict]) -> str:
    date_str = newsletter["date"]
    rows = ""
    for i, a in enumerate(articles):
        if not a.get("included", 1):
            continue
        summary = a.get("summary") or "<em>No summary generated.</em>"
        url = a.get("url") or a.get("hn_url", "#")
        hn_url = a.get("hn_url", "#")
        rows += f"""
        <tr>
          <td style="padding:16px 0; border-bottom:1px solid #21262d;">
            <div style="font-size:0.75em; color:#6e7681; margin-bottom:4px;">
              #{i+1} &nbsp;·&nbsp; HN score: {a.get('hn_score',0)}
              &nbsp;·&nbsp; {a.get('hn_comments',0)} comments
            </div>
            <a href="{url}" style="color:#58a6ff; font-size:1.05em; font-weight:600;
               text-decoration:none;">{a['title']}</a>
            <p style="color:#c9d1d9; margin:8px 0 4px 0; font-size:0.9em;
               line-height:1.5;">{summary}</p>
            <a href="{hn_url}" style="color:#6e7681; font-size:0.8em;">HN discussion →</a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0"
       style="max-width:680px;padding:24px 16px;">
  <tr>
    <td style="padding-bottom:24px;border-bottom:2px solid #39ff14;">
      <span style="font-family:monospace;font-size:1.6em;
            font-weight:700;color:#39ff14;">SecDigest</span>
      <span style="color:#6e7681;margin-left:12px;font-size:0.9em;">{date_str}</span>
    </td>
  </tr>
  {rows}
  <tr>
    <td style="padding-top:24px;font-size:0.75em;color:#6e7681;">
      You're receiving this because you subscribed to SecDigest.
    </td>
  </tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def _render_text(newsletter: dict, articles: list[dict]) -> str:
    lines = [f"SecDigest — {newsletter['date']}", "=" * 40, ""]
    for i, a in enumerate(articles):
        if not a.get("included", 1):
            continue
        lines.append(f"{i+1}. {a['title']}")
        lines.append(f"   {a.get('url', a.get('hn_url', ''))}")
        if a.get("summary"):
            lines.append(f"   {a['summary']}")
        lines.append(f"   HN: {a.get('hn_url', '')} ({a.get('hn_score',0)} pts)")
        lines.append("")
    return "\n".join(lines)


def send_newsletter(date_str: str) -> tuple[bool, str]:
    """Send newsletter to all active subscribers. Returns (success, message)."""
    newsletter = db.newsletter_get(date_str)
    if not newsletter:
        return False, f"No newsletter found for {date_str}"

    articles = [a for a in db.article_list(newsletter["id"]) if a.get("included", 1)]
    if not articles:
        return False, "No included articles to send"

    subscribers = db.subscriber_active()
    if not subscribers:
        return False, "No active subscribers"

    cfg = db.cfg_all()
    smtp_host = cfg.get("smtp_host", "")
    smtp_port = int(cfg.get("smtp_port", 587))
    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_pass", "")
    smtp_from = cfg.get("smtp_from", "SecDigest <noreply@example.com>")

    if not smtp_host:
        return False, "SMTP not configured — set smtp_host in Settings"

    html_body = _render_html(newsletter, articles)
    text_body = _render_text(newsletter, articles)

    sent = 0
    errors = []
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_pass)

            for sub in subscribers:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = f"SecDigest — {date_str}"
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

    if errors:
        return True, f"Sent to {sent} subscribers. Errors: {'; '.join(errors)}"
    return True, f"Sent to {sent} subscribers"

"""Send newsletter emails via SMTP."""
import html
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from secdigest import db, crypto


def _sanitize_header(value: str) -> str:
    """Strip CR/LF from any string that's about to land in an SMTP header.

    SMTP headers are CRLF-terminated; smuggling \\r\\n into a Subject or From
    header lets an attacker inject arbitrary headers (BCC themselves, change
    Reply-To, etc.). We apply this at one boundary — right before composing
    the MIME message — so callers don't have to remember to strip after every
    intermediate mutation (template-substitution, decryption, etc.)."""
    if value is None:
        return ""
    return str(value).replace("\r", "").replace("\n", "")


def _smtp_send(to_email: str, subject: str, html_body: str, text_body: str) -> tuple[bool, str]:
    """Internal: open a single SMTP connection and send one message. Used by all the
    higher-level send_* helpers below for one-off transactional mail."""
    cfg = db.cfg_all()
    smtp_host = cfg.get("smtp_host", "")
    if not smtp_host:
        return False, "SMTP not configured"
    smtp_from = cfg.get("smtp_from", "SecDigest <noreply@example.com>")
    if "example.com" in smtp_from:
        return False, "From address not configured"

    to_email = _sanitize_header(to_email).strip()
    if not to_email or "@" not in to_email:
        return False, "Invalid recipient"
    subject = _sanitize_header(subject)
    smtp_from = _sanitize_header(smtp_from)

    port = int(cfg.get("smtp_port", 587))
    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_pass", "")
    tls_context = ssl.create_default_context()
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
                server.login(smtp_user, crypto.decrypt(smtp_pass))
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = smtp_from
            msg["To"] = to_email
            msg.attach(MIMEText(text_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))
            server.send_message(msg)
    except Exception as e:
        return False, f"SMTP error: {e}"
    return True, "ok"


def send_confirmation_email(to_email: str, confirm_url: str) -> tuple[bool, str]:
    """Send a double-opt-in confirmation email with a single confirm link."""
    safe_url = html.escape(confirm_url, quote=True)
    safe_url_text = confirm_url  # plain-text version intentionally unescaped
    html_body = (
        f'<!DOCTYPE html><html><body style="margin:0;padding:40px;background:#f6f8fa;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#1f2328;">'
        f'<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:10px;'
        f'padding:32px;box-shadow:0 1px 3px rgba(0,0,0,.06);">'
        f'<h1 style="margin:0 0 12px;font-size:20px;color:#0969da;">Confirm your subscription</h1>'
        f'<p style="line-height:1.6;">Click the link below to confirm your SecDigest subscription. '
        f'If you didn\'t request this, just ignore the message.</p>'
        f'<p style="margin:24px 0;">'
        f'<a href="{safe_url}" style="display:inline-block;padding:12px 22px;background:#0969da;'
        f'color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">Confirm subscription</a>'
        f'</p>'
        f'<p style="font-size:13px;color:#6e7781;line-height:1.6;">Or copy this link into your browser:<br>'
        f'<span style="font-family:monospace;word-break:break-all;">{safe_url}</span></p>'
        f'</div></body></html>'
    )
    text_body = (
        "Confirm your SecDigest subscription\n\n"
        "Click the link below to confirm. If you didn't request this, just ignore this message.\n\n"
        f"{safe_url_text}\n"
    )
    return _smtp_send(to_email, "Confirm your SecDigest subscription", html_body, text_body)


def _render_toc(included: list[dict], is_2col: bool = False) -> str:
    """Return a table-of-contents <tr> block with anchor links to each article."""
    items = "".join(
        f'<div style="margin-bottom:5px;">'
        f'<a href="#article-{i+1}" style="color:#58a6ff;text-decoration:none;font-size:.85em;line-height:1.4;">'
        f'<span style="font-family:monospace;color:#6e7681;margin-right:6px;">#{i+1}</span>'
        f'{html.escape(a.get("title", ""), quote=True)}'
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
    raw_url = a.get("url") or a.get("hn_url") or ""
    raw_hn_url = a.get("hn_url") or ""
    # Only allow http(s) URLs in href positions
    safe_url = raw_url if raw_url.startswith(("http://", "https://")) else ""
    safe_hn_url = raw_hn_url if raw_hn_url.startswith(("http://", "https://")) else ""

    title = html.escape(a.get("title", ""), quote=True)
    summary = html.escape(a.get("summary") or "No summary generated.", quote=True)
    safe_url_attr = html.escape(safe_url, quote=True)
    safe_hn_url_attr = html.escape(safe_hn_url, quote=True)

    s = article_html
    s = s.replace("{number}", str(n))
    s = s.replace("{title}", title)
    s = s.replace("{url}", safe_url_attr)
    s = s.replace("{hn_url}", safe_hn_url_attr)
    s = s.replace("{summary}", summary)
    s = s.replace("{hn_score}", str(a.get("hn_score", 0)))
    s = s.replace("{hn_comments}", str(a.get("hn_comments", 0)))
    return s


def _render_feedback_block(signal_url: str, noise_url: str) -> str:
    """Two inline-styled buttons that render in HTML mail clients without external
    CSS. The link targets are GET endpoints — clients like Gmail won't honour
    POST-from-email, and one-click GET is the convention for this kind of
    feedback widget. The dark/light contrast was picked to read on every
    built-in template footer (dark backgrounds + light backgrounds both)."""
    safe_signal = html.escape(signal_url, quote=True)
    safe_noise = html.escape(noise_url, quote=True)
    return (
        '<div style="text-align:center;padding:18px 0 12px;">'
        '<div style="font-size:.78em;color:#8b949e;margin-bottom:10px;'
        'letter-spacing:.04em;text-transform:uppercase;">how was this issue?</div>'
        f'<a href="{safe_signal}" '
        'style="display:inline-block;padding:8px 18px;margin:0 6px;'
        'border-radius:6px;background:#238636;color:#ffffff;'
        'text-decoration:none;font-weight:600;font-size:.9em;">'
        '&#x1F44D; signal</a>'
        f'<a href="{safe_noise}" '
        'style="display:inline-block;padding:8px 18px;margin:0 6px;'
        'border-radius:6px;background:#6e7681;color:#ffffff;'
        'text-decoration:none;font-weight:600;font-size:.9em;">'
        '&#x1F44E; noise</a>'
        '</div>'
    )


def render_email_html(newsletter: dict, articles: list[dict],
                      template_id: int | None = None,
                      unsubscribe_url: str = "",
                      include_toc: bool = False,
                      feedback_block: str = "") -> str:
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

    body = template["html"]
    body = body.replace("{articles}", rows_1col)
    body = body.replace("{articles_2col}", rows_2col)
    body = body.replace("{date}", newsletter["date"])
    body = body.replace("{unsubscribe_url}", unsubscribe_url)
    body = body.replace("{feedback_block}", feedback_block)
    return body


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


def _load_for_send(date_str: str, kind: str) -> tuple[dict | None, list[dict]]:
    """Resolve the newsletter row + the article list to render, kind-aware.
    Daily newsletters own their articles directly; weekly/monthly digests pull from
    the digest_articles join."""
    newsletter = db.newsletter_get(date_str, kind=kind)
    if not newsletter:
        return None, []
    if kind == "daily":
        articles = db.article_list(newsletter["id"])
    else:
        articles = db.digest_article_list(newsletter["id"])
    return newsletter, articles


def _default_subject_for(kind: str) -> str:
    if kind == "weekly":
        return "SecDigest Weekly — {date}"
    if kind == "monthly":
        return "SecDigest Monthly — {date}"
    return "SecDigest — {date}"


def send_test_email(date_str: str, recipient: str, kind: str = "daily") -> tuple[bool, str]:
    """Send a single test copy of the newsletter to one address.
    Output is identical to a production send (same template, subject, and TOC setting),
    except the unsubscribe link uses a non-DB token so it shows as invalid if clicked."""
    newsletter, articles = _load_for_send(date_str, kind)
    if not newsletter:
        return False, f"No {kind} newsletter found for {date_str}"

    if not any(a.get("included", 1) for a in articles):
        return False, "No included articles to send"

    recipient = _sanitize_header(recipient).strip()
    if not recipient or "@" not in recipient:
        return False, "Invalid recipient email"

    cfg = db.cfg_all()
    smtp_host = cfg.get("smtp_host", "")
    if not smtp_host:
        return False, "SMTP not configured — set smtp_host in Settings"

    subject_override = db.newsletter_get_subject(newsletter["id"])
    template = db.email_template_get(db.newsletter_get_template_id(newsletter["id"]) or 0)
    default_subject = template["subject"] if template else _default_subject_for(kind)
    subject = (subject_override or default_subject).replace("{date}", date_str)

    smtp_from = cfg.get("smtp_from", "SecDigest <noreply@example.com>")
    if "example.com" in smtp_from:
        return False, "From address is not configured (still using example.com)"
    # Single sanitisation point — applies after the {date} substitution above so
    # CRLF in either the subject template or the override gets stripped.
    subject = _sanitize_header(subject)
    smtp_from = _sanitize_header(smtp_from)

    base_url = cfg.get("base_url", "http://localhost:8000").rstrip("/")
    include_toc = db.newsletter_get_toc(newsletter["id"])
    unsub_url = f"{base_url}/unsubscribe/test-preview"

    fb_block = ""
    if cfg.get("feedback_enabled", "1") == "1":
        # 'test-preview' is intentionally not a real subscriber token, so
        # clicking the buttons in a test email lands on a friendly "invalid
        # link" page rather than recording a vote against a real subscriber.
        fb_block = _render_feedback_block(
            f"{base_url}/feedback/test-preview/{newsletter['id']}/signal",
            f"{base_url}/feedback/test-preview/{newsletter['id']}/noise",
        )

    html_body = render_email_html(newsletter, articles, unsubscribe_url=unsub_url,
                                  include_toc=include_toc, feedback_block=fb_block)
    text_body = _render_text(newsletter, articles, unsubscribe_url=unsub_url)

    port = int(cfg.get("smtp_port", 587))
    smtp_user = cfg.get("smtp_user", "")
    smtp_pass = cfg.get("smtp_pass", "")
    tls_context = ssl.create_default_context()

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
                server.login(smtp_user, crypto.decrypt(smtp_pass))

            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = smtp_from
            msg["To"] = recipient
            msg.attach(MIMEText(text_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))
            server.send_message(msg)
    except Exception as e:
        return False, f"SMTP error: {e}"

    return True, f"Test email sent to {recipient}"


def send_newsletter(date_str: str, kind: str = "daily") -> tuple[bool, str]:
    """Send the newsletter for date_str to active subscribers whose cadence matches kind."""
    newsletter, articles = _load_for_send(date_str, kind)
    if not newsletter:
        return False, f"No {kind} newsletter found for {date_str}"

    if not any(a.get("included", 1) for a in articles):
        return False, "No included articles to send"

    subscribers = db.subscriber_active(cadence=kind)
    if not subscribers:
        return False, f"No active {kind} subscribers"

    cfg = db.cfg_all()
    smtp_host = cfg.get("smtp_host", "")
    if not smtp_host:
        return False, "SMTP not configured — set smtp_host in Settings"

    subject_override = db.newsletter_get_subject(newsletter["id"])
    template = db.email_template_get(db.newsletter_get_template_id(newsletter["id"]) or 0)
    default_subject = template["subject"] if template else _default_subject_for(kind)
    subject = (subject_override or default_subject).replace("{date}", date_str)

    smtp_from = cfg.get("smtp_from", "SecDigest <noreply@example.com>")
    if "example.com" in smtp_from:
        return False, "From address is not configured (still using example.com)"
    # Single sanitisation boundary — applies after every mutation above (template
    # substitution, override fallback) so CRLF is stripped from the final value.
    subject = _sanitize_header(subject)
    smtp_from = _sanitize_header(smtp_from)
    base_url = cfg.get("base_url", "http://localhost:8000").rstrip("/")
    include_toc = db.newsletter_get_toc(newsletter["id"])
    feedback_on = cfg.get("feedback_enabled", "1") == "1"

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
                server.login(smtp_user, crypto.decrypt(smtp_pass))
            for sub in subscribers:
                token = sub.get("unsubscribe_token", "")
                unsub_url = f"{base_url}/unsubscribe/{token}" if token else ""
                # The unsubscribe_token doubles as the feedback-recording
                # identity: both arrive in the user's verified inbox, so
                # treating them as the same trust anchor keeps the URL space
                # tidy and avoids minting a parallel token.
                fb_block = ""
                if feedback_on and token:
                    fb_block = _render_feedback_block(
                        f"{base_url}/feedback/{token}/{newsletter['id']}/signal",
                        f"{base_url}/feedback/{token}/{newsletter['id']}/noise",
                    )
                html_body = render_email_html(newsletter, articles, unsubscribe_url=unsub_url,
                                              include_toc=include_toc, feedback_block=fb_block)
                text_body = _render_text(newsletter, articles, unsubscribe_url=unsub_url)
                to_email = _sanitize_header(sub["email"]).strip()
                if not to_email or "@" not in to_email:
                    errors.append(f"{sub['email']}: invalid email")
                    continue
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = smtp_from
                msg["To"] = to_email
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

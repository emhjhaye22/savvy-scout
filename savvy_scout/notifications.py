"""SMTP email sending for dashboard account invites. Same SMTP-over-Graph
approach as the sibling app (new-app/notifications.py) -- Microsoft Graph
needs an Azure AD app registration that was never completed for this app
(see graph/mail.py), so invite emails go out over plain SMTP instead, using
whatever mailbox SMTP_* points at.
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()

# Shown as the sender's display name in the recipient's mail client instead
# of the raw SMTP mailbox address (2026-08-09) -- Outlook was showing
# "emhjhaye22@gmail.com" as the sender, which reads as a random personal
# account rather than the app.
SENDER_DISPLAY_NAME = "TenderSight™"

URGENT_DAYS = 3
APPROACHING_DAYS = 7


def _deadline_urgency(deadline: str | None) -> str | None:
    """A short urgency label for a deadline, or None if it's not close (or
    unknown/already passed -- nothing useful to flag either way)."""
    if not deadline:
        return None
    try:
        dt = datetime.fromisoformat(deadline)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days_left = (dt - datetime.now(timezone.utc)).days
    if days_left < 0:
        return None
    if days_left <= URGENT_DAYS:
        return f"🔴 URGENT -- {days_left} day(s) left"
    if days_left <= APPROACHING_DAYS:
        return f"🟡 Approaching -- {days_left} day(s) left"
    return None


class NotificationError(RuntimeError):
    """Raised when an email can't be sent. Never includes SMTP_PASSWORD (or
    any other credential) in the message."""


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise NotificationError(
            f"Missing required environment variable: {name}. Set it in your .env file."
        )
    return value


def send_email(to_address: str, subject: str, body: str, html_body: str | None = None) -> None:
    """Send an email via SMTP: plain text, or plain text with an HTML
    alternative when html_body is given (a real multipart/alternative
    message, not HTML-only -- keeps a plain-text fallback for clients that
    prefer it, and avoids the HTML-only signal some spam filters weight).
    Raises NotificationError on failure."""
    _send_email_message(to_address, subject, body, attachment_path=None, html_body=html_body)


def send_email_with_attachment(
    to_address: str, subject: str, body: str, attachment_path: str
) -> None:
    """Same as send_email, plus one file attachment -- used for the Weekly/
    Monthly Trifork reports (2026-08-15). Enforces the same @bidsavvy.io-only
    whitelist as graph/mail.py's assert_whitelisted (SPEC.md non-negotiable
    2): these reports are prepared for Trifork's leadership team, but the
    tool itself only ever delivers them to a Bid Savvy inbox -- a person
    decides when/how to forward to Trifork, the same trust boundary as the
    Internal Addendum vs. the client-facing Capture Brief."""
    domain = to_address.rsplit("@", 1)[-1].lower() if "@" in to_address else ""
    if domain != "bidsavvy.io":
        raise NotificationError(
            f"Refusing to send to {to_address}: outbound email is whitelisted to "
            f"@bidsavvy.io addresses only (SPEC.md non-negotiable 2)."
        )
    _send_email_message(to_address, subject, body, attachment_path=attachment_path)


def _send_email_message(
    to_address: str, subject: str, body: str, attachment_path: str | None, html_body: str | None = None
) -> None:
    host = _require_env("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() not in ("false", "0", "no")
    sender = _require_env("NOTIFICATION_SENDER_EMAIL")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{SENDER_DISPLAY_NAME} <{sender}>"
    message["To"] = to_address
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    if attachment_path:
        from pathlib import Path

        file_bytes = Path(attachment_path).read_bytes()
        message.add_attachment(
            file_bytes,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=Path(attachment_path).name,
        )

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_tls:
                smtp.starttls()
            if username:
                smtp.login(username, password or "")
            smtp.send_message(message)
    except smtplib.SMTPException as exc:
        raise NotificationError(f"Failed to send email: {exc}") from exc
    except OSError as exc:
        raise NotificationError(f"Could not reach the SMTP server: {exc}") from exc


def send_account_invite_email(
    to_address: str, display_name: str, app_url: str, login_identifier: str, temp_password: str
) -> None:
    """Sent whenever someone needs real, working credentials in their inbox:
    a brand-new account, the first time an email is set on one of the
    originally-seeded accounts, a password reset, or a manual "resend the
    invite" click. Always includes the actual username/temp password --
    2026-08-09, previously the "first email set on an existing account"
    path sent a bare link with no credentials ("log in with your usual
    username/password"), which was useless to someone who never had a
    reason to know their own seeded login before receiving any email at all.

    Written as plain prose, not a "Log in with: / Email: / Password:"
    bulleted list (2026-08-09) -- that exact layout is the textbook shape
    Microsoft 365's anti-phishing filters are built to catch on mail from an
    unfamiliar external sender, and was landing in quarantine rather than
    the inbox. This can't guarantee a filter won't still flag it (that's
    ultimately about sender reputation, not wording), but it removes the
    most obvious template signal."""
    body = (
        f"Hi {display_name},\n\n"
        f"Mark has set you up with access to TenderSight™, the tender-scouting dashboard the Bid Savvy "
        f"team uses to track and triage procurement opportunities.\n\n"
        f"You can sign in at {app_url} using {login_identifier} and the temporary password "
        f"{temp_password}. Once you're in, you can set your own password from the \"Change Password\" "
        f"link in the sidebar.\n\n"
        "If anything doesn't work, just message Mark directly.\n"
    )
    send_email(to_address, "Your TenderSight™ access from Mark", body)


def send_new_opportunity_email(
    to_address: str, display_name: str, ref: str, title: str, buyer: str | None,
    deadline: str | None, app_url: str, notice_id: int,
) -> None:
    """Sent to a sector owner the first time a new notice is triaged and
    assigned to them (2026-08-09), so an opportunity doesn't just sit
    unnoticed in the in-app "needs attention" badge until they next open the
    dashboard."""
    urgency = _deadline_urgency(deadline)
    subject = f"New opportunity: {title}"
    if urgency:
        subject = f"[{urgency.split(' -- ')[0]}] {subject}"
    lines = [
        f"Hi {display_name},",
        "",
        "A new opportunity has been assigned to you on TenderSight™:",
        "",
        f"  {title}",
        f"  Buyer: {buyer or 'Unknown'}",
        f"  Reference: {ref}",
        f"  Deadline: {deadline or 'Not stated'}",
    ]
    if urgency:
        lines.append(f"  {urgency}")
    lines.extend(["", f"View it here: {app_url}/notices/{notice_id}" if app_url else f"View it here: /notices/{notice_id}"])
    send_email(to_address, subject, "\n".join(lines))


def send_victoria_escalation_email(
    to_address: str, notice_id: int, ref: str, title: str, buyer: str | None,
    sector: str | None, owner: str | None, indicative_value: str | None, deadline: str | None,
    overall_rating: str | None, overall_reasoning: str | None, trigger_reason: str, app_url: str,
) -> None:
    """Sent the moment a notice reaches ESCALATED_TO_VICTORIA (2026-08-09) --
    previously the only route to notify Victoria was the manual "Send Brief
    Email" button, which needs Microsoft Graph configured (it isn't). Full
    detail here so Victoria can make a go/no-go call from the email alone if
    needed, with an urgency flag since escalations near their deadline need
    a faster decision than ones with weeks to spare."""
    urgency = _deadline_urgency(deadline)
    subject = f"Escalation for your decision: {title}"
    if urgency:
        subject = f"[{urgency.split(' -- ')[0]}] {subject}"
    lines = [
        "Hi Victoria,",
        "",
        "A notice has been escalated to you for a go/no-go/park decision:",
        "",
        f"  {title}",
        f"  Buyer: {buyer or 'Unknown'}",
        f"  Sector: {sector or 'UNVERIFIED'}",
        f"  Owner: {owner or 'Unassigned'}",
        f"  Reference: {ref}",
        f"  Indicative value: {indicative_value or 'UNVERIFIED'}",
        f"  Deadline: {deadline or 'Not stated'}",
    ]
    if urgency:
        lines.append(f"  {urgency}")
    if overall_rating:
        lines.append(f"  Phase 2 AI overall rating: {overall_rating} -- {overall_reasoning or ''} (PROVISIONAL, FOR VALIDATION)")
    lines.extend([
        f"  Escalation reason: {trigger_reason}",
        "",
        f"Review and decide here: {app_url}/notices/{notice_id}" if app_url else f"Review and decide here: /notices/{notice_id}",
        "The full Internal Addendum (triage gates, capability fit, open questions) is available there too.",
    ])
    send_email(to_address, subject, "\n".join(lines))


def _item_link(item: dict, app_url: str) -> str | None:
    if app_url and item.get("notice_id"):
        return f"{app_url}/notices/{item['notice_id']}"
    return None


def _reminder_lines(item: dict, app_url: str) -> list[str]:
    lines = [f"  {item['title']}"]
    link = _item_link(item, app_url)
    if link:
        lines.append(f"  Link: {link}")
    lines.extend([
        f"  Buyer: {item.get('buyer') or 'Unknown'}",
        f"  Reference: {item['ref']}",
        f"  Value: {item.get('value') or 'Not stated'}",
        f"  Owner: {item.get('owner') or 'Unassigned'}",
    ])
    if item.get("deadline"):
        lines.append(f"  Deadline: {item['deadline']}")
    if item.get("why"):
        lines.append(f"  Why this needs a decision: {item['why']}")
    return lines


def _reminder_card_html(item: dict, app_url: str, accent: str) -> str:
    import html as _html

    link = _item_link(item, app_url)
    title = _html.escape(item["title"])
    title_html = f'<a href="{_html.escape(link)}" style="color:#1a4fa0;text-decoration:none;">{title}</a>' if link else title
    rows = [
        ("Buyer", item.get("buyer") or "Unknown"),
        ("Reference", item["ref"]),
        ("Value", item.get("value") or "Not stated"),
        ("Owner", item.get("owner") or "Unassigned"),
    ]
    if item.get("deadline"):
        rows.append(("Deadline", item["deadline"]))
    field_rows = "".join(
        f'<tr><td style="padding:2px 8px 2px 0;color:#666;font-size:13px;white-space:nowrap;">{_html.escape(str(label))}</td>'
        f'<td style="padding:2px 0;font-size:13px;">{_html.escape(str(value))}</td></tr>'
        for label, value in rows
    )
    why_html = (
        f'<p style="margin:8px 0 0;font-size:13px;color:#333;"><b>Why this needs a decision:</b> {_html.escape(item["why"])}</p>'
        if item.get("why") else ""
    )
    return (
        f'<div style="border-left:4px solid {accent};background:#fafafa;padding:10px 14px;margin-bottom:10px;border-radius:4px;">'
        f'<div style="font-size:15px;font-weight:600;margin-bottom:4px;">{title_html}</div>'
        f'<table style="border-collapse:collapse;">{field_rows}</table>'
        f'{why_html}'
        f'</div>'
    )


def send_victoria_reminder_digest_email(
    to_address: str, urgent_items: list[dict], high_value_items: list[dict], app_url: str,
) -> None:
    """Explicit request (2026-08-21): a daily digest of escalations still
    awaiting Victoria's go/no-go/park, split into two distinct reasons she
    needs to look, since a bare "reminder" without a reason to act on is
    easy to skim past:
    - urgent_items: deadline approaching, decision still outstanding.
    - high_value_items: HIGH capability fit or an outright PURSUE
      recommendation, still outstanding, regardless of deadline -- so a
      strong opportunity never quietly expires just because the clock
      wasn't the trigger.
    Each item carries its own "why" so the email explains the decision, not
    just names it. Sent as HTML with a plain-text fallback (2026-08-21,
    explicit feedback that a plain-text wall of words with no live link and
    no value was hard to act on) -- each opportunity is its own card with a
    clickable link straight to the notice and its indicative value shown.
    Callers should only call this when there is genuinely something in at
    least one list -- an empty digest is noise, not a reminder."""
    lines = [
        "Hi Victoria,",
        "",
        "The following escalations are still awaiting your go / no-go / park decision.",
    ]
    html_sections = []
    if urgent_items:
        lines.extend(["", "URGENT, DEADLINE APPROACHING", ""])
        for item in urgent_items:
            lines.extend(_reminder_lines(item, app_url))
            lines.append("")
        html_sections.append(
            '<h3 style="color:#af1f23;font-size:14px;margin:18px 0 8px;">URGENT, DEADLINE APPROACHING</h3>'
            + "".join(_reminder_card_html(item, app_url, "#af1f23") for item in urgent_items)
        )
    if high_value_items:
        lines.extend(["", "STRONG OPPORTUNITIES AWAITING YOUR DECISION", ""])
        for item in high_value_items:
            lines.extend(_reminder_lines(item, app_url))
            lines.append("")
        html_sections.append(
            '<h3 style="color:#1a4fa0;font-size:14px;margin:18px 0 8px;">STRONG OPPORTUNITIES AWAITING YOUR DECISION</h3>'
            + "".join(_reminder_card_html(item, app_url, "#1a4fa0") for item in high_value_items)
        )
    lines.append(f"Review outstanding escalations here: {app_url}" if app_url else "Review outstanding escalations in TenderSight™.")
    subject_bits = []
    if urgent_items:
        subject_bits.append(f"{len(urgent_items)} urgent")
    if high_value_items:
        subject_bits.append(f"{len(high_value_items)} high-value")
    subject = f"TenderSight™: {' and '.join(subject_bits)} escalation(s) awaiting your decision"

    footer_link = (
        f'<p style="font-size:13px;margin-top:16px;"><a href="{app_url}" style="color:#1a4fa0;">'
        f"Review outstanding escalations in TenderSight™</a></p>"
        if app_url else ""
    )
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;max-width:640px;">'
        "<p>Hi Victoria,</p>"
        "<p>The following escalations are still awaiting your go / no-go / park decision.</p>"
        + "".join(html_sections)
        + footer_link
        + "</div>"
    )
    send_email(to_address, subject, "\n".join(lines), html_body=html_body)


def send_new_opportunity_teams_message(
    webhook_url: str, display_name: str, ref: str, title: str, buyer: str | None,
    deadline: str | None, app_url: str, notice_id: int,
) -> None:
    """Posts the same new-opportunity alert to the owner's configured Teams
    incoming webhook (2026-08-09). Uses the legacy Office 365 Connector
    MessageCard format -- still the format Teams incoming webhooks accept,
    and needs no Azure AD app registration (unlike a real per-user Graph
    chat message, which this app has no permissions for)."""
    import requests

    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": f"New opportunity: {title}",
        "themeColor": "AF1F23",
        "title": f"New opportunity assigned to {display_name}",
        "text": (
            f"**{title}**\n\n"
            f"Buyer: {buyer or 'Unknown'}  \n"
            f"Reference: {ref}  \n"
            f"Deadline: {deadline or 'Not stated'}"
        ),
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name": "View in TenderSight™",
                "targets": [{"os": "default", "uri": f"{app_url}/notices/{notice_id}"}],
            }
        ] if app_url else [],
    }
    try:
        response = requests.post(webhook_url, json=card, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NotificationError(f"Failed to post Teams notification: {exc}") from exc

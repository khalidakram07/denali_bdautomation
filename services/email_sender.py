"""
services/email_sender.py — Gmail SMTP sender.

Reads `mailboxes.json` at the project root. Each entry is one Gmail account
that Maryam can send from. The frontend shows a dropdown of these.

How to add a real Gmail account:
  1. The account must have 2-Step Verification turned on.
  2. Generate an "App password" here (signed into THAT account):
        https://myaccount.google.com/apppasswords
  3. Paste the 16-char password into mailboxes.json under "app_password".

Dry-run mode:
  If `app_password` is missing or starts with "REPLACE", the send is faked —
  we log a warning and return a fake message-id. This lets the demo flow
  work end-to-end before real credentials are wired in.
"""

import json
import logging
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "mailboxes.json"


def _is_dry_run(mailbox: dict) -> bool:
    pw = (mailbox.get("app_password") or "").strip()
    return (not pw) or pw.upper().startswith("REPLACE")


def load_mailboxes() -> list[dict]:
    """Read mailboxes.json. Returns [] if the file is missing."""
    if not CONFIG_PATH.exists():
        log.warning("mailboxes.json not found at %s — no mailboxes configured", CONFIG_PATH)
        return []
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            log.error("mailboxes.json must be a JSON array")
            return []
        return data
    except json.JSONDecodeError as e:
        log.error("mailboxes.json is invalid JSON: %s", e)
        return []


def list_mailboxes_public() -> list[dict]:
    """
    Returns mailbox info safe to expose to the frontend (no passwords).
    Each item: {email, display_name, ready (bool — false if dry-run/missing pw)}
    """
    out = []
    for m in load_mailboxes():
        out.append({
            "email":        m.get("email"),
            "display_name": m.get("display_name") or m.get("email"),
            "ready":        not _is_dry_run(m),
        })
    return out


def find_mailbox(email: str) -> Optional[dict]:
    """Look up a mailbox by email. Returns None if not configured."""
    for m in load_mailboxes():
        if m.get("email", "").lower() == email.lower():
            return m
    return None


# ─────────────────────────────────────────────────────────────
# Send
# ─────────────────────────────────────────────────────────────

class SendResult:
    def __init__(self, message_id: str, dry_run: bool, sent_via: str):
        self.message_id = message_id
        self.dry_run    = dry_run
        self.sent_via   = sent_via   # the From address actually used


def send_email(
    from_mailbox_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    sender_display_name: Optional[str] = None,
) -> SendResult:
    """
    Send an email through the chosen Gmail mailbox.

    Raises ValueError if the mailbox isn't configured.
    Raises smtplib errors if the SMTP server rejects the send.
    Returns SendResult with the Message-ID (real or fake in dry-run).
    """
    mb = find_mailbox(from_mailbox_email)
    if not mb:
        raise ValueError(f"Mailbox '{from_mailbox_email}' is not in mailboxes.json")

    if not to_email:
        raise ValueError("Cannot send: contact has no email address")

    display = sender_display_name or mb.get("display_name") or mb["email"]
    message_id = make_msgid(domain=mb["email"].split("@")[-1])

    if _is_dry_run(mb):
        # Demo path — pretend we sent
        fake_id = f"<dryrun-{uuid.uuid4()}@denali.local>"
        log.warning(
            "DRY-RUN send: would have emailed %s from %s (subject=%r). "
            "Set a real app_password in mailboxes.json to actually send.",
            to_email, mb["email"], subject[:60],
        )
        return SendResult(message_id=fake_id, dry_run=True, sent_via=mb["email"])

    # Build the MIME message
    msg = EmailMessage()
    msg["From"]       = formataddr((display, mb["email"]))
    msg["To"]         = to_email
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    msg["Reply-To"]   = mb["email"]
    msg.set_content(body_text)

    host = mb.get("smtp_host", "smtp.gmail.com")
    port = int(mb.get("smtp_port", 587))

    log.info("Sending email via %s:%d as %s -> %s", host, port, mb["email"], to_email)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(mb["email"], mb["app_password"])
        server.send_message(msg)

    return SendResult(message_id=message_id, dry_run=False, sent_via=mb["email"])

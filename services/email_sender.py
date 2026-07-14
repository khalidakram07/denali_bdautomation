"""
services/email_sender.py - Gmail SMTP sender (with optional attachments).

In dev: reads mailboxes.json at the project root.
In production: reads the MAILBOXES_JSON env var (same JSON array format).

App passwords: account needs 2-Step Verification, then generate one at
https://myaccount.google.com/apppasswords

Dry-run mode: if app_password is missing or starts with "REPLACE", the send is
faked (logs a warning, returns a fake message-id) so the demo flow works
without real credentials.
"""

import json
import logging
import mimetypes
import os
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "mailboxes.json"

SCOPES_NOTE = "Gmail SMTP (smtp.gmail.com:587, STARTTLS)"


def _is_dry_run(mailbox: dict) -> bool:
    pw = (mailbox.get("app_password") or "").strip()
    return (not pw) or pw.upper().startswith("REPLACE")


def load_mailboxes() -> list[dict]:
    """Load mailbox configs: MAILBOXES_JSON env var first, then mailboxes.json file."""
    env_json = os.getenv("MAILBOXES_JSON", "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, list):
                return data
            log.error("MAILBOXES_JSON env var must be a JSON array")
        except json.JSONDecodeError as e:
            log.error("MAILBOXES_JSON env var is invalid JSON: %s", e)
        return []

    if not CONFIG_PATH.exists():
        log.warning("mailboxes.json not found at %s - no mailboxes configured", CONFIG_PATH)
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
    """Mailbox info safe for the frontend (no passwords)."""
    out = []
    for m in load_mailboxes():
        out.append({
            "email":        m.get("email"),
            "display_name": m.get("display_name") or m.get("email"),
            "ready":        not _is_dry_run(m),
        })
    return out


def find_mailbox(email: str) -> Optional[dict]:
    for m in load_mailboxes():
        if m.get("email", "").lower() == email.lower():
            return m
    return None


class SendResult:
    def __init__(self, message_id: str, dry_run: bool, sent_via: str, attachment_count: int = 0):
        self.message_id = message_id
        self.dry_run    = dry_run
        self.sent_via   = sent_via
        self.attachment_count = attachment_count


def _attach_files(msg: EmailMessage, attachments: list[tuple[str, bytes]]) -> int:
    """Attach (filename, bytes) tuples to the message. Returns count attached."""
    count = 0
    for fname, data in attachments or []:
        if not fname or data is None:
            continue
        ctype, _ = mimetypes.guess_type(fname)
        if ctype is None:
            maintype, subtype = "application", "octet-stream"
        else:
            maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
        count += 1
    return count


def _clean_email_list(raw) -> list[str]:
    """Normalize a CC/BCC input (str or list) into a list of clean addresses."""
    if not raw:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    # de-dupe while preserving order, drop anything without an '@'
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if not p or "@" not in p:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def send_email(
    from_mailbox_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    sender_display_name: Optional[str] = None,
    attachments: Optional[list[tuple[str, bytes]]] = None,
    cc_emails: Optional[list[str] | str] = None,
) -> SendResult:
    """
    Send an email through the chosen Gmail mailbox, optionally with attachments
    and CC recipients.

    attachments: list of (filename, raw_bytes) tuples.
    cc_emails:   list[str] or comma-separated str of CC addresses.
    Raises ValueError if the mailbox/recipient is missing.
    Raises smtplib errors if the SMTP server rejects the send.
    """
    mb = find_mailbox(from_mailbox_email)
    if not mb:
        raise ValueError(f"Mailbox '{from_mailbox_email}' is not configured")
    if not to_email:
        raise ValueError("Cannot send: contact has no email address")

    display = sender_display_name or mb.get("display_name") or mb["email"]
    message_id = make_msgid(domain=mb["email"].split("@")[-1])
    n_attach = len(attachments or [])
    cc_list = _clean_email_list(cc_emails)
    # Never CC the primary recipient (or the sender) — dedupe defensively.
    to_lower = (to_email or "").lower()
    from_lower = mb["email"].lower()
    cc_list = [c for c in cc_list if c.lower() not in (to_lower, from_lower)]

    if _is_dry_run(mb):
        fake_id = f"<dryrun-{uuid.uuid4()}@denali.local>"
        log.warning(
            "DRY-RUN send: would have emailed %s (cc=%s) from %s (subject=%r, attachments=%d). "
            "Set a real app_password to actually send.",
            to_email, cc_list, mb["email"], subject[:60], n_attach,
        )
        return SendResult(message_id=fake_id, dry_run=True, sent_via=mb["email"], attachment_count=n_attach)

    msg = EmailMessage()
    msg["From"]       = formataddr((display, mb["email"]))
    msg["To"]         = to_email
    if cc_list:
        msg["Cc"]     = ", ".join(cc_list)
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    msg["Reply-To"]   = mb["email"]
    msg.set_content(body_text)

    attached = _attach_files(msg, attachments or [])

    host = mb.get("smtp_host", "smtp.gmail.com")
    port = int(mb.get("smtp_port", 587))
    log.info("Sending via %s:%d as %s -> %s (cc=%s, %d attachments)",
             host, port, mb["email"], to_email, cc_list, attached)

    # send_message() honors To/Cc/Bcc headers as envelope recipients by default,
    # so CC'd addresses receive a real copy — no need to pass to_addrs explicitly.
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=60) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(mb["email"], mb["app_password"])
        server.send_message(msg)

    return SendResult(message_id=message_id, dry_run=False, sent_via=mb["email"], attachment_count=attached)

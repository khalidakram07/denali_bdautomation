"""
routers/campaigns.py — Aggregate views: activity feed, dashboard, sends, mailboxes.

Endpoints:
    GET    /api/campaigns/activity     Live activity log (newest first)
    GET    /api/campaigns/dashboard    Counts + funnel for the homepage
    GET    /api/campaigns/sends        List email_sends rows
    GET    /api/campaigns/mailboxes    Configured Gmail mailboxes (no passwords)
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Query

from database import db_cursor
from models import ActivityLogRead, EmailSendRead, SendStatus
from services.email_sender import list_mailboxes_public

log = logging.getLogger(__name__)
router = APIRouter()


def _row_to_activity(row) -> ActivityLogRead:
    d = dict(row)
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = None
    return ActivityLogRead.model_validate(d)


@router.get("/activity", response_model=list[ActivityLogRead])
def activity_feed(
    limit: int = Query(50, ge=1, le=500),
    since_id: Optional[int] = Query(None, description="Only return entries with id > since_id"),
):
    """
    Newest-first activity log. The frontend polls this for the live feed —
    pass `since_id` to avoid re-fetching seen entries.
    """
    sql = "SELECT * FROM activity_log"
    params: list = []
    if since_id is not None:
        sql += " WHERE id > ?"
        params.append(since_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_row_to_activity(r) for r in rows]


@router.get("/dashboard")
def dashboard():
    """Summary counts for the homepage."""
    with db_cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM opportunities GROUP BY status")
        opps_by_status = {r["status"]: r["n"] for r in cur.fetchall()}

        cur.execute("SELECT approval_status, COUNT(*) AS n FROM email_drafts GROUP BY approval_status")
        drafts_by_status = {r["approval_status"]: r["n"] for r in cur.fetchall()}

        cur.execute("SELECT COUNT(*) AS n FROM contacts")
        contact_count = cur.fetchone()["n"]

        cur.execute("SELECT COUNT(*) AS n FROM email_sends")
        send_count = cur.fetchone()["n"]

    return {
        "opportunities": {
            "total": sum(opps_by_status.values()),
            "by_status": opps_by_status,
        },
        "drafts": {
            "pending":  drafts_by_status.get("pending", 0),
            "approved": drafts_by_status.get("approved", 0),
            "rejected": drafts_by_status.get("rejected", 0),
        },
        "contacts": contact_count,
        "sends": send_count,
    }


@router.get("/sends", response_model=list[EmailSendRead])
def list_sends(
    send_status: Optional[SendStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
):
    """List email_sends rows, newest first."""
    sql = "SELECT * FROM email_sends"
    params: list = []
    if send_status:
        sql += " WHERE send_status = ?"
        params.append(send_status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [EmailSendRead.model_validate(dict(r)) for r in rows]


@router.get("/mailboxes")
def list_mailboxes():
    """
    Returns the Gmail mailboxes configured in mailboxes.json.
    Passwords are NEVER included in this response.

    Each item:
      - email:        the Gmail address
      - display_name: human-readable label for the dropdown
      - ready:        false if app_password is missing/placeholder (dry-run only)
    """
    return {"mailboxes": list_mailboxes_public()}

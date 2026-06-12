"""
routers/campaigns.py — Aggregate views: metrics, history, sends, mailboxes, templates.

Endpoints:
    GET    /api/campaigns/activity         Live activity log (newest first)
    GET    /api/campaigns/metrics          Tile metrics + per-category summary (Phase 2)
    GET    /api/campaigns/dashboard        (legacy) Counts from old DB tables
    GET    /api/campaigns/sends            List email_sends rows
    GET    /api/campaigns/sent-history     Filterable + searchable send history (Phase 2)
    GET    /api/campaigns/contact-history  All sends to a given email (Phase 2)
    GET    /api/campaigns/mailboxes        Configured Gmail mailboxes (no passwords)
    GET    /api/campaigns/templates        Drive + local .docx templates
    POST   /api/campaigns/templates/refresh  Bust the Drive templates cache
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query

from database import db_cursor
from models import ActivityLogRead, EmailSendRead, SendStatus
from services.email_sender import list_mailboxes_public
from services.template_engine import list_templates, refresh_templates_cache

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


# ─────────────────────────────────────────────────────────────
# /activity — unchanged
# ─────────────────────────────────────────────────────────────

@router.get("/activity", response_model=list[ActivityLogRead])
def activity_feed(
    limit: int = Query(50, ge=1, le=500),
    since_id: Optional[int] = Query(None, description="Only return entries with id > since_id"),
):
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


# ─────────────────────────────────────────────────────────────
# /metrics — Phase 2 dashboard tiles
# ─────────────────────────────────────────────────────────────

@router.get("/metrics")
def metrics():
    """
    Tile metrics for the dashboard plus per-category summary.

    Returns:
      sent_today, sent_this_week, sent_this_month, sent_total
      active_leads  (live count from category caches; None if no category cached yet)
      by_category   [{name, sent_this_week, sent_total}, ...]
    """
    now = datetime.utcnow()
    today_start    = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start     = today_start - timedelta(days=now.weekday())   # Mon 00:00
    month_start    = today_start.replace(day=1)

    def cnt(where_sql: str, params: list) -> int:
        with db_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM email_sends s {where_sql}", params)
            r = cur.fetchone()
            return r["n"] if r else 0

    base = "WHERE s.send_status = 'sent'"
    sent_today      = cnt(f"{base} AND s.sent_at >= ?", [today_start.isoformat(timespec='seconds')])
    sent_this_week  = cnt(f"{base} AND s.sent_at >= ?", [week_start.isoformat(timespec='seconds')])
    sent_this_month = cnt(f"{base} AND s.sent_at >= ?", [month_start.isoformat(timespec='seconds')])
    sent_total      = cnt(base, [])

    # Per-category counts via the draft snapshot
    by_category: list[dict] = []
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT d.category AS name,
                   COUNT(*) AS sent_total,
                   SUM(CASE WHEN s.sent_at >= ? THEN 1 ELSE 0 END) AS sent_this_week
            FROM email_sends s
            JOIN email_drafts d ON d.id = s.draft_id
            WHERE s.send_status = 'sent' AND d.category IS NOT NULL AND d.category <> ''
            GROUP BY d.category
            ORDER BY sent_total DESC
            """,
            [week_start.isoformat(timespec='seconds')],
        )
        for r in cur.fetchall():
            by_category.append({
                "name":           r["name"],
                "sent_total":     r["sent_total"],
                "sent_this_week": r["sent_this_week"] or 0,
            })

    # Active leads = sum of unsent contacts across whatever categories are warm
    # in the in-memory cache. None until at least one category has been loaded
    # (avoids a 30-second cold call from the dashboard).
    active_leads: Optional[int] = None
    try:
        from services.leads_categories import _category_data_cache
        if _category_data_cache:
            active_leads = sum(
                payload.get("contact_count", 0)
                for payload, _ts in _category_data_cache.values()
            )
    except Exception:
        active_leads = None

    return {
        "sent_today":      sent_today,
        "sent_this_week":  sent_this_week,
        "sent_this_month": sent_this_month,
        "sent_total":      sent_total,
        "active_leads":    active_leads,
        "by_category":     by_category,
        "as_of":           now.isoformat(timespec='seconds'),
    }


# ─────────────────────────────────────────────────────────────
# /dashboard — legacy (still used? returns 0s now in live-Sheets model)
# ─────────────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard():
    """Legacy. Live-Sheets model doesn't populate opportunities/contacts tables;
    those numbers will be 0. Prefer /api/campaigns/metrics."""
    with db_cursor() as cur:
        cur.execute("SELECT approval_status, COUNT(*) AS n FROM email_drafts GROUP BY approval_status")
        drafts_by_status = {r["approval_status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM email_sends")
        send_count = cur.fetchone()["n"]
    return {
        "drafts": {
            "pending":  drafts_by_status.get("pending", 0),
            "approved": drafts_by_status.get("approved", 0),
            "rejected": drafts_by_status.get("rejected", 0),
        },
        "sends": send_count,
    }


# ─────────────────────────────────────────────────────────────
# /sends — raw email_sends rows
# ─────────────────────────────────────────────────────────────

@router.get("/sends", response_model=list[EmailSendRead])
def list_sends(
    send_status: Optional[SendStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
):
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


# ─────────────────────────────────────────────────────────────
# /mailboxes
# ─────────────────────────────────────────────────────────────

@router.get("/mailboxes")
def list_mailboxes():
    return {"mailboxes": list_mailboxes_public()}


# ─────────────────────────────────────────────────────────────
# /templates + refresh
# ─────────────────────────────────────────────────────────────

@router.get("/templates")
def list_email_templates(refresh: bool = False):
    return {"templates": list_templates(force_refresh=refresh)}


@router.post("/templates/refresh")
def refresh_templates():
    cleared = refresh_templates_cache()
    return {"cleared": cleared, "templates": list_templates(force_refresh=True)}


# ─────────────────────────────────────────────────────────────
# /sent-history — Phase 2 filterable + searchable
# ─────────────────────────────────────────────────────────────

@router.get("/sent-history")
def sent_history(
    limit: int = Query(50, ge=1, le=500),
    mailbox: Optional[str] = Query(None, description="Filter to one sender mailbox"),
    send_status: Optional[SendStatus] = Query(None, alias="status"),
    category: Optional[str] = Query(None, description="Filter to one category"),
    date_from: Optional[str] = Query(None, description="ISO date or datetime, inclusive"),
    date_to: Optional[str] = Query(None, description="ISO date or datetime, exclusive"),
    search: Optional[str] = Query(None, description="Free-text across contact/company/trial/subject/recipient"),
):
    """
    Rich send history. Joins sends -> drafts (snapshot columns) so the UI gets
    everything in one call. All filters are optional; combine freely.
    """
    sql = """
        SELECT
            s.id                     AS send_id,
            s.draft_id,
            s.recipient_email,
            s.from_mailbox_email,
            s.is_to_overridden,
            s.sent_at,
            s.message_id,
            s.send_status,
            s.bounce_type,
            s.replied_at,
            d.subject_line           AS subject,
            d.body_text              AS body,
            d.approved_by,
            d.approved_at,
            d.category,
            d.trial_id,
            d.trial_title            AS opportunity_title,
            d.sponsor_name           AS sponsor_name,
            d.contact_name           AS contact_name,
            d.recipient_email        AS contact_stored_email,
            d.contact_title          AS contact_title
        FROM email_sends s
        JOIN email_drafts d ON d.id = s.draft_id
    """
    params: list = []
    where: list[str] = []

    if mailbox:
        where.append("s.from_mailbox_email = ?")
        params.append(mailbox)
    if send_status:
        where.append("s.send_status = ?")
        params.append(send_status)
    if category:
        where.append("d.category = ?")
        params.append(category)
    if date_from:
        where.append("s.sent_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("s.sent_at < ?")
        params.append(date_to)
    if search:
        like = f"%{search.strip()}%"
        where.append(
            "(d.contact_name LIKE ? OR d.sponsor_name LIKE ? OR d.trial_title LIKE ? "
            " OR d.subject_line LIKE ? OR s.recipient_email LIKE ?)"
        )
        params.extend([like, like, like, like, like])

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.id DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["is_to_overridden"] = bool(d.get("is_to_overridden"))
        out.append(d)
    return {"sends": out, "count": len(out)}


# ─────────────────────────────────────────────────────────────
# /contact-history — Phase 2: every send to one email
# ─────────────────────────────────────────────────────────────

@router.get("/contact-history")
def contact_history(
    email: str = Query(..., description="Recipient email address to look up"),
    limit: int = Query(100, ge=1, le=500),
):
    """All sends to this email (case-insensitive), newest first."""
    target = email.strip().lower()
    sql = """
        SELECT
            s.id                AS send_id,
            s.sent_at,
            s.recipient_email,
            s.from_mailbox_email,
            s.send_status,
            s.message_id,
            d.subject_line      AS subject,
            d.body_text         AS body,
            d.category,
            d.trial_id,
            d.trial_title       AS opportunity_title,
            d.sponsor_name      AS sponsor_name,
            d.contact_name      AS contact_name,
            d.contact_title     AS contact_title
        FROM email_sends s
        JOIN email_drafts d ON d.id = s.draft_id
        WHERE LOWER(s.recipient_email) = ?
        ORDER BY s.id DESC
        LIMIT ?
    """
    with db_cursor() as cur:
        cur.execute(sql, [target, limit])
        rows = cur.fetchall()
    sends = [dict(r) for r in rows]
    # Pull a representative contact name/title from the most recent send
    contact_name  = sends[0]["contact_name"]  if sends else None
    contact_title = sends[0]["contact_title"] if sends else None
    return {
        "email":         email,
        "contact_name":  contact_name,
        "contact_title": contact_title,
        "sends":         sends,
        "count":         len(sends),
    }


# ─────────────────────────────────────────────────────────────
# /by-mailbox — Phase 2.1: who sent how many
# ─────────────────────────────────────────────────────────────

@router.get("/by-mailbox")
def by_mailbox(days: int = Query(30, ge=1, le=3650)):
    """Per-mailbox send counts within the last `days`."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec='seconds')
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT s.from_mailbox_email AS mailbox,
                   COUNT(*) AS sent,
                   SUM(CASE WHEN s.send_status = 'replied' THEN 1 ELSE 0 END) AS replied,
                   SUM(CASE WHEN s.send_status = 'failed'  THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN s.send_status = 'bounced' THEN 1 ELSE 0 END) AS bounced
            FROM email_sends s
            WHERE s.sent_at >= ? AND s.from_mailbox_email IS NOT NULL
            GROUP BY s.from_mailbox_email
            ORDER BY sent DESC
            """,
            [cutoff],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"days": days, "by_mailbox": rows}


# ─────────────────────────────────────────────────────────────
# /top-recipients — Phase 2.1: most-emailed people
# ─────────────────────────────────────────────────────────────

@router.get("/top-recipients")
def top_recipients(days: int = Query(90, ge=1, le=3650), limit: int = Query(20, ge=1, le=100)):
    """People we've emailed most often in the last `days`, newest contact info."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec='seconds')
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT s.recipient_email AS email,
                   COUNT(*) AS sent,
                   MAX(s.sent_at) AS last_sent_at,
                   (SELECT d.contact_name  FROM email_drafts d JOIN email_sends s2 ON s2.draft_id=d.id
                     WHERE s2.recipient_email = s.recipient_email ORDER BY s2.id DESC LIMIT 1) AS contact_name,
                   (SELECT d.contact_title FROM email_drafts d JOIN email_sends s2 ON s2.draft_id=d.id
                     WHERE s2.recipient_email = s.recipient_email ORDER BY s2.id DESC LIMIT 1) AS contact_title,
                   (SELECT d.sponsor_name  FROM email_drafts d JOIN email_sends s2 ON s2.draft_id=d.id
                     WHERE s2.recipient_email = s.recipient_email ORDER BY s2.id DESC LIMIT 1) AS sponsor_name,
                   (SELECT d.category      FROM email_drafts d JOIN email_sends s2 ON s2.draft_id=d.id
                     WHERE s2.recipient_email = s.recipient_email ORDER BY s2.id DESC LIMIT 1) AS category
            FROM email_sends s
            WHERE s.sent_at >= ? AND s.recipient_email IS NOT NULL AND s.recipient_email <> ''
            GROUP BY s.recipient_email
            ORDER BY sent DESC, last_sent_at DESC
            LIMIT ?
            """,
            [cutoff, limit],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"days": days, "recipients": rows}


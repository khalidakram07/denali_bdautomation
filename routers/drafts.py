"""
routers/drafts.py — AI email drafting + the human approval queue.

Endpoints:
    GET    /api/drafts/                List drafts (filter by approval_status)
    POST   /api/drafts/generate        Generate a new draft via Anthropic
    GET    /api/drafts/{id}            Detail
    POST   /api/drafts/{id}/approve    Approve, optionally send via chosen mailbox
    POST   /api/drafts/{id}/reject     Reject with reason
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from database import db_cursor, log_activity
from models import (
    ApprovalStatus,
    DraftApprove,
    DraftGenerateRequest,
    DraftRead,
    DraftReject,
    DraftWithContext,
)
from services.ai_engine import PROMPT_VERSION, generate_draft
from services.email_sender import send_email, find_mailbox

log = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _row_to_draft_dict(row) -> dict:
    d = dict(row)
    if d.get("quality_flags"):
        try:
            d["quality_flags"] = json.loads(d["quality_flags"])
        except (json.JSONDecodeError, TypeError):
            d["quality_flags"] = None
    return d


def _row_to_draft(row) -> DraftRead:
    return DraftRead.model_validate(_row_to_draft_dict(row))


# ─────────────────────────────────────────────────────────────
# GET /api/drafts/
# ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[DraftWithContext])
def list_drafts(
    approval_status: Optional[ApprovalStatus] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    sql = """
        SELECT  d.*,
                o.trial_title                       AS opportunity_title,
                c.first_name || ' ' || c.last_name  AS contact_name,
                c.contact_score                     AS contact_score
        FROM    email_drafts d
        JOIN    opportunities o ON o.id = d.opportunity_id
        JOIN    contacts      c ON c.id = d.contact_id
    """
    params: list = []
    if approval_status:
        sql += " WHERE d.approval_status = ?"
        params.append(approval_status)
    sql += " ORDER BY d.created_at DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [DraftWithContext.model_validate(_row_to_draft_dict(r)) for r in rows]


# ─────────────────────────────────────────────────────────────
# POST /api/drafts/generate
# ─────────────────────────────────────────────────────────────

@router.post("/generate", status_code=status.HTTP_201_CREATED, response_model=DraftRead)
def generate(req: DraftGenerateRequest):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM opportunities WHERE id = ?", (req.opportunity_id,))
        opp_row = cur.fetchone()
        if not opp_row:
            raise HTTPException(404, f"Opportunity {req.opportunity_id} not found")
        cur.execute("SELECT * FROM contacts WHERE id = ?", (req.contact_id,))
        contact_row = cur.fetchone()
        if not contact_row:
            raise HTTPException(404, f"Contact {req.contact_id} not found")
        if contact_row["opportunity_id"] != req.opportunity_id:
            raise HTTPException(400, "Contact does not belong to this opportunity")

    opp = dict(opp_row)
    contact = dict(contact_row)
    if contact.get("score_reasoning"):
        try:
            contact["score_reasoning"] = json.loads(contact["score_reasoning"])
        except (json.JSONDecodeError, TypeError):
            contact["score_reasoning"] = None

    sender_name = os.getenv("DEFAULT_SENDER_NAME", "Maryam")

    try:
        draft = generate_draft(
            opp, contact,
            sender_name=sender_name,
            template_filename=req.template_filename,
        )
    except Exception as e:
        log.exception("AI generation failed for opp=%s contact=%s template=%s",
                      req.opportunity_id, req.contact_id, req.template_filename)
        raise HTTPException(502, f"AI generation failed: {e}")

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_drafts
                (opportunity_id, contact_id, sequence_step, subject_line, body_text,
                 prompt_version, quality_flags, approval_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                draft.opportunity_id, draft.contact_id, draft.sequence_step,
                draft.subject_line, draft.body_text,
                draft.prompt_version,
                json.dumps(draft.quality_flags) if draft.quality_flags else None,
            ),
        )
        draft_id = cur.lastrowid
        cur.execute(
            "UPDATE opportunities SET status='drafted' WHERE id = ? AND status IN ('new','enriched')",
            (req.opportunity_id,),
        )
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        new_row = cur.fetchone()

    log_activity(
        "draft", draft_id, "generated",
        actor_type="ai",
        actor_id=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        metadata={
            "prompt_version": PROMPT_VERSION,
            "opportunity_id": req.opportunity_id,
            "contact_id": req.contact_id,
        },
    )
    return _row_to_draft(new_row)


# ─────────────────────────────────────────────────────────────
# GET /api/drafts/{id}
# ─────────────────────────────────────────────────────────────

@router.get("/{draft_id}", response_model=DraftRead)
def get_draft(draft_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"Draft {draft_id} not found")
    return _row_to_draft(row)


# ─────────────────────────────────────────────────────────────
# POST /api/drafts/{id}/approve
# ─────────────────────────────────────────────────────────────

@router.post("/{draft_id}/approve")
def approve_draft(draft_id: int, body: DraftApprove):
    """
    Approve a pending draft. If `from_mailbox` is provided, also send the email
    via that Gmail mailbox and log a row in email_sends.
    """
    # 1. Validate + load
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT d.*, c.email AS contact_email, c.first_name AS contact_first_name,
                   c.last_name AS contact_last_name
            FROM email_drafts d
            JOIN contacts c ON c.id = d.contact_id
            WHERE d.id = ?
            """,
            (draft_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Draft {draft_id} not found")
        if row["approval_status"] != "pending":
            raise HTTPException(409, f"Draft already {row['approval_status']}")
        draft_data = dict(row)

    # 2. If a mailbox was provided, validate it exists in config
    chosen_mb = None
    if body.from_mailbox:
        chosen_mb = find_mailbox(body.from_mailbox)
        if not chosen_mb:
            raise HTTPException(400, f"Mailbox '{body.from_mailbox}' is not configured in mailboxes.json")

    # 3. Mark approved (with any edits)
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE email_drafts SET
                approval_status = 'approved',
                approved_by     = ?,
                approved_at     = ?,
                edited_body     = ?,
                subject_line    = COALESCE(?, subject_line)
            WHERE id = ?
            """,
            (
                body.approved_by,
                datetime.utcnow().isoformat(timespec="seconds"),
                body.edited_body,
                body.edited_subject,
                draft_id,
            ),
        )

    log_activity(
        "draft", draft_id, "approved",
        actor_type="user", actor_id=body.approved_by,
        metadata={"edited": bool(body.edited_body or body.edited_subject)},
    )

    # 4. Send the email if a mailbox was chosen
    send_info = None
    if chosen_mb:
        # Resolve recipient: override first, else contact's stored email
        final_to = (body.to_email_override or draft_data.get("contact_email") or "").strip()
        if not final_to:
            raise HTTPException(400, "No recipient address — set one on the contact or pass to_email_override.")

        final_subject = body.edited_subject or draft_data["subject_line"]
        final_body    = body.edited_body    or draft_data["body_text"]
        sender_display = os.getenv("DEFAULT_SENDER_NAME", "Maryam") + " (Denali Health)"

        try:
            result = send_email(
                from_mailbox_email = body.from_mailbox,
                to_email           = final_to,
                subject            = final_subject,
                body_text          = final_body,
                sender_display_name = sender_display,
            )
        except Exception as e:
            log.exception("Send failed for draft %s", draft_id)
            # Insert a failed send row so we have an audit trail
            with db_cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_sends (draft_id, send_status, message_id)
                    VALUES (?, 'failed', ?)
                    """,
                    (draft_id, f"error: {str(e)[:200]}"),
                )
            log_activity(
                "send", draft_id, "send_failed",
                actor_type="system",
                metadata={"mailbox": body.from_mailbox, "error": str(e)[:200]},
            )
            raise HTTPException(502, f"Email approved but send failed: {e}")

        # 5. Insert email_sends row
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_sends
                    (draft_id, recipient_email, from_mailbox_email, is_to_overridden,
                     sent_at, message_id, send_status)
                VALUES (?, ?, ?, ?, ?, ?, 'sent')
                """,
                (
                    draft_id,
                    final_to,
                    result.sent_via,
                    1 if body.to_email_override else 0,
                    datetime.utcnow().isoformat(timespec="seconds"),
                    result.message_id,
                ),
            )
            send_id = cur.lastrowid
            # Bump opportunity status
            cur.execute(
                "UPDATE opportunities SET status='sent' WHERE id = ? AND status IN ('drafted','enriched','new')",
                (draft_data["opportunity_id"],),
            )

        log_activity(
            "send", send_id, "sent",
            actor_type="system",
            metadata={
                "mailbox":    result.sent_via,
                "to":         final_to,
                "to_overridden": bool(body.to_email_override),
                "dry_run":    result.dry_run,
                "message_id": result.message_id,
            },
        )

        send_info = {
            "send_id":    send_id,
            "message_id": result.message_id,
            "sent_via":   result.sent_via,
            "dry_run":    result.dry_run,
            "to":         final_to,
            "to_overridden": bool(body.to_email_override),
        }

    # 6. Return the updated draft + send info
    with db_cursor() as cur:
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        new_row = cur.fetchone()

    return {
        "draft": _row_to_draft(new_row).model_dump(mode="json"),
        "send":  send_info,
    }


# ─────────────────────────────────────────────────────────────
# POST /api/drafts/{id}/reject
# ─────────────────────────────────────────────────────────────

@router.post("/{draft_id}/reject", response_model=DraftRead)
def reject_draft(draft_id: int, body: DraftReject):
    with db_cursor() as cur:
        cur.execute("SELECT approval_status FROM email_drafts WHERE id = ?", (draft_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Draft {draft_id} not found")
        if row["approval_status"] != "pending":
            raise HTTPException(409, f"Draft already {row['approval_status']}")

        cur.execute(
            "UPDATE email_drafts SET approval_status='rejected', rejection_reason=? WHERE id = ?",
            (body.rejection_reason, draft_id),
        )
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        new_row = cur.fetchone()
    log_activity(
        "draft", draft_id, "rejected",
        actor_type="user", actor_id=body.rejected_by,
        metadata={"reason": body.rejection_reason},
    )
    return _row_to_draft(new_row)

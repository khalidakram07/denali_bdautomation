"""
routers/drafts.py — AI email drafting + the human approval queue.

Endpoints:
    GET    /api/drafts/                List drafts (filter by approval_status)
    POST   /api/drafts/generate        Generate a new draft via Anthropic
    GET    /api/drafts/{id}            Detail
    POST   /api/drafts/{id}/approve    Approve & queue for send
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
    """
    List drafts joined with opportunity + contact context, newest first.
    The approval queue UI uses this — filter by approval_status='pending' for the inbox.
    """
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
    """Generate an AI draft for a (opportunity, contact) pair."""
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
        draft = generate_draft(opp, contact, sender_name=sender_name)
    except Exception as e:
        log.exception("AI generation failed for opp=%s contact=%s", req.opportunity_id, req.contact_id)
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
                draft.opportunity_id,
                draft.contact_id,
                draft.sequence_step,
                draft.subject_line,
                draft.body_text,
                draft.prompt_version,
                json.dumps(draft.quality_flags) if draft.quality_flags else None,
            ),
        )
        draft_id = cur.lastrowid

        # Bump opportunity status: new/enriched → drafted
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

@router.post("/{draft_id}/approve", response_model=DraftRead)
def approve_draft(draft_id: int, body: DraftApprove):
    """Approve a pending draft — optionally with edits to subject/body."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Draft {draft_id} not found")
        if row["approval_status"] != "pending":
            raise HTTPException(409, f"Draft already {row['approval_status']}")

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

        # Bump opportunity status: drafted → sent (the actual send is queued)
        cur.execute(
            "UPDATE opportunities SET status='sent' WHERE id = ? AND status='drafted'",
            (row["opportunity_id"],),
        )
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        new_row = cur.fetchone()

    log_activity(
        "draft", draft_id, "approved",
        actor_type="user", actor_id=body.approved_by,
        metadata={"edited": bool(body.edited_body or body.edited_subject)},
    )
    return _row_to_draft(new_row)


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

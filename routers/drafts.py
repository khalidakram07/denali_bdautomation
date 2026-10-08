"""
routers/drafts.py — AI email drafting + the human approval queue.

Live-Sheets model: opportunities + contacts are NOT persisted. The /generate
endpoint receives the trial + contact as snapshots read straight from the
selected category's Google Sheet, generates the email, and stores a
self-contained draft (snapshot columns) so approval + send history still work
without any opportunities/contacts rows.

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

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile, status

from database import db_cursor, log_activity
from models import (
    ApprovalStatus,
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
                COALESCE(d.trial_title, '')  AS opportunity_title,
                COALESCE(d.contact_name, '') AS contact_name
        FROM    email_drafts d
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
    opp = dict(req.opportunity or {})
    contact = dict(req.contact or {})
    if not opp:
        raise HTTPException(400, "Missing opportunity data")
    if not contact:
        raise HTTPException(400, "Missing contact data")

    # Sentinel ids: ai_engine echoes opp['id']/contact['id'] into DraftCreate
    # (typed int). We store a snapshot instead of FK references.
    opp["id"] = 0
    contact["id"] = 0

    # The template path looks for the trial's Full Text under raw_data["Full Text"].
    if not opp.get("raw_data"):
        opp["raw_data"] = {
            "Title":      opp.get("trial_title"),
            "Company":    opp.get("sponsor_name"),
            "Drugs":      opp.get("drug"),
            "Conditions": opp.get("indication"),
            "Trial IDs":  opp.get("trial_id") or opp.get("nct_number"),
            "Phase":      opp.get("phase"),
            "Source URL": opp.get("source_url"),
            "Full Text":  opp.get("full_text"),
        }

    sender_name = os.getenv("DEFAULT_SENDER_NAME", "Maryam")
    try:
        draft = generate_draft(
            opp, contact,
            sender_name=sender_name,
            template_filename=req.template_filename,
        )
    except Exception as e:
        log.exception("AI generation failed (category=%s trial=%s)",
                      opp.get("category"), opp.get("trial_id"))
        raise HTTPException(502, f"AI generation failed: {e}")

    first = (contact.get("first_name") or "").strip()
    last  = (contact.get("last_name") or "").strip()
    contact_name = contact.get("full_name") or (f"{first} {last}".strip()) or None

    snap = {
        "category":        opp.get("category"),
        "trial_id":        opp.get("trial_id") or opp.get("nct_number"),
        "trial_title":     opp.get("trial_title"),
        "sponsor_name":    opp.get("sponsor_name"),
        "contact_name":    contact_name,
        "contact_title":   contact.get("title"),
        "recipient_email": contact.get("email"),
        "contact_score":   contact.get("contact_score"),
    }

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_drafts
                (opportunity_id, contact_id, sequence_step, subject_line, body_text,
                 prompt_version, quality_flags, approval_status,
                 category, trial_id, trial_title, sponsor_name,
                 contact_name, contact_title, recipient_email, contact_score)
            VALUES (0, 0, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.sequence_step, draft.subject_line, draft.body_text,
                draft.prompt_version,
                json.dumps(draft.quality_flags) if draft.quality_flags else None,
                snap["category"], snap["trial_id"], snap["trial_title"], snap["sponsor_name"],
                snap["contact_name"], snap["contact_title"], snap["recipient_email"], snap["contact_score"],
            ),
        )
        draft_id = cur.lastrowid
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        new_row = cur.fetchone()

    log_activity(
        "draft", draft_id, "generated",
        actor_type="ai",
        actor_id=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        metadata={
            "prompt_version": PROMPT_VERSION,
            "category": snap["category"],
            "trial_id": snap["trial_id"],
            "contact":  snap["contact_name"],
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
async def approve_draft(
    draft_id: int,
    background_tasks: BackgroundTasks,
    approved_by: str = Form(...),
    edited_body: Optional[str] = Form(None),
    edited_subject: Optional[str] = Form(None),
    from_mailbox: Optional[str] = Form(None),
    to_email_override: Optional[str] = Form(None),
    cc_emails: Optional[str] = Form(None),
    attachments: list[UploadFile] = File(default=[]),
):
    """
    Approve a pending draft. If `from_mailbox` is provided, also send the email
    via that Gmail mailbox and log a row in email_sends.

    Accepts multipart/form-data so one or more files can be attached at send time.
    The recipient comes from the draft's snapshot (recipient_email) unless overridden.
    cc_emails: optional comma-separated string of CC addresses.
    """
    edited_body       = (edited_body or "").strip() or None
    edited_subject    = (edited_subject or "").strip() or None
    from_mailbox      = (from_mailbox or "").strip() or None
    to_email_override = (to_email_override or "").strip() or None
    cc_emails_raw     = (cc_emails or "").strip() or None

    # Read uploaded attachments into memory as (filename, bytes)
    attachment_files: list[tuple[str, bytes]] = []
    attachment_names: list[str] = []
    for up in attachments or []:
        if up is None or not up.filename:
            continue
        data = await up.read()
        if data:
            attachment_names.append(up.filename)
            attachment_files.append((up.filename, data))
    attachment_label = ", ".join(attachment_names) if attachment_names else None

    # 1. Validate + load (recipient comes from the draft snapshot)
    with db_cursor() as cur:
        cur.execute(
            "SELECT d.*, d.recipient_email AS contact_email "
            "FROM email_drafts d WHERE d.id = ?",
            (draft_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"Draft {draft_id} not found")

        status = (row["approval_status"] or "").lower()
        if status == "rejected":
            raise HTTPException(409, "Draft was rejected; regenerate before re-approving.")
        if status == "approved":
            # Soft block: only reject the retry if a successful send is already
            # on record. Otherwise this is a draft that was marked approved by
            # the old flow before the SMTP send blew up; let the user retry.
            cur.execute(
                "SELECT 1 FROM email_sends "
                "WHERE draft_id = ? AND send_status IN ('sent','queued') LIMIT 1",
                (draft_id,),
            )
            if cur.fetchone():
                raise HTTPException(409, "Draft already sent (or send in progress).")
            # Otherwise fall through and let the retry happen.
        draft_data = dict(row)

    # 2. If a mailbox was provided, validate it exists in config
    chosen_mb = None
    if from_mailbox:
        chosen_mb = find_mailbox(from_mailbox)
        if not chosen_mb:
            raise HTTPException(400, f"Mailbox '{from_mailbox}' is not configured in mailboxes.json")

    # 3. Mark approved (with any edits).
    #
    # Important: we only flip approval_status to 'approved' here if the user
    # ISN'T sending via SMTP. When a mailbox is chosen we delay the UPDATE
    # until AFTER the send succeeds, so a failed SMTP attempt leaves the
    # draft in 'pending' state and the user can retry. Previously the UPDATE
    # ran first, so one failed send stuck the user on 409 Draft already
    # approved and the Approve & Send button looked permanently broken.
    def _mark_draft_approved():
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
                    approved_by,
                    datetime.utcnow().isoformat(timespec="seconds"),
                    edited_body,
                    edited_subject,
                    draft_id,
                ),
            )
        log_activity(
            "draft", draft_id, "approved",
            actor_type="user", actor_id=approved_by,
            metadata={"edited": bool(edited_body or edited_subject)},
        )

    # 4. Mark approved IMMEDIATELY (instant UI feedback). Then, if a mailbox was
    #    chosen, insert a 'queued' email_sends row and hand the real SMTP work to
    #    a background task. The HTTP response returns in milliseconds instead of
    #    waiting 3-8s for Gmail. Status flips to 'sent' or 'failed' on the
    #    Analytics / history page once the worker finishes.
    _mark_draft_approved()

    send_info = None
    if chosen_mb:
        final_to = (to_email_override or draft_data.get("contact_email") or "").strip()
        if not final_to:
            raise HTTPException(400, "No recipient address — this lead has no email; pass to_email_override.")

        final_subject = edited_subject or draft_data["subject_line"]
        final_body    = edited_body    or draft_data["body_text"]
        sender_display = (chosen_mb.get("display_name") or "").strip() \
            or f"{os.getenv('DEFAULT_SENDER_NAME', 'Maryam')} (Denali Health)"

        # Normalize CC list once so we can log + push consistently.
        from services.email_sender import _clean_email_list as _clean_cc
        cc_list_final = _clean_cc(cc_emails_raw)
        cc_list_final = [
            c for c in cc_list_final
            if c.lower() not in ((final_to or "").lower(), (from_mailbox or "").lower())
        ]
        cc_joined = ",".join(cc_list_final) if cc_list_final else None

        # Insert a 'queued' send row so the UI (and the retry guard) can see
        # there's an in-flight send even before SMTP returns.
        queued_at = datetime.utcnow().isoformat(timespec="seconds")
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_sends
                    (draft_id, recipient_email, from_mailbox_email, is_to_overridden,
                     sent_at, send_status, cc_emails)
                VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    draft_id, final_to, from_mailbox,
                    1 if to_email_override else 0,
                    queued_at, cc_joined,
                ),
            )
            send_id = cur.lastrowid

        log_activity(
            "send", send_id, "queued",
            actor_type="system",
            metadata={"mailbox": from_mailbox, "to": final_to,
                      "to_overridden": bool(to_email_override)},
        )

        # Hand the slow work (SMTP + Sheets write-back + HubSpot push) to a
        # background task. FastAPI runs it after the response is sent.
        background_tasks.add_task(
            _do_send_in_background,
            send_id         = send_id,
            draft_id        = draft_id,
            from_mailbox    = from_mailbox,
            final_to        = final_to,
            final_subject   = final_subject,
            final_body      = final_body,
            sender_display  = sender_display,
            attachment_files= attachment_files,
            attachment_names= attachment_names,
            cc_list_final   = cc_list_final,
            cc_joined       = cc_joined,
            to_email_override = to_email_override,
            draft_data      = draft_data,
        )

        send_info = {
            "send_id":    send_id,
            "status":     "queued",
            "sent_via":   from_mailbox,
            "to":         final_to,
            "cc":         cc_list_final,
            "to_overridden": bool(to_email_override),
            "attachment": attachment_label,
            "attachment_names": attachment_names,
            "attachment_count": len(attachment_files),
        }

    # 5. Return the updated draft + send info (immediately)
    with db_cursor() as cur:
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        new_row = cur.fetchone()

    return {
        "draft": _row_to_draft(new_row).model_dump(mode="json"),
        "send":  send_info,
    }


# ─────────────────────────────────────────────────────────────
# Background worker: actual SMTP + Sheets + HubSpot
# ─────────────────────────────────────────────────────────────

def _do_send_in_background(
    send_id: int,
    draft_id: int,
    from_mailbox: str,
    final_to: str,
    final_subject: str,
    final_body: str,
    sender_display: str,
    attachment_files: list,
    attachment_names: list,
    cc_list_final: list,
    cc_joined: Optional[str],
    to_email_override: Optional[str],
    draft_data: dict,
):
    """
    Runs after the HTTP response is returned. Does the SMTP send and the
    best-effort Sheets + HubSpot pushes, then updates the email_sends row
    from 'queued' -> 'sent' or 'failed'. Never raises (errors go to the DB).
    """
    try:
        result = send_email(
            from_mailbox_email = from_mailbox,
            to_email           = final_to,
            subject            = final_subject,
            body_text          = final_body,
            sender_display_name = sender_display,
            attachments        = attachment_files or None,
            cc_emails          = cc_list_final or None,
        )
    except Exception as e:
        log.exception("Background send failed for draft %s (send_id=%s)", draft_id, send_id)
        try:
            with db_cursor() as cur:
                cur.execute(
                    "UPDATE email_sends SET send_status='failed', message_id=?, "
                    "sent_at=? WHERE id=?",
                    (f"error: {str(e)[:200]}",
                     datetime.utcnow().isoformat(timespec="seconds"),
                     send_id),
                )
            log_activity(
                "send", send_id, "send_failed",
                actor_type="system",
                metadata={"mailbox": from_mailbox, "error": str(e)[:200]},
            )
        except Exception:
            log.exception("Could not even record the failure for send_id=%s", send_id)
        return

    # Success: flip the row from 'queued' -> 'sent' and attach the real
    # message-id / mailbox used.
    try:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE email_sends SET send_status='sent', message_id=?, "
                "from_mailbox_email=?, sent_at=? WHERE id=?",
                (result.message_id, result.sent_via,
                 datetime.utcnow().isoformat(timespec="seconds"), send_id),
            )
    except Exception:
        log.exception("Could not update send row to 'sent' for send_id=%s", send_id)

    # Sheets write-back (best-effort)
    try:
        from services.google_sheets import mark_lead_sent
        from services import leads_categories as lc
        cat = draft_data.get("category")
        tid = draft_data.get("trial_id")
        if cat and tid:
            cats = lc.list_categories()
            sheet_id = next((c["sheet_id"] for c in cats if c["name"] == cat), None)
            if sheet_id:
                sent_at_iso = datetime.utcnow().isoformat(timespec="seconds")
                lookup_email = (draft_data.get("contact_email") or final_to).strip()
                marked = mark_lead_sent(sheet_id, tid, lookup_email, sent_at_iso)
                log.info("mark_lead_sent for %s (sent to %s) in %s -> %s",
                         lookup_email, final_to, cat, marked)
            lc._category_data_cache.pop(cat, None)
    except Exception as e:
        log.exception("mark_lead_sent failed (email still sent): %s", e)

    # HubSpot push (best-effort)
    hubspot_info = {"contact_id": None, "deal_id": None, "engagement_id": None, "error": None}
    try:
        from services import hubspot as hs
        contact_props = {}
        if draft_data.get("contact_name"):
            parts = draft_data["contact_name"].strip().split(" ", 1)
            contact_props["firstname"] = parts[0]
            if len(parts) > 1:
                contact_props["lastname"] = parts[1]
        if draft_data.get("contact_title"):
            contact_props["jobtitle"]         = draft_data["contact_title"]
        if draft_data.get("sponsor_name"):
            contact_props["site_institution"] = draft_data["sponsor_name"]
        contact_props["preferred_mailbox"]    = result.sent_via or ""

        contact_id = hs.upsert_contact(final_to, contact_props)

        deal_props = {
            "indication":            draft_data.get("category") or "",
            "denali_category":       draft_data.get("category") or "",
            "primary_contact_email": final_to,
        }
        if draft_data.get("trial_title"):
            deal_props["dealname"] = f"{draft_data.get('sponsor_name','')}, {draft_data['trial_title']}".strip(", ")
        deal_id = hs.upsert_deal(
            nct_id     = draft_data.get("trial_id") or "",
            sponsor    = draft_data.get("sponsor_name") or "",
            properties = deal_props,
            stage_label= "Outreach Needed",
            contact_id = contact_id,
        )

        engagement_id = hs.log_email_engagement(
            contact_id = contact_id,
            subject    = final_subject,
            body_html  = final_body,
            from_email = result.sent_via or "",
            to_email   = final_to,
            deal_id    = deal_id,
            cc_emails  = cc_list_final or None,
        )
        hubspot_info.update({
            "contact_id":    contact_id,
            "deal_id":       deal_id,
            "engagement_id": engagement_id,
        })
        try:
            enroll_res = hs.enroll_contact_in_sequence(
                contact_id   = contact_id,
                sender_email = result.sent_via,
            )
            hubspot_info["sequence_enrollment"] = enroll_res
        except Exception as e:
            hubspot_info["sequence_enrollment"] = {"ok": False, "error": str(e)[:200]}
    except Exception as e:
        hubspot_info["error"] = str(e)[:300]
        log.exception("HubSpot push failed (email still sent): %s", e)

    log_activity(
        "send", send_id, "sent",
        actor_type="system",
        metadata={
            "mailbox":    result.sent_via,
            "to":         final_to,
            "cc":         cc_list_final,
            "to_overridden": bool(to_email_override),
            "dry_run":    result.dry_run,
            "message_id": result.message_id,
            "category":   draft_data.get("category"),
            "trial":      draft_data.get("trial_title"),
            "attachments": attachment_names,
            "attachment_count": result.attachment_count,
            "hubspot":    hubspot_info,
        },
    )


# ─────────────────────────────────────────────────────────────
# Reject a draft
# ─────────────────────────────────────────────────────────────

@router.post("/{draft_id}/reject", response_model=DraftRead)
def reject_draft(draft_id: int, payload: DraftReject):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Draft not found")
    if row["approval_status"] != ApprovalStatus.PENDING.value:
        raise HTTPException(409, f"Draft already {row['approval_status']}")

    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE email_drafts
            SET approval_status = ?, rejection_reason = ?, reviewer = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (ApprovalStatus.REJECTED.value, payload.reason, payload.reviewer,
             datetime.utcnow().isoformat(timespec="seconds"), draft_id),
        )
        cur.execute("SELECT * FROM email_drafts WHERE id = ?", (draft_id,))
        new_row = cur.fetchone()

    log_activity("draft", draft_id, "rejected", actor=payload.reviewer,
                 metadata={"reason": payload.reason})

    return _row_to_draft(new_row)

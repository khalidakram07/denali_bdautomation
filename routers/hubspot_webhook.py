"""
routers/hubspot_webhook.py — Part C: receive reply notifications from HubSpot.

When Maryam's HubSpot Workflow fires on a new inbound email engagement, it
POSTs here. We fetch the engagement body, classify it with Claude, and update
the Contact + advance the Deal stage per the REPLY_TRANSITIONS table.

Setup in HubSpot UI (one-time, by Maryam or Khalid):
    Workflows → Create Contact-based workflow
    Trigger:  Filter "Email engagement" → Direction = Incoming
    Action:   Send webhook → POST → https://denali-bd.onrender.com/api/hubspot/webhook
              Include contact ID + engagement ID in the body

Endpoint:
    POST /api/hubspot/webhook
        Body (any of these key names — we try them all):
          { "contactId": "...", "engagementId": "..." }
          or HubSpot's stock format {"objectId":"...", ...}
"""

from datetime import datetime
import logging

from fastapi import APIRouter, HTTPException, Request

from database import db_cursor, log_activity
from services import hubspot as hs

log = logging.getLogger(__name__)
router = APIRouter()


def _pluck(payload: dict, *keys: str) -> str | None:
    """Return the first non-empty value found at any of the given top-level keys."""
    for k in keys:
        v = payload.get(k)
        if v:
            return str(v)
    return None


def _process_reply(contact_id: str, engagement_id: str | None) -> dict:
    """Core reply-handling logic. Idempotent enough for retries."""
    # 1. Fetch the engagement (the actual email body) if HubSpot gave us its ID
    body_text = ""
    direction = "INCOMING"
    if engagement_id:
        eng = hs.get_engagement(engagement_id)
        meta = (eng or {}).get("metadata", {}) or {}
        body_text = meta.get("text") or meta.get("html") or ""
        direction = (eng or {}).get("engagement", {}).get("source", "INCOMING")

    # 2. Skip outbound emails (our own sends)
    if direction and "OUT" in direction.upper():
        log.info("skip outbound engagement %s", engagement_id)
        return {"skipped": "outbound"}

    # 3. Classify with Claude
    label = hs.classify_reply(body_text)
    rule  = hs.REPLY_TRANSITIONS.get(label, {})

    # 4. Update Contact properties
    contact_props = {
        "last_reply_status": rule.get("contact_status") or label,
        "last_reply_at":     datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    contact_props.update(rule.get("extra_contact_props") or {})
    try:
        hs._request("PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                    json={"properties": contact_props})
    except Exception as e:
        log.exception("contact update failed: %s", e)

    # 5. Advance Deal stage if classifier rule says so
    deal_id = hs.find_latest_deal_for_contact(contact_id)
    deal_action = "no_change"
    if deal_id and rule.get("stage"):
        try:
            hs.advance_deal_stage(deal_id, rule["stage"], extra_props={
                "outreach_outcome": rule.get("contact_status") or label,
            })
            deal_action = f"advanced to '{rule['stage']}'"
        except Exception as e:
            log.exception("deal advance failed: %s", e)
            deal_action = f"failed: {e}"
    elif deal_id:
        # No stage change but record the outcome
        try:
            hs._request("PATCH", f"/crm/v3/objects/deals/{deal_id}",
                        json={"properties": {"outreach_outcome": rule.get("contact_status") or label}})
            deal_action = "outcome tagged (no stage change)"
        except Exception as e:
            log.exception("deal outcome update failed: %s", e)

    # 6. Local activity log so the dashboard can show what happened
    log_activity("hubspot_reply", None, label,
                 actor_type="hubspot_webhook",
                 metadata={"contact_id": contact_id, "deal_id": deal_id,
                           "label": label, "deal_action": deal_action,
                           "body_preview": (body_text or "")[:200]})

    return {"label": label, "contact_id": contact_id, "deal_id": deal_id,
            "deal_action": deal_action, "stop_cadence": bool(rule.get("stop_cadence"))}


@router.post("/webhook")
async def hubspot_webhook(request: Request):
    """
    Receives reply notifications from HubSpot Workflows.

    Accepts several payload shapes (HubSpot Workflow webhook UI is flexible):
      { "contactId": "12345", "engagementId": "98765" }
      { "objectId":  "12345", "engagementId": "98765" }
      { "vid":       "12345", "engagement": {"id": "98765"} }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")

    if isinstance(payload, list):
        # HubSpot batch-mode webhooks send a list of events
        results = []
        for item in payload:
            cid = _pluck(item, "contactId", "objectId", "vid")
            eid = _pluck(item, "engagementId") or _pluck(item.get("engagement", {}) or {}, "id")
            if not cid:
                results.append({"error": "missing contactId"})
                continue
            results.append(_process_reply(cid, eid))
        return {"processed": len(results), "items": results}

    cid = _pluck(payload, "contactId", "objectId", "vid")
    eid = _pluck(payload, "engagementId") or _pluck(payload.get("engagement", {}) or {}, "id")
    if not cid:
        raise HTTPException(400, "Payload must include contactId / objectId / vid")
    return _process_reply(cid, eid)

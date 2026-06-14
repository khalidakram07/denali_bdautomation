"""
services/hubspot.py — HubSpot integration.

Two responsibilities:
  1. SCHEMA — bootstrap_properties() ensures every custom property + pipeline
     stage we depend on exists. Idempotent: run it any time, only creates what's
     missing. Schema-as-code so the next person can rebuild HubSpot from this file.
  2. RUNTIME — upsert_contact(), upsert_deal(), log_email_engagement() — called
     from routers/drafts.py after every approved send (Part B of the spec).

Auth: a Service Key in env var HUBSPOT_TOKEN (pat-na2-…). Portal ID is
hardcoded to 246352213 (Denali Health) for now.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
HUBSPOT_TOKEN  = os.getenv("HUBSPOT_TOKEN", "")
HUBSPOT_PORTAL = os.getenv("HUBSPOT_PORTAL_ID", "246352213")
HUBSPOT_BASE   = "https://api.hubapi.com"

# Pipeline name (created in HubSpot UI; we look up its ID at runtime).
PIPELINE_NAME = "Clinical Trial Outreach"

# Stages must match what's in HubSpot exactly (case-insensitive lookup).
PIPELINE_STAGES = [
    "Outreach Needed",
    "Nurture (No Reply)",
    "CDA Executed",
    "Qualification",
    "Site Visit Scheduled",
    "Selected as Site",
    "Lost / Closed",
]


# ─────────────────────────────────────────────────────────────
# Schema definition (Part A) — what bootstrap_properties() creates.
# ─────────────────────────────────────────────────────────────

# Deal custom properties (HubSpot object type 0-3)
DEAL_PROPERTIES: list[dict[str, Any]] = [
    {"name": "trial_nct_id",         "label": "Trial NCT ID",         "type": "string",   "fieldType": "text",
     "description": "ClinicalTrials.gov ID (e.g. NCT05512345)"},
    {"name": "sponsor",              "label": "Sponsor",              "type": "string",   "fieldType": "text",
     "description": "Trial sponsor — pharma company name"},
    {"name": "indication",           "label": "Indication",           "type": "string",   "fieldType": "text",
     "description": "Therapeutic area (Alzheimer, CNS, etc.)"},
    {"name": "denali_category",      "label": "Denali Category",      "type": "string",   "fieldType": "text",
     "description": "LeadsCategory folder this opp came from"},
    {"name": "last_outreach_at",     "label": "Last Outreach At",     "type": "datetime", "fieldType": "date",
     "description": "Most recent send_at timestamp"},
    {"name": "days_since_touch",     "label": "Days Since Last Touch","type": "number",   "fieldType": "number",
     "description": "Days since the last outreach email"},
    {"name": "outreach_outcome",     "label": "Outreach Outcome",     "type": "enumeration", "fieldType": "select",
     "description": "Classified result of the latest reply",
     "options": [
         {"label": "Pending",        "value": "pending",        "displayOrder": 0},
         {"label": "Interested",     "value": "interested",     "displayOrder": 1},
         {"label": "Not Interested", "value": "not_interested", "displayOrder": 2},
         {"label": "Wrong Contact",  "value": "wrong_contact",  "displayOrder": 3},
         {"label": "Out of Office",  "value": "out_of_office",  "displayOrder": 4},
         {"label": "Opted Out",      "value": "opt_out",        "displayOrder": 5},
         {"label": "No Reply",       "value": "no_reply",       "displayOrder": 6},
     ]},
    {"name": "primary_contact_email","label": "Primary Contact Email","type": "string",   "fieldType": "text",
     "description": "Email of the PI/coordinator we're reaching out to"},
]

# Contact custom properties (HubSpot object type 0-1)
CONTACT_PROPERTIES: list[dict[str, Any]] = [
    {"name": "site_institution",    "label": "Site Institution",     "type": "string",   "fieldType": "text",
     "description": "Hospital / research site (e.g. Mass General)"},
    {"name": "last_emailed_at",     "label": "Last Emailed At",      "type": "datetime", "fieldType": "date",
     "description": "Most recent send_at timestamp to this contact"},
    {"name": "emails_sent_total",   "label": "Emails Sent (Total)",  "type": "number",   "fieldType": "number",
     "description": "Running total of outreach emails sent"},
    {"name": "last_reply_status",   "label": "Last Reply Status",    "type": "enumeration", "fieldType": "select",
     "description": "Classified result of the latest reply",
     "options": [
         {"label": "No Response",   "value": "no_response",   "displayOrder": 0},
         {"label": "Interested",    "value": "interested",    "displayOrder": 1},
         {"label": "Not Interested","value": "not_interested","displayOrder": 2},
         {"label": "Wrong Contact", "value": "wrong_contact", "displayOrder": 3},
         {"label": "Out of Office", "value": "out_of_office", "displayOrder": 4},
         {"label": "Opted Out",     "value": "opt_out",       "displayOrder": 5},
     ]},
    {"name": "last_reply_at",       "label": "Last Reply At",        "type": "datetime", "fieldType": "date",
     "description": "When the latest reply arrived"},
    {"name": "preferred_mailbox",   "label": "Preferred Mailbox",    "type": "string",   "fieldType": "text",
     "description": "Whichever Denali mailbox they replied to last"},
    {"name": "opted_out",           "label": "Opted Out",            "type": "bool",     "fieldType": "booleancheckbox",
     "description": "True = suppress forever; never email again",
     "options": [
         {"label": "True",  "value": "true",  "displayOrder": 0},
         {"label": "False", "value": "false", "displayOrder": 1},
     ]},
]

PROPERTY_GROUP_NAME = "denali_bd_automation"
PROPERTY_GROUP_LABEL = "Denali BD Automation"


# ─────────────────────────────────────────────────────────────
# HTTP plumbing
# ─────────────────────────────────────────────────────────────

class HubSpotError(RuntimeError):
    """Wraps any non-2xx HubSpot API response."""


def _headers() -> dict[str, str]:
    if not HUBSPOT_TOKEN:
        raise HubSpotError("HUBSPOT_TOKEN is not set in environment")
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type":  "application/json",
    }


def _request(method: str, path: str, *, json: Any = None, params: dict | None = None,
             ok_statuses: Iterable[int] = (200, 201, 204)) -> Any:
    url = f"{HUBSPOT_BASE}{path}"
    resp = requests.request(method, url, headers=_headers(), json=json, params=params, timeout=30)
    if resp.status_code not in ok_statuses:
        raise HubSpotError(f"{method} {path} → {resp.status_code}: {resp.text[:500]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


# ─────────────────────────────────────────────────────────────
# Bootstrap: properties + groups (idempotent)
# ─────────────────────────────────────────────────────────────

def _ensure_property_group(object_type: str) -> None:
    """Create the 'Denali BD Automation' group on the given object type if missing."""
    path = f"/crm/v3/properties/{object_type}/groups"
    try:
        existing = _request("GET", path).get("results", [])
    except HubSpotError as e:
        log.warning("Could not list groups for %s: %s", object_type, e)
        existing = []
    if any(g.get("name") == PROPERTY_GROUP_NAME for g in existing):
        return
    _request("POST", path, json={
        "name":         PROPERTY_GROUP_NAME,
        "label":        PROPERTY_GROUP_LABEL,
        "displayOrder": -1,
    })
    log.info("Created property group '%s' on %s", PROPERTY_GROUP_NAME, object_type)


def _ensure_property(object_type: str, spec: dict[str, Any]) -> str:
    """Create the property if missing. Returns 'created' / 'exists'."""
    name = spec["name"]
    get_path = f"/crm/v3/properties/{object_type}/{name}"
    try:
        _request("GET", get_path)
        return "exists"
    except HubSpotError as e:
        if "404" not in str(e):
            raise

    payload = {
        "name":        name,
        "label":       spec["label"],
        "type":        spec["type"],
        "fieldType":   spec["fieldType"],
        "groupName":   PROPERTY_GROUP_NAME,
        "description": spec.get("description", ""),
    }
    if "options" in spec:
        payload["options"] = spec["options"]

    _request("POST", f"/crm/v3/properties/{object_type}", json=payload)
    return "created"


def bootstrap_properties() -> dict[str, Any]:
    """Idempotently create all custom properties + groups. Safe to call repeatedly."""
    result: dict[str, Any] = {"deals": [], "contacts": []}

    _ensure_property_group("deals")
    for spec in DEAL_PROPERTIES:
        status = _ensure_property("deals", spec)
        result["deals"].append({"name": spec["name"], "status": status})

    _ensure_property_group("contacts")
    for spec in CONTACT_PROPERTIES:
        status = _ensure_property("contacts", spec)
        result["contacts"].append({"name": spec["name"], "status": status})

    return result


# ─────────────────────────────────────────────────────────────
# Pipeline / stage lookup (cached after first call)
# ─────────────────────────────────────────────────────────────

_pipeline_cache: dict[str, Any] = {}


def _get_pipeline() -> dict[str, Any]:
    """Return the pipeline dict matching PIPELINE_NAME, with stages."""
    if "pipeline" in _pipeline_cache:
        return _pipeline_cache["pipeline"]
    data = _request("GET", "/crm/v3/pipelines/deals")
    for p in data.get("results", []):
        if p.get("label", "").strip().lower() == PIPELINE_NAME.lower():
            _pipeline_cache["pipeline"] = p
            return p
    raise HubSpotError(f"Pipeline '{PIPELINE_NAME}' not found in HubSpot. Create it first.")


def get_stage_id(stage_label: str) -> str:
    """Resolve a human stage label (e.g. 'Outreach Needed') to its HubSpot stage ID."""
    p = _get_pipeline()
    for s in p.get("stages", []):
        if s.get("label", "").strip().lower() == stage_label.lower():
            return s["id"]
    raise HubSpotError(f"Stage '{stage_label}' not found in pipeline '{PIPELINE_NAME}'")


def get_pipeline_id() -> str:
    return _get_pipeline()["id"]


# ─────────────────────────────────────────────────────────────
# Runtime: upsert Contact / Deal, log Email engagement (Part B)
# ─────────────────────────────────────────────────────────────

def _iso_now() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _ms_now() -> int:
    return int(_dt.datetime.utcnow().timestamp() * 1000)


def upsert_contact(email: str, properties: dict[str, Any]) -> str:
    """
    Find a Contact by email or create one. Returns the Contact ID.
    Bumps emails_sent_total and sets last_emailed_at; caller passes any extra fields.
    """
    email = (email or "").strip().lower()
    if not email:
        raise HubSpotError("upsert_contact requires a non-empty email")

    # Search first
    search = _request("POST", "/crm/v3/objects/contacts/search", json={
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "emails_sent_total"],
        "limit": 1,
    })
    results = search.get("results", [])
    base_props = {
        "email":           email,
        "last_emailed_at": _iso_now(),
        **properties,
    }

    if results:
        cid = results[0]["id"]
        prev = int(results[0].get("properties", {}).get("emails_sent_total") or 0)
        base_props["emails_sent_total"] = str(prev + 1)
        _request("PATCH", f"/crm/v3/objects/contacts/{cid}", json={"properties": base_props})
        return cid

    base_props["emails_sent_total"] = "1"
    created = _request("POST", "/crm/v3/objects/contacts", json={"properties": base_props})
    return created["id"]


def upsert_deal(nct_id: str, sponsor: str, properties: dict[str, Any],
                stage_label: str = "Outreach Needed",
                contact_id: str | None = None) -> str:
    """
    Find a Deal by (trial_nct_id, sponsor) or create one in `stage_label`.
    Associates with `contact_id` when given. Returns the Deal ID.
    """
    nct_id  = (nct_id  or "").strip()
    sponsor = (sponsor or "").strip()
    if not nct_id and not sponsor:
        raise HubSpotError("upsert_deal needs at least trial_nct_id or sponsor")

    filters = []
    if nct_id:
        filters.append({"propertyName": "trial_nct_id", "operator": "EQ", "value": nct_id})
    if sponsor:
        filters.append({"propertyName": "sponsor", "operator": "EQ", "value": sponsor})

    search = _request("POST", "/crm/v3/objects/deals/search", json={
        "filterGroups": [{"filters": filters}],
        "properties": ["dealname", "dealstage", "pipeline"],
        "limit": 1,
    })
    results = search.get("results", [])

    base_props = {
        "trial_nct_id":     nct_id,
        "sponsor":          sponsor,
        "last_outreach_at": _iso_now(),
        "days_since_touch": "0",
        **properties,
    }

    if results:
        did = results[0]["id"]
        _request("PATCH", f"/crm/v3/objects/deals/{did}", json={"properties": base_props})
    else:
        # New deal — set stage + pipeline + a reasonable dealname
        base_props["pipeline"]  = get_pipeline_id()
        base_props["dealstage"] = get_stage_id(stage_label)
        if "dealname" not in base_props:
            base_props["dealname"] = f"{sponsor} — {nct_id}".strip(" —")
        if "outreach_outcome" not in base_props:
            base_props["outreach_outcome"] = "pending"
        created = _request("POST", "/crm/v3/objects/deals", json={"properties": base_props})
        did = created["id"]

    if contact_id:
        try:
            _request("PUT",
                     f"/crm/v3/objects/deals/{did}/associations/contacts/{contact_id}/deal_to_contact",
                     json=None)
        except HubSpotError as e:
            log.warning("Could not associate deal %s with contact %s: %s", did, contact_id, e)

    return did


def log_email_engagement(contact_id: str, *, subject: str, body_html: str,
                         from_email: str, to_email: str,
                         deal_id: str | None = None,
                         sent_at_ms: int | None = None) -> str:
    """
    Create an Email engagement on the Contact's timeline (and Deal's, when linked).
    Returns the engagement ID.
    """
    payload = {
        "engagement": {
            "active":    True,
            "type":      "EMAIL",
            "timestamp": sent_at_ms or _ms_now(),
        },
        "associations": {
            "contactIds": [int(contact_id)],
            **({"dealIds": [int(deal_id)]} if deal_id else {}),
        },
        "metadata": {
            "from":    {"email": from_email},
            "to":      [{"email": to_email}],
            "subject": subject,
            "html":    body_html or "",
            "text":    "",
        },
    }
    res = _request("POST", "/engagements/v1/engagements", json=payload)
    eid = (res.get("engagement") or {}).get("id") or res.get("id")
    return str(eid)


def advance_deal_stage(deal_id: str, stage_label: str,
                       extra_props: dict[str, Any] | None = None) -> None:
    """Move a deal to a new pipeline stage. Used by reply-detection (Part C)."""
    props: dict[str, Any] = {"dealstage": get_stage_id(stage_label)}
    if extra_props:
        props.update(extra_props)
    _request("PATCH", f"/crm/v3/objects/deals/{deal_id}", json={"properties": props})


# ─────────────────────────────────────────────────────────────
# Part C — Reply classification + read helpers
# ─────────────────────────────────────────────────────────────

_CLASSIFY_PROMPT = """You read one reply email from a prospective clinical trial site
investigator and return EXACTLY ONE label from this list:

interested      → wants to learn more, asks questions, says yes, books a call
not_interested  → declines, says no thanks, says they don't take outreach
wrong_contact   → says "I'm not the right person", suggests forwarding to someone else
out_of_office   → auto-reply about being away, vacation responder, ooo
opt_out         → asks to be removed, unsubscribed, never contacted again

Return ONLY the label, lowercase, no other text.

REPLY:
---
{body}
---"""


def classify_reply(body: str) -> str:
    """Send reply text to Claude Haiku, return one of the 5 labels."""
    body = (body or "").strip()
    if not body:
        return "no_response"
    try:
        from anthropic import Anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.warning("ANTHROPIC_API_KEY not set — defaulting reply to 'interested'")
            return "interested"
        client = Anthropic(api_key=api_key)
        model = os.getenv("ANTHROPIC_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
        msg = client.messages.create(
            model=model, max_tokens=20,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(body=body[:4000])}],
        )
        label = (msg.content[0].text or "").strip().lower().split()[0]
        if label not in {"interested", "not_interested", "wrong_contact", "out_of_office", "opt_out"}:
            log.warning("classifier returned unrecognised label %r — defaulting interested", label)
            label = "interested"
        return label
    except Exception as e:
        log.exception("classify_reply failed, defaulting interested: %s", e)
        return "interested"


def get_engagement(engagement_id: str) -> dict[str, Any]:
    """Fetch a single email engagement by ID (legacy /engagements/v1 API)."""
    return _request("GET", f"/engagements/v1/engagements/{engagement_id}")


def find_latest_deal_for_contact(contact_id: str) -> str | None:
    """Return the most recent Deal ID linked to this Contact, in our pipeline."""
    res = _request(
        "GET",
        f"/crm/v4/objects/contacts/{contact_id}/associations/deals",
        params={"limit": 50},
    )
    deal_ids = [r["toObjectId"] for r in res.get("results", []) if r.get("toObjectId")]
    if not deal_ids:
        return None
    pid = get_pipeline_id()
    # batch read to filter by pipeline
    batch = _request(
        "POST", "/crm/v3/objects/deals/batch/read",
        json={"properties": ["pipeline", "hs_lastmodifieddate"],
              "inputs": [{"id": str(d)} for d in deal_ids]},
    )
    candidates = [d for d in batch.get("results", []) if d.get("properties", {}).get("pipeline") == pid]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.get("properties", {}).get("hs_lastmodifieddate", ""), reverse=True)
    return str(candidates[0]["id"])


# Mapping: classifier label → (Deal stage to advance to, contact reply_status)
REPLY_TRANSITIONS: dict[str, dict[str, Any]] = {
    "interested":     {"stage": "Qualification",   "contact_status": "interested",     "stop_cadence": True},
    "not_interested": {"stage": "Lost / Closed",   "contact_status": "not_interested", "stop_cadence": True},
    "wrong_contact":  {"stage": None,              "contact_status": "wrong_contact",  "stop_cadence": False},
    "out_of_office":  {"stage": None,              "contact_status": "out_of_office",  "stop_cadence": False},
    "opt_out":        {"stage": "Lost / Closed",   "contact_status": "opt_out",        "stop_cadence": True,
                       "extra_contact_props": {"opted_out": "true"}},
}


# ─────────────────────────────────────────────────────────────
# Part D — Sequence auto-enrollment (HubSpot Sequences handles cadence)
# ─────────────────────────────────────────────────────────────

DEFAULT_SEQUENCE_ID = os.getenv("HUBSPOT_SEQUENCE_ID", "685892297")


def enroll_contact_in_sequence(contact_id: str,
                               sequence_id: str | None = None,
                               sender_email: str | None = None) -> dict[str, Any]:
    """
    Enroll a Contact into a HubSpot Sequence so D+2 / D+4 follow-ups fire.

    Returns {"ok": True, "enrollment_id": "..."} on success, {"ok": False, "error": "..."}
    on failure. Never raises — caller treats this as best-effort.
    """
    sid = sequence_id or DEFAULT_SEQUENCE_ID
    if not contact_id or not sid:
        return {"ok": False, "error": "missing contact_id or sequence_id"}

    payload = {
        "sequenceId": int(sid),
        "contactId":  int(contact_id),
    }
    if sender_email:
        payload["senderEmail"] = sender_email

    # HubSpot Sequences enrollment endpoint. Service Keys may not have permission
    # for this — Sequences are user-scoped. We try anyway and surface the error.
    try:
        res = _request("POST", "/automation/v3/sequences/enrollments", json=payload)
        eid = res.get("id") or res.get("enrollmentId")
        log.info("enrolled contact %s in sequence %s → enrollment %s", contact_id, sid, eid)
        return {"ok": True, "enrollment_id": str(eid) if eid else None}
    except HubSpotError as e:
        # Most common failure: 403 because Service Keys can't enroll into personal sequences.
        # In that case the caller should fall back to manual enrollment.
        log.warning("sequence enrollment failed (contact %s, sequence %s): %s",
                    contact_id, sid, e)
        return {"ok": False, "error": str(e)[:300]}

"""
models.py — Pydantic v2 schemas for the Denali BD Automation API.

These are the *API* models, not DB models. They:
  - Validate JSON coming in from the frontend / API clients
  - Shape JSON going out (FastAPI response_model)
  - Document the API surface in /docs (OpenAPI)

Conventions
-----------
- *Create   = input shape for inserting (no id, no created_at)
- *Read     = output shape returned to clients (with id, created_at)
- *Update   = patch-style, all fields optional
- Status fields use Literal to mirror DB CHECK constraints exactly
"""

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ─────────────────────────────────────────────────────────────
# Enum-style literals (must match database.py CHECK constraints)
# ─────────────────────────────────────────────────────────────

OpportunityStatus = Literal["new", "enriched", "drafted", "sent", "replied", "archived"]
ApprovalStatus    = Literal["pending", "approved", "rejected"]
SendStatus        = Literal["queued", "sent", "failed", "bounced", "replied"]
ActorType         = Literal["system", "user", "ai"]
EntityType        = Literal["opportunity", "contact", "draft", "send"]
BounceType        = Literal["hard", "soft"]


# Default config: read from objects (sqlite3.Row supports __getattr__-like access via row[key])
_orm_config = ConfigDict(from_attributes=True)


# ─────────────────────────────────────────────────────────────
# Sub-models
# ─────────────────────────────────────────────────────────────

class ScoreBreakdown(BaseModel):
    """
    Five-dimensional contact score (matches the prototype UI).
    Total = title_relevance + seniority + department + geography + email_verified
          = up to 100.
    """
    title_relevance: int = Field(ge=0, le=35, description="Title fit, 0-35")
    seniority:       int = Field(ge=0, le=25, description="Seniority level, 0-25")
    department:      int = Field(ge=0, le=20, description="Department match, 0-20")
    geography:       int = Field(ge=0, le=10, description="Geography fit, 0-10")
    email_verified:  int = Field(ge=0, le=10, description="Email verification status, 0-10")
    rationale:       str = Field(description="Plain-English explanation of the score")


# ─────────────────────────────────────────────────────────────
# Opportunities
# ─────────────────────────────────────────────────────────────

class OpportunityBase(BaseModel):
    nct_number:       Optional[str] = None
    trial_title:      str
    sponsor_name:     Optional[str] = None
    cro_name:         Optional[str] = None
    therapeutic_area: Optional[str] = None
    phase:            Optional[str] = None
    indication:       Optional[str] = None
    sites_needed:     Optional[int] = None
    geography:        Optional[str] = None
    protocol_start:   Optional[date] = None
    source:           str = "clinwire"


class OpportunityCreate(OpportunityBase):
    """Used when ingesting a new opportunity (e.g. parsed Clinwire row)."""
    raw_data: Optional[dict[str, Any]] = None


class OpportunityRead(OpportunityBase):
    model_config = _orm_config

    id:         int
    status:     OpportunityStatus
    raw_data:   Optional[dict[str, Any]] = None
    created_at: datetime


class OpportunityUpdate(BaseModel):
    """Patch-style update — every field optional."""
    status: Optional[OpportunityStatus] = None
    cro_name: Optional[str] = None
    sites_needed: Optional[int] = None


# ─────────────────────────────────────────────────────────────
# Contacts
# ─────────────────────────────────────────────────────────────

class ContactBase(BaseModel):
    first_name:     Optional[str] = None
    last_name:      Optional[str] = None
    email:          Optional[EmailStr] = None
    email_verified: bool = False
    title:          Optional[str] = None
    seniority:      Optional[str] = None
    department:     Optional[str] = None
    geography:      Optional[str] = None
    linkedin_url:   Optional[str] = None
    apollo_id:      Optional[str] = None


class ContactCreate(ContactBase):
    opportunity_id: int


class ContactRead(ContactBase):
    model_config = _orm_config

    id:              int
    opportunity_id:  int
    contact_score:   Optional[int] = Field(default=None, ge=0, le=100)
    score_reasoning: Optional[ScoreBreakdown] = None
    is_primary:      bool = False
    do_not_contact:  bool = False
    created_at:      datetime


class ContactUpdate(BaseModel):
    """Used for marking primary / do-not-contact, or correcting fields."""
    is_primary:     Optional[bool] = None
    do_not_contact: Optional[bool] = None
    email:          Optional[EmailStr] = None
    email_verified: Optional[bool] = None


# ─────────────────────────────────────────────────────────────
# Email drafts
# ─────────────────────────────────────────────────────────────

class DraftBase(BaseModel):
    subject_line:  str = Field(min_length=1, max_length=200)
    body_text:     str = Field(min_length=1)
    sequence_step: int = Field(default=1, ge=1, le=4)


class DraftCreate(DraftBase):
    """Created by services/ai_engine.py after generating a draft."""
    opportunity_id: int
    contact_id:     int
    prompt_version: Optional[str] = None
    quality_flags:  Optional[list[str]] = None


class DraftRead(DraftBase):
    model_config = _orm_config

    id:               int
    opportunity_id:   int
    contact_id:       int
    prompt_version:   Optional[str] = None
    quality_flags:    Optional[list[str]] = None
    approval_status:  ApprovalStatus
    approved_by:      Optional[str] = None
    approved_at:      Optional[datetime] = None
    edited_body:      Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at:       datetime


class DraftApprove(BaseModel):
    """POST /drafts/{id}/approve body."""
    approved_by: str = Field(min_length=1, description="Name of the rep approving")
    edited_body: Optional[str] = Field(default=None, description="If the rep edited before approving")
    edited_subject: Optional[str] = None
    from_mailbox: Optional[str] = Field(
        default=None,
        description="Email of the Gmail mailbox to send from. Must exist in mailboxes.json. "
                    "If omitted, the draft is approved but not sent.",
    )


class DraftReject(BaseModel):
    """POST /drafts/{id}/reject body."""
    rejected_by:      str = Field(min_length=1)
    rejection_reason: str = Field(min_length=1, max_length=500)


class DraftGenerateRequest(BaseModel):
    """POST /drafts/generate body — kicks off AI generation for a contact."""
    opportunity_id: int
    contact_id:     int
    sequence_step:  int = Field(default=1, ge=1, le=4)


# ─────────────────────────────────────────────────────────────
# Email sends
# ─────────────────────────────────────────────────────────────

class EmailSendRead(BaseModel):
    model_config = _orm_config

    id:          int
    draft_id:    int
    sent_at:     Optional[datetime] = None
    message_id:  Optional[str] = None
    send_status: SendStatus
    bounce_type: Optional[BounceType] = None
    replied_at:  Optional[datetime] = None


# ─────────────────────────────────────────────────────────────
# Activity log
# ─────────────────────────────────────────────────────────────

class ActivityLogRead(BaseModel):
    model_config = _orm_config

    id:          int
    entity_type: EntityType
    entity_id:   Optional[int] = None
    action:      str
    actor_type:  ActorType
    actor_id:    Optional[str] = None
    metadata:    Optional[dict[str, Any]] = None
    created_at:  datetime


# ─────────────────────────────────────────────────────────────
# Composite / dashboard responses
# ─────────────────────────────────────────────────────────────

class OpportunityWithContacts(OpportunityRead):
    """Used by GET /opportunities/{id} — bundles top-scored contacts inline."""
    contacts: list[ContactRead] = []



class DraftWithContext(DraftRead):
    """
    Used by the approval queue UI — bundles enough context (opportunity name,
    contact name, score) so the rep can decide without N+1 fetches.
    """
    opportunity_title: str
    contact_name:      str
    contact_score:     Optional[int] = None

"""
routers/contacts.py — Apollo enrichment + contact scoring + listing.

Endpoints (to be implemented):
    GET    /api/contacts/?opportunity_id=...  → list contacts for an opportunity
    POST   /api/contacts/enrich/{opp_id}      → kick off Apollo enrichment
    GET    /api/contacts/{id}                 → contact detail with score breakdown
    PATCH  /api/contacts/{id}                 → mark primary / DNC / correct fields
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def list_contacts(opportunity_id: int | None = None):
    """Placeholder. Will return list[ContactRead]."""
    return {"items": [], "opportunity_id": opportunity_id, "_note": "stub"}

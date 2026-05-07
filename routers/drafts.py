"""
routers/drafts.py — AI email drafting + the human approval queue.

Endpoints (to be implemented):
    GET    /api/drafts/                  → list drafts (filter by approval_status)
    POST   /api/drafts/generate          → generate a new draft via Anthropic
    GET    /api/drafts/{id}              → draft detail
    POST   /api/drafts/{id}/approve      → approve & queue for send
    POST   /api/drafts/{id}/reject       → reject with reason
    PATCH  /api/drafts/{id}              → edit before approval
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def list_drafts(approval_status: str | None = None):
    """Placeholder. Will return list[DraftWithContext]."""
    return {"items": [], "approval_status": approval_status, "_note": "stub"}

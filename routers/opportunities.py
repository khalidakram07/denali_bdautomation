"""
routers/opportunities.py — Trial opportunity ingestion + listing.

Endpoints (to be implemented):
    GET    /api/opportunities/          → list opportunities
    POST   /api/opportunities/upload    → upload Clinwire CSV
    GET    /api/opportunities/{id}      → opportunity detail (with contacts)
    PATCH  /api/opportunities/{id}      → update status / fields

For now this is a stub so main.py can import it.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def list_opportunities():
    """Placeholder. Will return list[OpportunityRead]."""
    return {"items": [], "_note": "stub — implement in Week 1 hours 11+"}

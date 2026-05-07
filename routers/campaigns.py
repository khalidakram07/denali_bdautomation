"""
routers/campaigns.py — Aggregate views: sends, replies, activity feed, dashboard.

Endpoints (to be implemented):
    GET    /api/campaigns/sends            → list email_sends rows (filter by status)
    GET    /api/campaigns/activity         → live activity log feed (newest first)
    GET    /api/campaigns/dashboard        → counts + funnel for the homepage
    POST   /api/campaigns/sends/{id}/sync  → pull latest delivery status from Instantly
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/activity")
def activity_feed(limit: int = 50):
    """Placeholder. Will return list[ActivityLogRead]."""
    return {"items": [], "limit": limit, "_note": "stub"}

"""
routers/leads.py — Live, read-only category leads from the LeadsCategory Drive folder.

Nothing here is persisted. The dashboard reflects the Google Sheets directly;
only sent emails are saved (see routers/drafts.py).

Endpoints:
    GET  /api/leads/categories         Dropdown options (one per category subfolder)
    GET  /api/leads/category/{name}    Live opportunities + contacts for a category
    POST /api/leads/refresh            Bust the in-memory cache
"""

import logging

from fastapi import APIRouter, HTTPException, Query

from services import leads_categories as lc

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/categories")
def categories(refresh: bool = Query(False)):
    """List selectable categories (subfolders of the LeadsCategory Drive folder)."""
    if not lc.LEADS_CATEGORY_FOLDER_ID:
        raise HTTPException(
            500,
            "LEADS_CATEGORY_FOLDER_ID is not configured. Set it to the Drive "
            "folder ID that contains one subfolder per category.",
        )
    try:
        cats = lc.list_categories(force_refresh=refresh)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        log.exception("Listing categories failed")
        raise HTTPException(502, f"Could not read the LeadsCategory folder: {e}")
    return {
        "categories": [{"name": c["name"]} for c in cats],
        "count": len(cats),
    }


@router.get("/category/{name}")
def category(name: str, refresh: bool = Query(False)):
    """
    Live opportunities (with contacts attached) for one category.
    Reads the category Sheet's Clinwire + Leads tabs on every call (cached briefly).
    """
    try:
        payload = lc.read_category(name, force_refresh=refresh)
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        log.exception("Reading category %r failed", name)
        raise HTTPException(502, f"Could not read category '{name}': {e}")
    return payload


@router.post("/refresh")
def refresh():
    """Clear the in-memory cache so the next read pulls fresh from the Sheets."""
    cleared = lc.refresh_cache()
    return {"cleared": cleared}


from pydantic import BaseModel
from typing import Optional as _Optional


class NewContact(BaseModel):
    trial_id: str
    company:  _Optional[str] = None
    phase:    _Optional[str] = None
    condition: _Optional[str] = None
    name:     str
    title:    _Optional[str] = None
    location: _Optional[str] = None
    business_email: _Optional[str] = None
    personal_email: _Optional[str] = None
    phone:         _Optional[str] = None
    linkedin_url:  _Optional[str] = None
    notes:         _Optional[str] = None
    priority_rank: _Optional[int] = None
    priority_score: _Optional[float] = None


@router.post("/category/{name}/contact")
def add_contact(name: str, body: NewContact):
    """Append a new lead row to the category's Leads tab. Cache is busted so the
    contact appears immediately on the next read."""
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    cats = lc.list_categories()
    match = next((c for c in cats if c["name"].lower() == name.lower()), None)
    if not match:
        raise HTTPException(404, f"Category '{name}' not found")
    from services.google_sheets import append_lead_row
    sheet_row = {
        "Trial ID":           body.trial_id or "",
        "Company":            body.company or "",
        "Phase":              body.phase or "",
        "Condition":          body.condition or "",
        "Priority Rank":      body.priority_rank if body.priority_rank is not None else "",
        "Priority Score":     body.priority_score if body.priority_score is not None else "",
        "Name":               body.name.strip(),
        "Title":              body.title or "",
        "Location":           body.location or "",
        "Business Email":     body.business_email or "",
        "Personal Email":     body.personal_email or "",
        "Email Status":       "Manual",
        "Phone (HQ)":         body.phone or "",
        "LinkedIn URL":       body.linkedin_url or "",
        "Apollo Profile URL": "",
        "Outreach Notes":     body.notes or "",
    }
    ok = append_lead_row(match["sheet_id"], sheet_row)
    if not ok:
        raise HTTPException(502, "Failed to append contact to the Sheet")
    lc._category_data_cache.pop(match["name"], None)
    return {"added": True, "name": body.name}


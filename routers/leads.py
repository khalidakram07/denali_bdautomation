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

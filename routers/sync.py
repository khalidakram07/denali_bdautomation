"""
routers/sync.py — Manual sync trigger for Google Sheets.

POST /api/sync/sheets   pulls the latest data from the configured Google Sheet
                        and upserts opportunities + contacts.
"""

import logging
import os
import time

from fastapi import APIRouter, HTTPException

from services.sheets_sync import sync_sheet

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sheets")
def sync_sheets():
    """
    Trigger a one-shot sync from the Google Sheet whose ID is in the
    GOOGLE_SHEETS_ID env var. Returns counts of rows ingested.
    """
    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    if not sheet_id:
        raise HTTPException(
            500,
            "GOOGLE_SHEETS_ID not configured in .env. Set it to the long ID "
            "from your Sheet's URL (between /d/ and /edit).",
        )

    t0 = time.time()
    try:
        summary = sync_sheet(sheet_id)
    except RuntimeError as e:
        # Credentials problem
        raise HTTPException(500, f"Sheet sync failed: {e}")
    except Exception as e:
        log.exception("Sheet sync failed")
        raise HTTPException(502, f"Sheet sync error: {e}")

    summary["elapsed_seconds"] = round(time.time() - t0, 2)
    return summary

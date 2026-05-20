"""
routers/sync.py — Manual sync trigger + scheduler status for Google Sheets.

POST /api/sync/sheets    pull latest data from the Sheet, upsert opps + contacts
GET  /api/sync/status    auto-sync scheduler state (interval, running, last run)
"""

import logging
import os
import time

from fastapi import APIRouter, HTTPException

from services.sheets_sync import sync_sheet
from services import scheduler as sheets_scheduler

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sheets")
def sync_sheets():
    """Trigger a one-shot sync from the Google Sheet (GOOGLE_SHEETS_ID env var)."""
    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    if not sheet_id:
        raise HTTPException(
            500,
            "GOOGLE_SHEETS_ID not configured. Set it to the long ID from your "
            "Sheet's URL (between /d/ and /edit).",
        )

    t0 = time.time()
    try:
        summary = sync_sheet(sheet_id)
    except RuntimeError as e:
        raise HTTPException(500, f"Sheet sync failed: {e}")
    except Exception as e:
        log.exception("Sheet sync failed")
        raise HTTPException(502, f"Sheet sync error: {e}")

    summary["elapsed_seconds"] = round(time.time() - t0, 2)
    return summary


@router.get("/status")
def sync_status():
    """Returns auto-sync scheduler state: interval_minutes, running, last_run."""
    return sheets_scheduler.get_status()

"""
services/scheduler.py — Background scheduler for periodic Google Sheets sync.

Starts a long-lived asyncio task on app startup. Runs sync_sheet() every
SYNC_INTERVAL_MINUTES minutes. Logs successes/failures to activity_log.
Set SYNC_INTERVAL_MINUTES=0 to disable auto-sync.

Implementation: pure asyncio + run_in_executor (no extra dependency).
Sync calls block IO, so we offload them to the default thread pool so the
event loop stays responsive.
"""

import asyncio
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_last_run: Optional[dict] = None   # for the /api/sync/status endpoint


def get_status() -> dict:
    """Return current scheduler state. Used by the API."""
    return {
        "interval_minutes": int(os.getenv("SYNC_INTERVAL_MINUTES", "60")),
        "running":          _task is not None and not _task.done(),
        "last_run":         _last_run,
    }


async def _run_once():
    """Execute one sync. Runs in a thread because sync_sheet is blocking."""
    global _last_run
    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    if not sheet_id:
        log.warning("Scheduled sync skipped — GOOGLE_SHEETS_ID not set")
        _last_run = {"at": time.time(), "ok": False, "error": "GOOGLE_SHEETS_ID not set"}
        return

    from services.sheets_sync import sync_sheet
    started = time.time()
    try:
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, sync_sheet, sheet_id)
        elapsed = round(time.time() - started, 2)
        log.info("Scheduled sync OK in %ss: %s", elapsed, summary)
        _last_run = {
            "at": time.time(),
            "ok": True,
            "elapsed_seconds": elapsed,
            "summary": summary,
        }
    except Exception as e:
        log.exception("Scheduled sync failed")
        _last_run = {"at": time.time(), "ok": False, "error": str(e)[:200]}


async def _scheduler_loop():
    """Sleep, sync, repeat. Runs forever until cancelled."""
    interval_min = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))
    if interval_min <= 0:
        log.info("Auto-sync disabled (SYNC_INTERVAL_MINUTES=0)")
        return

    interval_sec = interval_min * 60
    log.info("Auto-sync scheduled every %d minutes", interval_min)

    # Quick first sync 60s after startup so the user sees data immediately
    await asyncio.sleep(60)
    await _run_once()

    while True:
        await asyncio.sleep(interval_sec)
        await _run_once()


def start():
    """Kick off the background loop. Safe to call multiple times."""
    global _task
    if _task is not None and not _task.done():
        return  # already running

    interval_min = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))
    if interval_min <= 0:
        log.info("Scheduler not started: SYNC_INTERVAL_MINUTES=0")
        return

    _task = asyncio.create_task(_scheduler_loop(), name="sheets_sync_scheduler")
    log.info("Sheets sync scheduler started (interval=%dmin)", interval_min)


async def stop():
    """Cancel the background loop on shutdown."""
    global _task
    if _task is None or _task.done():
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    log.info("Sheets sync scheduler stopped")

"""
services/drive_templates.py — Load .docx email templates from a Google Drive folder.

The same service account that reads the Sheets is used here. The folder ID
comes from GOOGLE_DRIVE_TEMPLATES_FOLDER_ID env var.

Supports BOTH file types in the folder:
  - Native .docx uploads (MIME application/vnd.openxmlformats-officedocument.wordprocessingml.document)
  - Google Docs (MIME application/vnd.google-apps.document) — exported as .docx on the fly

Templates are cached in memory for CACHE_TTL_SECONDS (default 300 = 5 min).
Call refresh_cache() to bust manually (used by the "Refresh templates" button).
"""

import io
import logging
import os
import time
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from services.google_sheets import _load_credentials   # reuse same auth path

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = int(os.getenv("DRIVE_TEMPLATES_CACHE_SECONDS", "300"))

MIME_DOCX  = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_GDOC  = "application/vnd.google-apps.document"

# In-memory cache: {filename: (bytes, fetched_at)}
_cache: dict[str, tuple[bytes, float]] = {}
_list_cache: tuple[list[dict], float] | None = None


def _drive_client():
    creds = _load_credentials()
    if creds is None:
        return None
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _folder_id() -> Optional[str]:
    fid = os.getenv("GOOGLE_DRIVE_TEMPLATES_FOLDER_ID", "").strip()
    return fid or None


def list_drive_templates(force_refresh: bool = False) -> list[dict]:
    """
    Return list of {filename, display_name, drive_id, mime_type, modified_time}.
    Returns [] if Drive isn't configured.
    """
    global _list_cache
    fid = _folder_id()
    if not fid:
        return []

    if not force_refresh and _list_cache:
        items, t = _list_cache
        if time.time() - t < CACHE_TTL_SECONDS:
            return items

    drive = _drive_client()
    if not drive:
        return []

    try:
        q = (
            f"'{fid}' in parents and trashed=false and "
            f"(mimeType='{MIME_DOCX}' or mimeType='{MIME_GDOC}')"
        )
        result = drive.files().list(
            q=q,
            fields="files(id, name, mimeType, modifiedTime)",
            pageSize=200,
        ).execute()
        files = result.get("files", [])
    except HttpError as e:
        log.error("Drive folder list failed: %s", e)
        return []
    except Exception as e:
        log.error("Drive folder list error: %s", e)
        return []

    items = []
    for f in files:
        name = f["name"]
        # Display name: strip extension, humanize
        display = name.rsplit(".", 1)[0] if "." in name else name
        display = display.replace("_", " ").replace("-", " ").title()
        # Always present the synthetic filename as "<name>.docx" so existing code
        # downstream treats it the same as local files.
        synthetic_name = name if name.lower().endswith(".docx") else f"{name}.docx"
        items.append({
            "filename":      synthetic_name,
            "display_name":  display,
            "drive_id":      f["id"],
            "mime_type":     f["mimeType"],
            "modified_time": f.get("modifiedTime"),
        })
    _list_cache = (items, time.time())
    log.info("Drive: discovered %d templates in folder", len(items))
    return items


def fetch_drive_template_bytes(drive_id: str, mime_type: str) -> Optional[bytes]:
    """
    Return raw .docx bytes for a Drive file.
    - If it's already a .docx, downloads it directly.
    - If it's a Google Doc, exports it as .docx.
    """
    drive = _drive_client()
    if not drive:
        return None

    try:
        if mime_type == MIME_GDOC:
            data = drive.files().export(fileId=drive_id, mimeType=MIME_DOCX).execute()
        else:
            data = drive.files().get_media(fileId=drive_id).execute()
        return data
    except HttpError as e:
        log.error("Drive download failed for %s: %s", drive_id, e)
        return None


def load_drive_template_text(filename: str, force_refresh: bool = False) -> Optional[str]:
    """
    Load template text for a Drive-sourced template. Returns None if not found.
    Uses the same .docx → text logic as template_engine (paragraph join).
    """
    items = list_drive_templates(force_refresh=force_refresh)
    match = next((t for t in items if t["filename"] == filename), None)
    if not match:
        return None

    # Cache by filename
    cached = _cache.get(filename)
    if not force_refresh and cached and (time.time() - cached[1] < CACHE_TTL_SECONDS):
        data = cached[0]
    else:
        data = fetch_drive_template_bytes(match["drive_id"], match["mime_type"])
        if data is None:
            return None
        _cache[filename] = (data, time.time())

    # Parse .docx bytes into paragraph text
    from docx import Document
    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs)


def refresh_cache() -> int:
    """Wipe the in-memory cache. Returns the number of items cleared."""
    global _cache, _list_cache
    n = len(_cache) + (1 if _list_cache else 0)
    _cache = {}
    _list_cache = None
    return n

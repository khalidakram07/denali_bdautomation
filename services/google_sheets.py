"""
services/google_sheets.py — Read/write the BD outreach Google Sheet.

Auth: service account credentials. Local dev reads google_credentials.json
from the project root; production (Render) reads GOOGLE_SERVICE_ACCOUNT_JSON
env var containing the same JSON as a single line.

The Sheet has two tabs we care about:
  - "Clinical Trial Companies"  -> opportunities
  - "Decision Maker Contacts"   -> contacts (joined to opportunities by Trial ID)

Optional third tab "Sent Log" gets appended on every approved send.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger(__name__)

CREDS_PATH = Path(__file__).resolve().parent.parent / "google_credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Tab names (override via env if your sheet uses different titles)
TAB_COMPANIES = os.getenv("SHEETS_TAB_COMPANIES", "Clinical Trial Companies")
TAB_CONTACTS  = os.getenv("SHEETS_TAB_CONTACTS",  "Decision Maker Contacts")
TAB_SENT_LOG  = os.getenv("SHEETS_TAB_SENT_LOG",  "Sent Log")


def _load_credentials() -> Optional[Credentials]:
    """
    Try env var first (production), then file (local dev).
    Returns None if neither is configured — caller should raise/skip.
    """
    env_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if env_json:
        try:
            info = json.loads(env_json)
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception as e:
            log.error("GOOGLE_SERVICE_ACCOUNT_JSON env var invalid: %s", e)
            return None

    if CREDS_PATH.exists():
        try:
            return Credentials.from_service_account_file(str(CREDS_PATH), scopes=SCOPES)
        except Exception as e:
            log.error("google_credentials.json couldn't be loaded: %s", e)
            return None

    log.warning("No Google credentials configured — set GOOGLE_SERVICE_ACCOUNT_JSON or place google_credentials.json")
    return None


def _client() -> gspread.Client:
    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("Google credentials not configured.")
    return gspread.authorize(creds)


def open_sheet(sheet_id: str) -> gspread.Spreadsheet:
    """Open the sheet by ID (the long string in its URL)."""
    return _client().open_by_key(sheet_id)


def read_tab_as_dicts(sheet_id: str, tab_name: str) -> list[dict]:
    """
    Return all data rows from a tab as a list of dicts keyed by header.
    Returns [] if the tab is empty.
    """
    sh = open_sheet(sheet_id)
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        log.warning("Tab not found: %r in sheet %s", tab_name, sheet_id)
        return []
    return ws.get_all_records()   # uses row 1 as headers


def append_to_sent_log(sheet_id: str, row: dict) -> None:
    """
    Append one row to the "Sent Log" tab. Creates the tab if missing.
    `row` should be a dict of column-name -> value.
    """
    sh = open_sheet(sheet_id)
    try:
        ws = sh.worksheet(TAB_SENT_LOG)
    except gspread.WorksheetNotFound:
        # Create with headers from the keys of this row
        ws = sh.add_worksheet(title=TAB_SENT_LOG, rows=1000, cols=max(10, len(row)))
        ws.append_row(list(row.keys()))

    # Match the order of existing headers
    headers = ws.row_values(1)
    if not headers:
        headers = list(row.keys())
        ws.append_row(headers)
    ws.append_row([str(row.get(h, "")) for h in headers])

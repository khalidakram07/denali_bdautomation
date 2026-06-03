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
    Robust to trailing blank header cells and duplicate header names.
    Returns [] if the tab is empty.
    """
    sh = open_sheet(sheet_id)
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        log.warning("Tab not found: %r in sheet %s", tab_name, sheet_id)
        return []
    rows = ws.get_all_values()
    if not rows:
        return []
    raw = [str(h).strip() for h in rows[0]]
    # Drop trailing empty header columns
    while raw and raw[-1] == "":
        raw.pop()
    # Deduplicate empty / repeated header names so we never lose a column
    seen, headers = {}, []
    for i, h in enumerate(raw):
        name = h or f"col_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        headers.append(name)
    out = []
    for r in rows[1:]:
        cells = list(r)[:len(headers)]
        if all((v is None or v == "") for v in cells):
            continue
        d = {headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers))}
        out.append(d)
    return out


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

# Column name written to the Leads tab when a send succeeds. If the column
# doesn't exist yet, it's added on the first call. A non-empty value means
# "this lead has been emailed" and the app filters them out of the dropdowns.
SENT_COLUMN = "Last Sent"


def mark_lead_sent(sheet_id: str, trial_id: str, email: str, when_iso: str,
                   leads_tab: str = None) -> bool:
    """
    Mark a Leads row as emailed by writing `when_iso` into the "Last Sent" column.
    Matches by Trial ID + Business Email or Personal Email (case-insensitive).
    Creates the "Last Sent" column on the fly if it's missing.
    Returns True if a row was updated, False otherwise (silently — never raises).
    """
    if not (sheet_id and trial_id and email):
        return False
    leads_tab = leads_tab or os.getenv("SHEETS_TAB_LEADS", "Leads")
    try:
        sh = open_sheet(sheet_id)
        ws = sh.worksheet(leads_tab)
    except Exception as e:
        log.warning("mark_lead_sent: cannot open %s/%s (%s)", sheet_id, leads_tab, e)
        return False

    try:
        rows = ws.get_all_values()
    except Exception as e:
        log.warning("mark_lead_sent: cannot read %s/%s (%s)", sheet_id, leads_tab, e)
        return False
    if not rows:
        return False

    headers = [h.strip() for h in rows[0]]
    def col(name):
        return headers.index(name) if name in headers else None

    tid_col  = col("Trial ID") or col("Trial IDs")
    biz_col  = col("Business Email")
    pers_col = col("Personal Email")
    if tid_col is None or (biz_col is None and pers_col is None):
        log.warning("mark_lead_sent: missing key columns in Leads tab")
        return False

    # Ensure SENT_COLUMN exists (append as last column if not)
    if SENT_COLUMN in headers:
        sent_col = headers.index(SENT_COLUMN)
    else:
        sent_col = len(headers)
        try:
            ws.update_cell(1, sent_col + 1, SENT_COLUMN)
        except Exception as e:
            log.warning("mark_lead_sent: cannot add header column: %s", e)
            return False

    target_email = email.strip().lower()
    target_tid   = trial_id.strip()

    for i, row in enumerate(rows[1:], start=2):  # gspread is 1-indexed; +1 for header
        row_tid = (row[tid_col] if tid_col < len(row) else "").strip()
        if row_tid != target_tid:
            continue
        row_biz  = (row[biz_col]  if biz_col  is not None and biz_col  < len(row) else "").strip().lower()
        row_pers = (row[pers_col] if pers_col is not None and pers_col < len(row) else "").strip().lower()
        if row_biz == target_email or row_pers == target_email:
            try:
                ws.update_cell(i, sent_col + 1, when_iso)
                return True
            except Exception as e:
                log.warning("mark_lead_sent: write failed for row %d: %s", i, e)
                return False
    log.info("mark_lead_sent: no Leads row matched trial=%s email=%s", target_tid, target_email)
    return False

def update_lead_field(sheet_id: str, trial_id: str, contact_name: str,
                      field: str, value: str, leads_tab: str = None) -> bool:
    """
    Set one field on a lead row in the Leads tab. Matches by Trial ID + Name
    (case-insensitive). Adds the column to the header row if it doesn't exist.
    Returns True if updated, False otherwise (silently — never raises).
    """
    if not (sheet_id and trial_id and contact_name and field):
        return False
    leads_tab = leads_tab or os.getenv("SHEETS_TAB_LEADS", "Leads")
    try:
        sh = open_sheet(sheet_id)
        ws = sh.worksheet(leads_tab)
        rows = ws.get_all_values()
    except Exception as e:
        log.warning("update_lead_field: cannot open %s/%s (%s)", sheet_id, leads_tab, e)
        return False
    if not rows:
        return False

    headers = [h.strip() for h in rows[0]]
    def col(name):
        return headers.index(name) if name in headers else None

    tid_col  = col("Trial ID")
    if tid_col is None:
        tid_col = col("Trial IDs")
    name_col = col("Name")
    if tid_col is None or name_col is None:
        log.warning("update_lead_field: missing Trial ID or Name column")
        return False

    if field in headers:
        target_col = headers.index(field)
    else:
        target_col = len(headers)
        try:
            ws.update_cell(1, target_col + 1, field)
        except Exception as e:
            log.warning("update_lead_field: cannot add header %s: %s", field, e)
            return False

    target_tid  = trial_id.strip()
    target_name = contact_name.strip().lower()
    for i, row in enumerate(rows[1:], start=2):
        row_tid = (row[tid_col] if tid_col < len(row) else "").strip()
        if row_tid != target_tid:
            continue
        row_name = (row[name_col] if name_col < len(row) else "").strip().lower()
        if row_name == target_name:
            try:
                ws.update_cell(i, target_col + 1, value)
                return True
            except Exception as e:
                log.warning("update_lead_field: write failed: %s", e)
                return False
    log.info("update_lead_field: no row matched trial=%s name=%s", target_tid, target_name)
    return False

def append_lead_row(sheet_id: str, row: dict, leads_tab: str = None) -> bool:
    """
    Append a new contact row to the Leads tab. `row` is keyed by column header
    (e.g. {"Name": "Jane Doe", "Trial ID": "NCT123", "Business Email": "x@y.com"}).
    Any header not present in `row` gets a blank cell. Returns True if appended.
    """
    if not (sheet_id and row):
        return False
    leads_tab = leads_tab or os.getenv("SHEETS_TAB_LEADS", "Leads")
    try:
        sh = open_sheet(sheet_id)
        ws = sh.worksheet(leads_tab)
        headers = ws.row_values(1)
    except Exception as e:
        log.warning("append_lead_row: cannot open %s/%s (%s)", sheet_id, leads_tab, e)
        return False
    if not headers:
        log.warning("append_lead_row: Leads tab has no headers")
        return False
    row_values = [str(row.get(h, "")) for h in headers]
    try:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        log.warning("append_lead_row: append failed: %s", e)
        return False


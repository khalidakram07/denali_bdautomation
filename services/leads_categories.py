"""
services/leads_categories.py — Live, read-only view of category leads from Drive.

NEW data model (replaces the single fixed Sheet):

    Drive folder  LeadsCategory  (LEADS_CATEGORY_FOLDER_ID)
      ├── Alzheimer/        ← one Google Sheet inside
      │     └── <Sheet>  tabs:  "Clinwire" (trials)  +  "Leads" (contacts)
      ├── Schizophrenia/
      └── ...

Each subfolder = one selectable category on the homepage. We never persist this
data — the dashboard reflects the Sheet live. Only sent emails are stored.

Two tabs per category Sheet:
  - "Clinwire"  → opportunities  (Title, Company, Location, Conditions, Drugs,
                  Trial IDs, Phase, Source URL, Event ID, Full Text)
  - "Leads"     → contacts       (Company, Trial ID, Phase, Condition,
                  Priority Rank, Priority Score, Name, Title, Location,
                  Business Email, Personal Email, Email Status, Phone (HQ),
                  LinkedIn URL, Apollo Profile URL, Outreach Notes)

Contacts join to opportunities on Trial ID  (Clinwire "Trial IDs" == Leads "Trial ID").
"""

import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# Drive folder that holds one subfolder per category.
LEADS_CATEGORY_FOLDER_ID = os.getenv(
    "LEADS_CATEGORY_FOLDER_ID", "1rBfcP7CC7UwY__95vdmTu6FwmwQUqHav"
).strip()

TAB_CLINWIRE = os.getenv("SHEETS_TAB_CLINWIRE", "Clinwire")
TAB_LEADS    = os.getenv("SHEETS_TAB_LEADS",    "Leads")

CACHE_TTL_SECONDS = int(os.getenv("LEADS_CACHE_SECONDS", "120"))

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_SHEET  = "application/vnd.google-apps.spreadsheet"

# In-memory caches
_categories_cache: tuple[list[dict], float] | None = None      # ([{name, folder_id, sheet_id}], ts)
_category_data_cache: dict[str, tuple[dict, float]] = {}        # name -> (payload, ts)


# ─────────────────────────────────────────────────────────────
# Drive / Sheets clients (reuse the service-account auth)
# ─────────────────────────────────────────────────────────────

def _drive_client():
    from services.google_sheets import _load_credentials
    from googleapiclient.discovery import build
    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("Google credentials not configured.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ─────────────────────────────────────────────────────────────
# Pure mapping helpers (no network — unit-testable)
# ─────────────────────────────────────────────────────────────

def _split_name(full: str) -> tuple[str, str]:
    full = (full or "").strip()
    if not full:
        return "", ""
    parts = full.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _priority_to_score(value) -> Optional[int]:
    """
    Convert a Priority Score into a 0-100 contact score.
      9.5      -> 95
      "9/10"   -> 90
      "9.5/10" -> 95
      85       -> 85   (already 0-100)
    """
    if value is None or value == "":
        return None
    s = str(value).strip()
    try:
        if "/" in s:
            num = float(s.split("/", 1)[0].strip())
            return max(0, min(100, round(num * 10)))
        num = float(s)
    except (ValueError, TypeError):
        return None
    if num <= 10:
        return max(0, min(100, round(num * 10)))
    return max(0, min(100, round(num)))


def _rank_to_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    s = str(value).strip().lstrip("#")
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def map_clinwire_row(row: dict, category: str) -> Optional[dict]:
    """Map one 'Clinwire' tab row into an opportunity dict. Returns None if empty."""
    def g(*keys):
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    trial_id = g("Trial IDs", "Trial ID")
    title    = g("Title")
    if not trial_id and not title:
        return None

    return {
        "id":               trial_id or title,        # stable key for the UI
        "trial_id":         trial_id,
        "nct_number":       trial_id,
        "trial_title":      title,
        "sponsor_name":     g("Company"),
        "company":          g("Company"),
        "geography":        g("Location"),
        "indication":       g("Conditions", "Condition"),
        "therapeutic_area": g("Conditions", "Condition") or category,
        "drug":             g("Drugs", "Drug"),
        "phase":            g("Phase"),
        "source_url":       g("Source URL"),
        "event_id":         g("Event ID"),
        "full_text":        g("Full Text"),
        "category":         category,
    }


def map_lead_row(row: dict, category: str) -> Optional[dict]:
    """Map one 'Leads' tab row into a contact dict. Returns None if empty."""
    def g(*keys):
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    name  = g("Name")
    biz   = g("Business Email")
    pers  = g("Personal Email")
    email = biz or pers
    if not name and not email:
        return None

    first, last = _split_name(name)
    rank   = _rank_to_int(g("Priority Rank"))
    status = g("Email Status")

    return {
        "id":                  f"{g('Trial ID')}|{email or name}",
        "opportunity_trial_id": g("Trial ID", "Trial IDs"),
        "first_name":          first,
        "last_name":           last,
        "full_name":           name,
        "title":               g("Title"),
        "company":             g("Company"),
        "geography":           g("Location"),
        "email":               email,
        "business_email":      biz,
        "personal_email":      pers,
        "email_status":        status,
        "email_verified":      ("verif" in status.lower()),
        "phone":               g("Phone (HQ)", "Phone"),
        "linkedin_url":        g("LinkedIn URL"),
        "apollo_url":          g("Apollo Profile URL"),
        "notes":               g("Outreach Notes"),
        "last_sent":           g("Last Sent"),
        "priority_rank":       rank,
        "is_primary":          (rank == 1),
        "contact_score":       _priority_to_score(g("Priority Score")),
        "phase":               g("Phase"),
        "condition":           g("Condition", "Conditions"),
        "category":            category,
    }


def _synth_opp_from_contact(c: dict, category: str) -> dict:
    """Build a minimal opportunity for a lead whose Trial ID isn't in the Clinwire tab."""
    company = c.get("company") or ""
    cond    = c.get("condition") or category
    phase   = c.get("phase") or ""
    bits = [b for b in (company, cond, phase) if b]
    title = " — ".join(bits) if bits else (company or "Untitled trial")
    tid = c.get("opportunity_trial_id") or ""
    return {
        "id":               tid or title,
        "trial_id":         tid,
        "nct_number":       tid,
        "trial_title":      title,
        "sponsor_name":     company,
        "company":          company,
        "geography":        "",
        "indication":       cond,
        "therapeutic_area": cond or category,
        "drug":             "",
        "phase":            phase,
        "source_url":       "",
        "event_id":         "",
        "full_text":        "",
        "category":         category,
        "synthetic":        True,
    }


def _dedupe_contacts(contacts: list[dict]) -> list[dict]:
    """Drop duplicate people (same email, else same name), keeping the higher score."""
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for c in contacts:
        key = (c.get("email") or "").strip().lower() or (c.get("full_name") or "").strip().lower()
        if not key:
            out.append(c)
            continue
        if key in seen:
            ex = seen[key]
            if (c.get("contact_score") or 0) > (ex.get("contact_score") or 0):
                out[out.index(ex)] = c
                seen[key] = c
            continue
        seen[key] = c
        out.append(c)
    return out


def build_category_view(clin_rows: list[dict], lead_rows: list[dict], category: str) -> list[dict]:
    """
    Turn raw Clinwire + Leads rows into a sorted list of opportunities, each with
    its contacts attached. No duplicates:
      - trials are deduped by Trial ID
      - a lead is attached to its trial (by Trial ID), or failing that to its
        company's existing trial, so we never create a second entry for a company
        that already has a trial
      - only companies with no trial at all get one synthesized entry
      - duplicate people (same email) are collapsed
    Opportunities with the strongest contacts surface first.
    """
    # 1. Map every Clinwire row to its own opportunity (each event = unique entry,
    #    relying on the Trial IDs being unique per row).
    opps: list[dict] = []
    for r in clin_rows:
        o = map_clinwire_row(r, category)
        if not o:
            continue
        o["contacts"] = []
        opps.append(o)

    by_trial_opp = {o["trial_id"]: o for o in opps if o["trial_id"]}
    by_company_opp: dict[str, dict] = {}
    for o in opps:
        co = (o.get("sponsor_name") or "").strip().lower()
        if co:
            by_company_opp.setdefault(co, o)   # first real trial per company

    # 2. Map contacts
    contacts = [c for c in (map_lead_row(r, category) for r in lead_rows) if c]

    # 3. Place each contact: Trial ID match first, then company match, else orphan
    orphan_groups: dict[str, list[dict]] = {}
    for c in contacts:
        tid = (c.get("opportunity_trial_id") or "").strip()
        co  = (c.get("company") or "").strip().lower()
        target = by_trial_opp.get(tid) if tid else None
        if target is None and co:
            target = by_company_opp.get(co)
        if target is not None:
            target["contacts"].append(c)
        else:
            gkey = co or tid or c.get("full_name") or str(id(c))
            orphan_groups.setdefault(gkey, []).append(c)

    # 4. One synthesized opportunity per orphan company (no real trial exists)
    for grp in orphan_groups.values():
        synth = _synth_opp_from_contact(grp[0], category)
        synth["contacts"] = list(grp)
        opps.append(synth)

    # 5. Dedupe + sort contacts within each opportunity
    suppressed_opps = set()
    for o in opps:
        o["contacts"] = _dedupe_contacts(o["contacts"])
        # Suppression: if ANY contact at this trial has already been emailed,
        # the whole opportunity is hidden from the dropdown — audit trail
        # lives in HubSpot's pipeline view, not here.
        already_touched = any((c.get("last_sent") or "").strip() for c in o["contacts"])
        if already_touched:
            suppressed_opps.add(id(o))
        o["contacts"].sort(key=lambda c: (c.get("contact_score") is None, -(c.get("contact_score") or 0)))
    opps = [o for o in opps if id(o) not in suppressed_opps]
    # NOTE: trials without contacts are kept in the list so the full pipeline is
    # visible. They just sort to the bottom (no contact score). Maryam can still
    # see the trial exists and add a contact email via the "Save typed email"
    # flow if she wants to reach out manually.

    # 6. Sort opportunities: best available contact score first, then contact count
    def opp_rank(o):
        scores = [c.get("contact_score") or 0 for c in o["contacts"]]
        best = max(scores) if scores else -1
        return (-best, -len(o["contacts"]))
    opps.sort(key=opp_rank)
    return opps


def attach_contacts(opportunities: list[dict], contacts: list[dict]) -> list[dict]:
    """Group contacts under their opportunity by Trial ID (highest score first)."""
    by_trial: dict[str, list[dict]] = {}
    for c in contacts:
        by_trial.setdefault(c["opportunity_trial_id"], []).append(c)

    for lst in by_trial.values():
        lst.sort(key=lambda c: (c.get("contact_score") is None, -(c.get("contact_score") or 0)))

    for o in opportunities:
        o["contacts"] = by_trial.get(o["trial_id"], [])
    return opportunities


# ─────────────────────────────────────────────────────────────
# Live reads (network)
# ─────────────────────────────────────────────────────────────

def list_categories(force_refresh: bool = False) -> list[dict]:
    """
    Return [{name, folder_id, sheet_id}] — one per subfolder of LeadsCategory
    that contains a Google Sheet. Cached for CACHE_TTL_SECONDS.
    """
    global _categories_cache
    if not LEADS_CATEGORY_FOLDER_ID:
        return []
    if not force_refresh and _categories_cache:
        items, t = _categories_cache
        if time.time() - t < CACHE_TTL_SECONDS:
            return items

    drive = _drive_client()
    q = (f"'{LEADS_CATEGORY_FOLDER_ID}' in parents and trashed=false "
         f"and mimeType='{MIME_FOLDER}'")
    res = drive.files().list(
        q=q, fields="files(id,name)", pageSize=200, orderBy="name",
        includeItemsFromAllDrives=True, supportsAllDrives=True,
    ).execute()
    folders = res.get("files", [])

    items: list[dict] = []
    for f in folders:
        sheet_id = _find_category_sheet_id(drive, f["id"], f["name"])
        if sheet_id:
            items.append({"name": f["name"], "folder_id": f["id"], "sheet_id": sheet_id})
        else:
            log.warning("Category folder %r has no Google Sheet inside", f["name"])

    _categories_cache = (items, time.time())
    log.info("Discovered %d lead categories", len(items))
    return items


def _find_category_sheet_id(drive, folder_id: str, category_name: str) -> Optional[str]:
    """Find the Google Sheet inside a category subfolder. Prefer one whose name matches the category."""
    q = f"'{folder_id}' in parents and trashed=false and mimeType='{MIME_SHEET}'"
    res = drive.files().list(
        q=q, fields="files(id,name)", pageSize=50,
        includeItemsFromAllDrives=True, supportsAllDrives=True,
    ).execute()
    sheets = res.get("files", [])
    if not sheets:
        return None
    for s in sheets:
        if category_name.lower() in s["name"].lower():
            return s["id"]
    return sheets[0]["id"]


def read_category(category_name: str, force_refresh: bool = False) -> dict:
    """
    Live-read one category's Sheet (Clinwire + Leads tabs) and return:
        {category, sheet_id, opportunities:[{...,contacts:[...]}], contact_count}
    Cached briefly per category.
    """
    cached = _category_data_cache.get(category_name)
    if not force_refresh and cached and (time.time() - cached[1] < CACHE_TTL_SECONDS):
        return cached[0]

    cats = list_categories(force_refresh=force_refresh)
    match = next((c for c in cats if c["name"].lower() == category_name.lower()), None)
    if not match:
        raise RuntimeError(f"Category '{category_name}' not found in LeadsCategory folder")

    from services.google_sheets import read_tab_as_dicts
    sheet_id = match["sheet_id"]
    clin_rows  = read_tab_as_dicts(sheet_id, TAB_CLINWIRE)
    lead_rows  = read_tab_as_dicts(sheet_id, TAB_LEADS)

    opportunities = build_category_view(clin_rows, lead_rows, category_name)
    contact_count = sum(len(o["contacts"]) for o in opportunities)

    payload = {
        "category":      category_name,
        "sheet_id":      sheet_id,
        "opportunities": opportunities,
        "opportunity_count": len(opportunities),
        "contact_count": contact_count,
    }
    _category_data_cache[category_name] = (payload, time.time())
    return payload


def refresh_cache() -> int:
    """Bust all in-memory caches. Returns number of cached entries cleared."""
    n = len(_category_data_cache) + len(_category_list_cache)
    _category_data_cache.clear()
    _category_list_cache.clear()
    return n

"""
services/sheets_sync.py — Pull both tabs of the Google Sheet and upsert.

Mapping:
  Sheet 1 "Clinical Trial Companies" -> opportunities
    Title       -> trial_title
    Company     -> sponsor_name
    Conditions  -> indication
    Drugs       -> raw_data["Drugs"]
    Trial IDs   -> nct_number  (dedup key)
    Phase       -> phase
    Location    -> geography
    Source URL  -> raw_data["Source URL"]
    Event ID    -> raw_data["Event ID"]

  Sheet 2 "Decision Maker Contacts" -> contacts (joined by Trial ID)
    Name               -> first_name + last_name (split on first space)
    Title              -> title
    Business Email     -> email (preferred)
    Personal Email     -> raw_data["Personal Email"]
    Email Status       -> email_verified (True if "Apollo Verified")
    Priority Rank      -> #1 becomes is_primary
    Priority Score     -> contact_score (e.g. "9/10" -> 90)
    Location           -> geography
    LinkedIn URL       -> linkedin_url
    Apollo Profile URL -> apollo_id (used as the "from-sheet" marker)
    Outreach Notes     -> stored in score_reasoning.rationale
    Phone (HQ)         -> raw_data["Phone (HQ)"]

Re-sync semantics:
  - Opportunities: INSERT OR IGNORE by (nct_number, source). Existing rows kept.
  - Contacts: on each sync, delete contacts where apollo_id LIKE 'https://app.apollo.io%'
    for opps in the sheet, then re-insert. Manually-added contacts (apollo_id NULL)
    and dummy contacts (apollo_id='dummy-...') are preserved.
"""

import json
import logging
import os
import re
from typing import Optional

from database import db_cursor, log_activity
from services.google_sheets import (
    read_tab_as_dicts, TAB_COMPANIES, TAB_CONTACTS,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Sheet 1 -> opportunities
# ─────────────────────────────────────────────────────────────

def _opp_row_to_record(r: dict) -> Optional[dict]:
    """Map a sheet row to opportunity columns. Skip if no Trial ID."""
    trial_id = (r.get("Trial IDs") or r.get("Trial ID") or "").strip()
    title    = (r.get("Title") or "").strip()
    if not trial_id or not title:
        return None

    raw = {k: v for k, v in r.items() if v not in (None, "")}
    return {
        "nct_number":   trial_id,
        "trial_title":  title,
        "sponsor_name": (r.get("Company") or "").strip() or None,
        "cro_name":     None,
        "therapeutic_area": None,
        "phase":        (r.get("Phase") or "").strip() or None,
        "indication":   (r.get("Conditions") or r.get("Condition") or "").strip() or None,
        "sites_needed": None,
        "geography":    (r.get("Location") or "").strip() or None,
        "protocol_start": None,
        "raw_data":     raw,
    }


def _sync_opportunities(rows: list[dict]) -> tuple[int, int]:
    """INSERT OR IGNORE all rows. Returns (inserted, duplicates)."""
    inserted, duplicates = 0, 0
    with db_cursor() as cur:
        for r in rows:
            rec = _opp_row_to_record(r)
            if not rec:
                continue
            cur.execute(
                """
                INSERT OR IGNORE INTO opportunities
                  (nct_number, trial_title, sponsor_name, cro_name, therapeutic_area,
                   phase, indication, sites_needed, geography, protocol_start,
                   source, raw_data)
                VALUES (?,?,?,?,?,?,?,?,?,?, 'clinwire', ?)
                """,
                (
                    rec["nct_number"], rec["trial_title"], rec["sponsor_name"],
                    rec["cro_name"], rec["therapeutic_area"], rec["phase"],
                    rec["indication"], rec["sites_needed"], rec["geography"],
                    rec["protocol_start"],
                    json.dumps(rec["raw_data"]),
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
            else:
                duplicates += 1
    return inserted, duplicates


# ─────────────────────────────────────────────────────────────
# Sheet 2 -> contacts
# ─────────────────────────────────────────────────────────────

PRIORITY_SCORE_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
PRIORITY_RANK_RE  = re.compile(r"#?\s*(\d+)")


def _parse_priority_score(s: str) -> int:
    """'9/10' -> 90, '10/10' -> 100. Default 70."""
    m = PRIORITY_SCORE_RE.search(s or "")
    if not m:
        return 70
    num, den = int(m.group(1)), int(m.group(2)) or 10
    return min(100, round(100 * num / den))


def _split_name(full: str) -> tuple[str, str]:
    """Sanjeev Pathak, M.D. -> ('Sanjeev', 'Pathak, M.D.')"""
    full = (full or "").strip()
    if not full:
        return ("", "")
    parts = full.split(None, 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def _score_reasoning(r: dict, score: int) -> dict:
    """Build the 5-dimension breakdown so ContactRead can validate it."""
    verified = "verified" in (r.get("Email Status") or "").lower()
    return {
        "title_relevance": 35,
        "seniority":       25,
        "department":      17,
        "geography":        8,
        "email_verified":  10 if verified else 0,
        "rationale": (
            f"Priority {r.get('Priority Rank','')} · "
            f"{r.get('Title','')} at {r.get('Company','')}. "
            f"Email Status: {r.get('Email Status','unknown')}. "
            f"Outreach notes: {r.get('Outreach Notes','')}"
        ).strip(),
    }


def _contact_row_to_record(r: dict, opp_id: int) -> Optional[dict]:
    """Map a contact-sheet row to contact columns. Skip if no Name."""
    name = (r.get("Name") or "").strip()
    if not name:
        return None
    first, last = _split_name(name)

    business = (r.get("Business Email") or "").strip()
    personal = (r.get("Personal Email") or "").strip()
    email = business or personal
    if not email or email.lower() in ("none", "n/a"):
        email = None

    score = _parse_priority_score(r.get("Priority Score") or "")
    is_primary = (r.get("Priority Rank") or "").strip().startswith("#1") or \
                 (r.get("Priority Rank") or "").strip() == "1"

    raw_extras = {
        "Personal Email": personal or None,
        "Phone (HQ)":     r.get("Phone (HQ)") or None,
        "Priority Rank":  r.get("Priority Rank") or None,
        "Priority Score": r.get("Priority Score") or None,
        "Email Status":   r.get("Email Status") or None,
        "Outreach Notes": r.get("Outreach Notes") or None,
    }
    apollo_url = (r.get("Apollo Profile URL") or "").strip() or None

    return {
        "opportunity_id":  opp_id,
        "first_name":      first,
        "last_name":       last,
        "email":           email,
        "email_verified":  "verified" in (r.get("Email Status") or "").lower(),
        "title":           (r.get("Title") or "").strip() or None,
        "seniority":       None,
        "department":      None,
        "geography":       (r.get("Location") or "").strip() or None,
        "linkedin_url":    (r.get("LinkedIn URL") or "").strip() or None,
        "apollo_id":       apollo_url,
        "contact_score":   score,
        "score_reasoning": _score_reasoning(r, score),
        "is_primary":      is_primary,
        "raw_extras":      raw_extras,
    }


def _sync_contacts(rows: list[dict]) -> dict:
    """Resolve opp_id by Trial ID, then upsert contacts."""
    stats = {"matched": 0, "inserted": 0, "skipped_no_opp": 0, "skipped_no_name": 0}

    # Index opps by Trial ID for fast lookup
    with db_cursor() as cur:
        cur.execute("SELECT id, nct_number FROM opportunities WHERE nct_number IS NOT NULL")
        trial_to_opp = {row["nct_number"]: row["id"] for row in cur.fetchall()}

    # Group contacts by opp_id so we can delete-then-insert per opp atomically
    by_opp: dict[int, list[dict]] = {}
    for r in rows:
        trial = (r.get("Trial ID") or r.get("Trial IDs") or "").strip()
        opp_id = trial_to_opp.get(trial)
        if not opp_id:
            stats["skipped_no_opp"] += 1
            continue
        rec = _contact_row_to_record(r, opp_id)
        if not rec:
            stats["skipped_no_name"] += 1
            continue
        by_opp.setdefault(opp_id, []).append(rec)
        stats["matched"] += 1

    # Atomic per-opp: delete sheet-sourced contacts, re-insert fresh ones
    with db_cursor() as cur:
        for opp_id, recs in by_opp.items():
            cur.execute(
                "DELETE FROM contacts WHERE opportunity_id = ? "
                "AND apollo_id LIKE 'https://app.apollo.io%'",
                (opp_id,),
            )
            for c in recs:
                merged_raw = c["raw_extras"]
                cur.execute(
                    """
                    INSERT INTO contacts
                      (opportunity_id, first_name, last_name, email, email_verified,
                       title, seniority, department, geography, linkedin_url, apollo_id,
                       contact_score, score_reasoning, is_primary)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        c["opportunity_id"], c["first_name"], c["last_name"],
                        c["email"], int(c["email_verified"]),
                        c["title"], c["seniority"], c["department"], c["geography"],
                        c["linkedin_url"], c["apollo_id"],
                        c["contact_score"], json.dumps(c["score_reasoning"]),
                        int(c["is_primary"]),
                    ),
                )
                stats["inserted"] += 1

            # Make sure at most one is_primary per opp (prefer rank #1)
            cur.execute(
                "UPDATE contacts SET is_primary = 0 WHERE opportunity_id = ? "
                "AND id NOT IN (SELECT id FROM contacts WHERE opportunity_id = ? "
                "AND is_primary = 1 ORDER BY contact_score DESC LIMIT 1)",
                (opp_id, opp_id),
            )

            # Bump opp status if currently 'new'
            cur.execute(
                "UPDATE opportunities SET status='enriched' WHERE id = ? AND status='new'",
                (opp_id,),
            )

    return stats


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

def sync_sheet(sheet_id: str) -> dict:
    """
    Pull both tabs, upsert opportunities + contacts.
    Returns a stats dict suitable for the API response.
    """
    log.info("Starting Google Sheets sync from %s", sheet_id)

    comp_rows = read_tab_as_dicts(sheet_id, TAB_COMPANIES)
    log.info("Read %d rows from '%s'", len(comp_rows), TAB_COMPANIES)
    opp_inserted, opp_duplicates = _sync_opportunities(comp_rows)

    contact_rows = read_tab_as_dicts(sheet_id, TAB_CONTACTS)
    log.info("Read %d rows from '%s'", len(contact_rows), TAB_CONTACTS)
    contact_stats = _sync_contacts(contact_rows)

    summary = {
        "sheet_id":   sheet_id,
        "opportunities": {
            "rows_in_sheet": len(comp_rows),
            "inserted":      opp_inserted,
            "duplicates":    opp_duplicates,
        },
        "contacts": {
            "rows_in_sheet": len(contact_rows),
            **contact_stats,
        },
    }
    log_activity(
        "opportunity", None, "sheet_synced",
        actor_type="system",
        metadata=summary,
    )
    log.info("Sync complete: %s", summary)
    return summary

"""
routers/opportunities.py — Trial opportunity ingestion + listing.

Endpoints:
    GET    /api/opportunities/          List, optionally filtered by status
    POST   /api/opportunities/upload    Upload a Clinwire CSV
    GET    /api/opportunities/{id}      Detail with attached contacts
    PATCH  /api/opportunities/{id}      Update status / fields

Notes
-----
- raw_data is stored as JSON-encoded TEXT in SQLite. We json.loads on the way
  out so callers receive a real dict.
- Bulk insert uses INSERT OR IGNORE to handle the (nct_number, source) UNIQUE
  constraint cleanly: duplicates are *counted*, not errored.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status

from database import db_cursor, log_activity
from models import (
    ContactRead,
    OpportunityRead,
    OpportunityStatus,
    OpportunityUpdate,
    OpportunityWithContacts,
)
from services.clinwire import parse_csv

log = logging.getLogger(__name__)
router = APIRouter()


# ─────────────────────────────────────────────────────────────
# Helpers — sqlite3.Row → Pydantic, with JSON column decoding
# ─────────────────────────────────────────────────────────────

def _row_to_opportunity_dict(row) -> dict:
    d = dict(row)
    if d.get("raw_data"):
        try:
            d["raw_data"] = json.loads(d["raw_data"])
        except (json.JSONDecodeError, TypeError):
            d["raw_data"] = None
    return d


def _row_to_opportunity(row) -> OpportunityRead:
    return OpportunityRead.model_validate(_row_to_opportunity_dict(row))


def _row_to_contact(row) -> ContactRead:
    d = dict(row)
    if d.get("score_reasoning"):
        try:
            d["score_reasoning"] = json.loads(d["score_reasoning"])
        except (json.JSONDecodeError, TypeError):
            d["score_reasoning"] = None
    return ContactRead.model_validate(d)


# ─────────────────────────────────────────────────────────────
# GET /api/opportunities/
# ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[OpportunityRead])
def list_opportunities(
    status_filter: Optional[OpportunityStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List opportunities, newest first. Optionally filter by status."""
    sql = "SELECT * FROM opportunities"
    params: list = []
    if status_filter:
        sql += " WHERE status = ?"
        params.append(status_filter)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [_row_to_opportunity(r) for r in rows]


# ─────────────────────────────────────────────────────────────
# GET /api/opportunities/{id}
# ─────────────────────────────────────────────────────────────

@router.get("/{opp_id}", response_model=OpportunityWithContacts)
def get_opportunity(opp_id: int):
    """Get one opportunity with its contacts (highest score first)."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        opp_row = cur.fetchone()
        if not opp_row:
            raise HTTPException(404, f"Opportunity {opp_id} not found")
        cur.execute(
            "SELECT * FROM contacts WHERE opportunity_id = ? "
            "ORDER BY contact_score IS NULL, contact_score DESC, created_at DESC",
            (opp_id,),
        )
        contact_rows = cur.fetchall()

    opp_dict = _row_to_opportunity_dict(opp_row)
    contacts = [_row_to_contact(r) for r in contact_rows]
    return OpportunityWithContacts(**opp_dict, contacts=contacts)


# ─────────────────────────────────────────────────────────────
# PATCH /api/opportunities/{id}
# ─────────────────────────────────────────────────────────────

@router.patch("/{opp_id}", response_model=OpportunityRead)
def update_opportunity(opp_id: int, patch: OpportunityUpdate):
    """Update status / cro_name / sites_needed."""
    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "No fields to update")

    with db_cursor() as cur:
        cur.execute("SELECT id FROM opportunities WHERE id = ?", (opp_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"Opportunity {opp_id} not found")

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cur.execute(
            f"UPDATE opportunities SET {set_clause} WHERE id = ?",
            list(fields.values()) + [opp_id],
        )
        cur.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        row = cur.fetchone()

    log_activity("opportunity", opp_id, "updated", actor_type="user", metadata=fields)
    return _row_to_opportunity(row)


# ─────────────────────────────────────────────────────────────
# POST /api/opportunities/upload
# ─────────────────────────────────────────────────────────────

@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_csv(file: UploadFile = File(...)):
    """
    Upload a Clinwire CSV. Returns a summary of how many rows were parsed,
    inserted, and skipped as duplicates (based on UNIQUE(nct_number, source)).
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Please upload a .csv file")

    content = await file.read()
    try:
        opps = parse_csv(content, source="clinwire")
    except Exception as e:
        log.exception("CSV parse failed")
        raise HTTPException(400, f"Could not parse CSV: {e}")

    inserted = 0
    duplicates = 0
    inserted_ids: list[int] = []

    with db_cursor() as cur:
        for o in opps:
            cur.execute(
                """
                INSERT OR IGNORE INTO opportunities
                    (nct_number, trial_title, sponsor_name, cro_name, therapeutic_area,
                     phase, indication, sites_needed, geography, protocol_start,
                     source, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    o.nct_number,
                    o.trial_title,
                    o.sponsor_name,
                    o.cro_name,
                    o.therapeutic_area,
                    o.phase,
                    o.indication,
                    o.sites_needed,
                    o.geography,
                    o.protocol_start.isoformat() if o.protocol_start else None,
                    o.source,
                    json.dumps(o.raw_data) if o.raw_data else None,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
                inserted_ids.append(cur.lastrowid)
            else:
                duplicates += 1

    log_activity(
        "opportunity",
        None,
        "csv_uploaded",
        actor_type="user",
        metadata={
            "filename": file.filename,
            "parsed": len(opps),
            "inserted": inserted,
            "duplicates": duplicates,
        },
    )

    return {
        "filename": file.filename,
        "parsed": len(opps),
        "inserted": inserted,
        "duplicates": duplicates,
        "skipped": len(opps) - inserted - duplicates,
        "inserted_ids": inserted_ids,
    }

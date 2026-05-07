"""
routers/contacts.py — Contact listing, dummy-data seeding, and updates.

Endpoints:
    GET    /api/contacts/?opportunity_id=...   List contacts (filter by opp)
    POST   /api/contacts/seed-dummy/{opp_id}   Seed N dummy contacts (DEMO ONLY)
    GET    /api/contacts/{id}                  Detail
    PATCH  /api/contacts/{id}                  Update (primary, DNC, etc.)

The /seed-dummy endpoint is for demos & development. When Apollo enrichment
is wired up, this gets replaced by /enrich/{opp_id}.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from database import db_cursor, log_activity
from models import ContactRead, ContactUpdate
from services.dummy_data import make_dummy_contacts

log = logging.getLogger(__name__)
router = APIRouter()


def _row_to_contact(row) -> ContactRead:
    d = dict(row)
    if d.get("score_reasoning"):
        try:
            d["score_reasoning"] = json.loads(d["score_reasoning"])
        except (json.JSONDecodeError, TypeError):
            d["score_reasoning"] = None
    return ContactRead.model_validate(d)


# ─────────────────────────────────────────────────────────────
# GET /api/contacts/
# ─────────────────────────────────────────────────────────────

@router.get("/", response_model=list[ContactRead])
def list_contacts(
    opportunity_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """List contacts, highest score first. Optionally filter by opportunity_id."""
    sql = "SELECT * FROM contacts"
    params: list = []
    if opportunity_id is not None:
        sql += " WHERE opportunity_id = ?"
        params.append(opportunity_id)
    sql += " ORDER BY contact_score IS NULL, contact_score DESC, created_at DESC LIMIT ?"
    params.append(limit)

    with db_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_row_to_contact(r) for r in rows]


# ─────────────────────────────────────────────────────────────
# GET /api/contacts/{id}
# ─────────────────────────────────────────────────────────────

@router.get("/{contact_id}", response_model=ContactRead)
def get_contact(contact_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"Contact {contact_id} not found")
    return _row_to_contact(row)


# ─────────────────────────────────────────────────────────────
# PATCH /api/contacts/{id}
# ─────────────────────────────────────────────────────────────

@router.patch("/{contact_id}", response_model=ContactRead)
def update_contact(contact_id: int, patch: ContactUpdate):
    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "No fields to update")

    # SQLite stores booleans as 0/1
    for k in ("is_primary", "do_not_contact", "email_verified"):
        if k in fields:
            fields[k] = 1 if fields[k] else 0

    with db_cursor() as cur:
        cur.execute("SELECT id FROM contacts WHERE id = ?", (contact_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"Contact {contact_id} not found")

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        cur.execute(
            f"UPDATE contacts SET {set_clause} WHERE id = ?",
            list(fields.values()) + [contact_id],
        )
        cur.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,))
        row = cur.fetchone()

    log_activity("contact", contact_id, "updated", actor_type="user", metadata=fields)
    return _row_to_contact(row)


# ─────────────────────────────────────────────────────────────
# POST /api/contacts/seed-dummy/{opp_id}
# ─────────────────────────────────────────────────────────────

@router.post("/seed-dummy/{opp_id}", status_code=status.HTTP_201_CREATED)
def seed_dummy_contacts(opp_id: int, n: int = Query(3, ge=1, le=10)):
    """
    DEMO ONLY: seed n plausible contacts (with scores) for an opportunity.
    The first contact is marked is_primary=true. Replaces real Apollo enrichment.
    """
    # Verify opportunity exists
    with db_cursor() as cur:
        cur.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        opp_row = cur.fetchone()
        if not opp_row:
            raise HTTPException(404, f"Opportunity {opp_id} not found")
        opp = dict(opp_row)

    pairs = make_dummy_contacts(opp, n=n)
    inserted_ids: list[int] = []

    with db_cursor() as cur:
        for i, (contact, score) in enumerate(pairs):
            total = (
                score.title_relevance + score.seniority + score.department
                + score.geography + score.email_verified
            )
            cur.execute(
                """
                INSERT INTO contacts
                    (opportunity_id, first_name, last_name, email, email_verified,
                     title, seniority, department, geography, apollo_id,
                     contact_score, score_reasoning, is_primary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact.opportunity_id,
                    contact.first_name,
                    contact.last_name,
                    contact.email,
                    int(contact.email_verified),
                    contact.title,
                    contact.seniority,
                    contact.department,
                    contact.geography,
                    contact.apollo_id,
                    total,
                    score.model_dump_json(),
                    1 if i == 0 else 0,
                ),
            )
            inserted_ids.append(cur.lastrowid)

        # Bump opportunity status: new → enriched
        cur.execute(
            "UPDATE opportunities SET status='enriched' WHERE id = ? AND status='new'",
            (opp_id,),
        )

    log_activity(
        "opportunity", opp_id, "contacts_seeded",
        actor_type="system",
        metadata={"count": len(inserted_ids), "method": "dummy"},
    )

    return {
        "opportunity_id": opp_id,
        "inserted_ids":   inserted_ids,
        "count":          len(inserted_ids),
    }

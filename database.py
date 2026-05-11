"""
database.py — SQLite connection + schema for Denali Health BD Automation.

What this module gives you:
  - get_connection()      → a sqlite3.Connection with sane defaults
  - db_cursor()           → context manager that auto-commits / rolls back
  - init_db()             → creates all tables + indexes if missing (idempotent)
  - log_activity(...)     → tiny helper to insert into activity_log

Run `python database.py` to initialise the DB from the command line.

Notes
-----
- sqlite3 is synchronous. FastAPI is async. For Phase 1 (low traffic, single
  rep approving emails) this is fine — connections are short-lived. If we
  outgrow it, swap in `aiosqlite` later without changing the schema.
- Foreign keys are OFF by default in SQLite. We turn them on per connection.
- JSON-typed columns (raw_data, score_reasoning, quality_flags, metadata) are
  stored as TEXT. The application layer is responsible for json.dumps / loads.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./denali.db")


# ─────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """
    Open a new SQLite connection.

    - row_factory = sqlite3.Row      → access columns by name (row["nct_number"])
    - PRAGMA foreign_keys = ON       → enforce FKs (off by default in SQLite)
    """
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_cursor() -> Iterator[sqlite3.Cursor]:
    """
    Context manager that yields a cursor, commits on success, rolls back on error.

    Usage:
        with db_cursor() as cur:
            cur.execute("INSERT INTO ...", (...))
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# Schema — all 5 tables + indexes
# ─────────────────────────────────────────────────────────────

SCHEMA = """
-- ── opportunities ────────────────────────────────────────────
-- One row per clinical trial opportunity ingested from Clinwire.
CREATE TABLE IF NOT EXISTS opportunities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    nct_number        TEXT,
    trial_title       TEXT NOT NULL,
    sponsor_name      TEXT,
    cro_name          TEXT,
    therapeutic_area  TEXT,
    phase             TEXT,
    indication        TEXT,
    sites_needed      INTEGER,
    geography         TEXT,
    protocol_start    DATE,
    source            TEXT NOT NULL DEFAULT 'clinwire',
    status            TEXT NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new', 'enriched', 'drafted', 'sent', 'replied', 'archived')),
    raw_data          TEXT,                              -- JSON: original record
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (nct_number, source)
);

CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_opportunities_nct    ON opportunities(nct_number);


-- ── contacts ─────────────────────────────────────────────────
-- People at the sponsor / CRO who might own site selection.
-- Many contacts per opportunity. Scored by services/scorer.py.
CREATE TABLE IF NOT EXISTS contacts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id    INTEGER NOT NULL,
    first_name        TEXT,
    last_name         TEXT,
    email             TEXT,
    email_verified    INTEGER NOT NULL DEFAULT 0,        -- 0 / 1
    title             TEXT,
    seniority         TEXT,
    department        TEXT,
    geography         TEXT,
    linkedin_url      TEXT,
    apollo_id         TEXT,
    contact_score     INTEGER,                           -- 0-100
    score_reasoning   TEXT,                              -- JSON: dimension breakdown + rationale
    is_primary        INTEGER NOT NULL DEFAULT 0,        -- 0 / 1
    do_not_contact    INTEGER NOT NULL DEFAULT 0,        -- 0 / 1
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contacts_opportunity ON contacts(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email       ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_score       ON contacts(contact_score DESC);


-- ── email_drafts ─────────────────────────────────────────────
-- AI-generated email drafts awaiting human approval.
-- sequence_step: 1 = first touch, 2/3/4 = follow-ups (Day 5/11/20).
CREATE TABLE IF NOT EXISTS email_drafts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id    INTEGER NOT NULL,
    contact_id        INTEGER NOT NULL,
    sequence_step     INTEGER NOT NULL DEFAULT 1,
    subject_line      TEXT NOT NULL,
    body_text         TEXT NOT NULL,
    prompt_version    TEXT,                              -- e.g., 'email_draft_v1'
    quality_flags     TEXT,                              -- JSON list of flag strings
    approval_status   TEXT NOT NULL DEFAULT 'pending'
                      CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_by       TEXT,
    approved_at       TIMESTAMP,
    edited_body       TEXT,                              -- if rep edited before approving
    rejection_reason  TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE,
    FOREIGN KEY (contact_id)     REFERENCES contacts(id)      ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drafts_opportunity ON email_drafts(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_drafts_contact     ON email_drafts(contact_id);
CREATE INDEX IF NOT EXISTS idx_drafts_status      ON email_drafts(approval_status);


-- ── email_sends ──────────────────────────────────────────────
-- One row per actual send attempt (Gmail SMTP, Instantly, etc.).
-- A draft can have multiple sends (e.g., retries) but usually just one.
CREATE TABLE IF NOT EXISTS email_sends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id            INTEGER NOT NULL,
    recipient_email     TEXT,                            -- actual To: address used (may differ from contact.email if overridden)
    from_mailbox_email  TEXT,                            -- which mailbox sent it
    is_to_overridden    INTEGER NOT NULL DEFAULT 0,      -- 1 if recipient differs from contact.email
    sent_at             TIMESTAMP,
    message_id          TEXT,                            -- Message-ID returned by Gmail SMTP / Instantly
    send_status   TEXT NOT NULL DEFAULT 'queued'
                  CHECK (send_status IN ('queued', 'sent', 'failed', 'bounced', 'replied')),
    bounce_type   TEXT,                                  -- 'hard' | 'soft' | NULL
    replied_at    TIMESTAMP,
    FOREIGN KEY (draft_id) REFERENCES email_drafts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sends_draft  ON email_sends(draft_id);
CREATE INDEX IF NOT EXISTS idx_sends_status ON email_sends(send_status);


-- ── activity_log ─────────────────────────────────────────────
-- Universal append-only log. Powers the live activity feed in the UI.
-- entity_type:  'opportunity' | 'contact' | 'draft' | 'send'
-- actor_type:   'system' | 'user' | 'ai'
CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER,
    action      TEXT NOT NULL,                           -- 'created' | 'scored' | 'approved' | ...
    actor_type  TEXT NOT NULL DEFAULT 'system',
    actor_id    TEXT,                                    -- user name OR model name
    metadata    TEXT,                                    -- JSON blob
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_activity_entity  ON activity_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC);
"""


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables and indexes if they don't exist. Idempotent — safe to call on every startup."""
    db_dir = Path(DATABASE_PATH).resolve().parent
    db_dir.mkdir(parents=True, exist_ok=True)

    with db_cursor() as cur:
        cur.executescript(SCHEMA)

        # ── Non-destructive migrations for older databases ───────────
        # Each ALTER is idempotent: if the column already exists we
        # silently skip; any other error re-raises.
        migrations = [
            "ALTER TABLE email_sends ADD COLUMN recipient_email     TEXT",
            "ALTER TABLE email_sends ADD COLUMN from_mailbox_email  TEXT",
            "ALTER TABLE email_sends ADD COLUMN is_to_overridden    INTEGER NOT NULL DEFAULT 0",
        ]
        for stmt in migrations:
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise


def log_activity(
    entity_type: str,
    entity_id: int | None,
    action: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """
    Insert one row into activity_log. Use this from every service/router
    whenever something interesting happens - that feed is the UI's heartbeat.

    Example:
        log_activity('draft', draft_id, 'approved',
                     actor_type='user', actor_id='Maryam')
    """
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO activity_log
                (entity_type, entity_id, action, actor_type, actor_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                action,
                actor_type,
                actor_id,
                json.dumps(metadata) if metadata is not None else None,
            ),
        )


# -------------------------------------------------------------
# CLI entry-point
# -------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {Path(DATABASE_PATH).resolve()}")

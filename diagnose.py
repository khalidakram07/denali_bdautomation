"""
diagnose.py - One-shot DB + API check.
Run from project root:   python diagnose.py
"""
import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

from pathlib import Path

# Make sure we're in the project dir
os.chdir(Path(__file__).resolve().parent)

DB_PATH = os.getenv("DATABASE_PATH", "./denali.db")
PW = os.getenv("APP_PASSWORD", "demo123")
BASE = "http://127.0.0.1:8000"


def section(title):
    print("\n" + "═" * 60)
    print(f"  {title}")
    print("═" * 60)


# ── PART 1: Direct DB inspection ────────────────────────────
section("1. DIRECT DB INSPECTION")
if not Path(DB_PATH).exists():
    print(f"  ❌ DB file not found: {DB_PATH}")
    sys.exit(1)
print(f"  DB file: {Path(DB_PATH).resolve()}  ({Path(DB_PATH).stat().st_size} bytes)")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")

# Total counts
counts = {}
for t in ["opportunities", "contacts", "email_drafts", "email_sends"]:
    counts[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
print(f"  Table sizes: {counts}")

# Per-opp contact counts
print()
print("  Contacts per opportunity:")
rows = conn.execute(
    "SELECT opportunity_id, COUNT(*) AS n FROM contacts GROUP BY opportunity_id ORDER BY opportunity_id"
).fetchall()
if not rows:
    print("    (none — NO contacts exist in DB for ANY opportunity)")
else:
    for r in rows:
        print(f"    opp #{r['opportunity_id']:3d}: {r['n']} contacts")

# Opportunity #1 details
print()
opp1 = conn.execute("SELECT id, trial_title, status FROM opportunities WHERE id = 1").fetchone()
if opp1:
    print(f"  Opp #1: {opp1['trial_title'][:50]}  status={opp1['status']}")
    print(f"  Contacts attached to opp #1 (direct SQL):")
    c_rows = conn.execute(
        "SELECT id, opportunity_id, first_name, last_name, email, contact_score "
        "FROM contacts WHERE opportunity_id = 1"
    ).fetchall()
    if not c_rows:
        print("    (ZERO — seeds aren't persisting OR they're going to wrong opp_id)")
        # Show ALL contacts so we can see if they went to wrong opp_id
        print()
        print("  ALL contacts in DB (any opp):")
        all_c = conn.execute(
            "SELECT id, opportunity_id, first_name, last_name FROM contacts ORDER BY id DESC LIMIT 10"
        ).fetchall()
        for c in all_c:
            print(f"    contact #{c['id']:3d}  opp_id={c['opportunity_id']}  {c['first_name']} {c['last_name']}")
    else:
        for c in c_rows:
            print(f"    contact #{c['id']:3d}  score={c['contact_score']}  {c['first_name']} {c['last_name']} <{c['email']}>")
else:
    print("  ⚠ No opportunity with id=1 exists in DB")

conn.close()


# ── PART 2: API check ──────────────────────────────────────
section("2. API CHECK (requires server running)")
auth = "Basic " + base64.b64encode(f"maryam:{PW}".encode()).decode()
headers = {"Authorization": auth}


def api_get(path):
    req = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.reason
    except Exception as e:
        return None, str(e)


print(f"  Hitting {BASE} (auth user=maryam pw={PW!r})")
print()

code, data = api_get("/health")
print(f"  GET /health                          → {code}  {data if isinstance(data, dict) else ''}")

code, data = api_get("/api/contacts/?opportunity_id=1")
n = len(data) if isinstance(data, list) else "?"
print(f"  GET /api/contacts/?opportunity_id=1  → {code}  count={n}")
if isinstance(data, list) and data:
    for c in data[:3]:
        print(f"      contact #{c['id']}  {c['first_name']} {c['last_name']}  opp_id={c['opportunity_id']}")

code, data = api_get("/api/opportunities/1")
if isinstance(data, dict):
    print(f"  GET /api/opportunities/1             → {code}  contacts={len(data.get('contacts', []))}")
    print(f"      trial:  {data.get('trial_title', '')[:50]}")
    print(f"      status: {data.get('status')}")
else:
    print(f"  GET /api/opportunities/1             → {code}  {data}")


# ── PART 3: Verdict ─────────────────────────────────────────
section("3. VERDICT")
print("""
  Compare the numbers above:

  Case A: 'Contacts per opportunity' shows opp #1 with N>0
          AND  /api/opportunities/1 shows contacts=N
       →  Backend fine. Bug is in the FRONTEND.
          Fix: hard-refresh browser (Ctrl+Shift+R), then re-check.

  Case B: DB shows opp #1 with N>0
          BUT  /api/opportunities/1 shows contacts=0
       →  Backend bug in get_opportunity. Send me this output.

  Case C: DB shows opp #1 with ZERO contacts
          AND  'ALL contacts in DB' shows recent contacts went to a
               DIFFERENT opp_id (e.g. 32 or 43)
       →  state.oppId mismatch in the frontend. Bug in onSeed.

  Case D: DB shows ZERO contacts anywhere
       →  Seed isn't persisting. Could be a stale pycache - delete
          all __pycache__/ folders and restart the server.
""")

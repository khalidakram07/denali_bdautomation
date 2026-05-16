"""
scripts/backup_to_drive.py - Safe SQLite backup to a Google Drive folder.

Run manually:
    python scripts/backup_to_drive.py

Run on a schedule (Windows Task Scheduler, every 30 min):
    Trigger: every 30 minutes
    Action:  C:\\path\\to\\python.exe  C:\\path\\to\\backup_to_drive.py

It uses SQLite's online backup API (NOT a raw file copy), so it works even
while the app is running and writing — no corruption risk.

Configure where backups go via env var BACKUP_DIR. Set it to your Google
Drive folder so the file syncs automatically:

    Windows:  set BACKUP_DIR=C:\\Users\\44791\\My Drive\\denali_backups
    macOS:    export BACKUP_DIR="$HOME/Library/CloudStorage/GoogleDrive-.../My Drive/denali_backups"

Retention: keeps the last N backups (default 20), deletes older ones.
"""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Allow `python scripts/backup_to_drive.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

DB_PATH    = Path(os.getenv("DATABASE_PATH", "./denali.db")).resolve()
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "./backups")).resolve()
RETENTION  = int(os.getenv("BACKUP_RETENTION", "20"))


def main() -> int:
    if not DB_PATH.exists():
        print(f"[backup] source DB does not exist: {DB_PATH}", file=sys.stderr)
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"denali_{timestamp}.db"

    # Use SQLite's online backup API - safe to run while DB is in use.
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    size_kb = target.stat().st_size / 1024
    print(f"[backup] wrote {target}  ({size_kb:.1f} KB)")

    # Retention: keep only the most recent N
    backups = sorted(
        BACKUP_DIR.glob("denali_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    deleted = 0
    for old in backups[RETENTION:]:
        try:
            old.unlink()
            deleted += 1
        except OSError:
            pass
    if deleted:
        print(f"[backup] pruned {deleted} old backup(s), kept {min(len(backups), RETENTION)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
services/clinwire.py — Parse Clinwire CSV exports into OpportunityCreate models.

Clinwire's CSV layout varies by report template, so this parser is forgiving:
  - Headers are normalised (lowercased, whitespace squashed)
  - Each canonical field has a list of accepted header variants in COLUMN_MAP
  - Integer fields tolerate strings like "30+", "≈25", "30 sites"
  - Dates accept several common formats (and fall back to None on failure)
  - The full original row is preserved in `raw_data` so nothing is lost

If a real Clinwire export uses headers we don't recognise, just add them to
COLUMN_MAP — no code changes needed elsewhere.
"""

import csv
import io
import logging
import re
from datetime import date, datetime
from typing import Optional

from models import OpportunityCreate

log = logging.getLogger(__name__)

# Canonical field → list of accepted header variants (matched case-insensitively).
# Supports BOTH the original Clinwire export format AND the "extraction agent"
# format (Title / Company / Conditions / Drugs / Trial IDs / Event ID / Full Text).
COLUMN_MAP: dict[str, list[str]] = {
    # "nct_number" is used as the dedup key. For the agent output, this is the
    # Clinwire "Trial IDs" (e.g. ZWPSAAFFC29B) — not a real NCT, but unique per trial.
    "nct_number":       ["nct number", "nct id", "nct", "nctid", "clinicaltrials.gov id",
                         "trial ids", "trial id", "clinwire id"],
    "trial_title":      ["trial title", "study title", "title", "official title", "brief title"],
    "sponsor_name":     ["sponsor", "sponsor name", "lead sponsor", "company"],
    "cro_name":         ["cro", "cro name", "contract research organization", "cro (european arm)"],
    "therapeutic_area": ["therapeutic area", "ta", "therapy area"],
    "phase":            ["phase", "trial phase", "study phase"],
    "indication":       ["indication", "condition", "disease", "conditions"],
    "sites_needed":     ["sites needed", "site count", "number of sites", "n sites", "sites"],
    "geography":        ["geography", "regions", "countries", "location", "geo"],
    "protocol_start":   ["protocol start", "start date", "estimated start date", "study start"],
}


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _build_header_index(headers: list[str]) -> dict[str, int]:
    """Map normalised header → column index."""
    return {_normalize(h): i for i, h in enumerate(headers)}


def _find_field(row: list[str], header_idx: dict[str, int], variants: list[str]) -> Optional[str]:
    """Return the first non-empty value matching any header variant, or None."""
    for v in variants:
        idx = header_idx.get(v)
        if idx is not None and idx < len(row):
            val = (row[idx] or "").strip()
            if val:
                return val
    return None


def _parse_int(s: Optional[str]) -> Optional[int]:
    """Pull the first integer out of a messy string. '30+' → 30, '≈25 sites' → 25."""
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else None


def _parse_date(s: Optional[str]) -> Optional[date]:
    """Try several common date formats; log + return None on failure."""
    if not s:
        return None
    s = s.strip()
    formats = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y-%m",
        "%b %Y",       # 'Mar 2026'
        "%B %Y",       # 'March 2026'
        "%d-%b-%Y",    # '15-Mar-2026'
    )
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    log.warning("Could not parse date: %r", s)
    return None


def parse_csv(content: bytes | str, source: str = "clinwire") -> list[OpportunityCreate]:
    """
    Parse a Clinwire CSV (bytes or string) into a list of OpportunityCreate.

    Behaviour:
      - Skips rows missing trial_title (required by the schema)
      - Skips fully blank rows
      - Stores the original row in raw_data so we can re-derive any field later
      - Returns an empty list if the CSV has no data rows
    """
    if isinstance(content, bytes):
        # Excel often saves CSV with a UTF-8 BOM — strip it
        content = content.decode("utf-8-sig")

    reader = csv.reader(io.StringIO(content))
    headers = next(reader, None)
    if not headers:
        return []

    header_idx = _build_header_index(headers)
    opportunities: list[OpportunityCreate] = []

    for row_num, row in enumerate(reader, start=2):
        if not any((c or "").strip() for c in row):
            continue  # blank row

        title = _find_field(row, header_idx, COLUMN_MAP["trial_title"])
        if not title:
            log.warning("Row %d skipped — no trial_title found", row_num)
            continue

        # Preserve the original row verbatim
        raw = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}

        opportunities.append(OpportunityCreate(
            nct_number       = _find_field(row, header_idx, COLUMN_MAP["nct_number"]),
            trial_title      = title,
            sponsor_name     = _find_field(row, header_idx, COLUMN_MAP["sponsor_name"]),
            cro_name         = _find_field(row, header_idx, COLUMN_MAP["cro_name"]),
            therapeutic_area = _find_field(row, header_idx, COLUMN_MAP["therapeutic_area"]),
            phase            = _find_field(row, header_idx, COLUMN_MAP["phase"]),
            indication       = _find_field(row, header_idx, COLUMN_MAP["indication"]),
            sites_needed     = _parse_int (_find_field(row, header_idx, COLUMN_MAP["sites_needed"])),
            geography        = _find_field(row, header_idx, COLUMN_MAP["geography"]),
            protocol_start   = _parse_date(_find_field(row, header_idx, COLUMN_MAP["protocol_start"])),
            source           = source,
            raw_data         = raw,
        ))

    return opportunities

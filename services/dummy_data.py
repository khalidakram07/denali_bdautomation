"""
services/dummy_data.py — Generate plausible contacts + scores for an opportunity.

Used during demo / dev when Apollo isn't connected. Picks names from a
small pool, picks titles based on a relevance ladder, and assigns scores
deterministically (seeded by opportunity id) so the demo is reproducible.
"""

import random
import re
from typing import Optional

from models import ContactCreate, ScoreBreakdown

FIRST_NAMES = [
    "David", "Sarah", "Michael", "Jennifer", "James", "Lisa",
    "Robert", "Maria", "John", "Patricia", "Daniel", "Aisha",
]

LAST_NAMES = [
    "Collins", "Chen", "Patel", "Kowalski", "Mueller", "Romero",
    "Tanaka", "Anderson", "Singh", "Reyes", "Okonkwo", "Park",
]

# (title, seniority bucket, department, title_pts /35, seniority_pts /25)
# Index 0 = best-fit.
TITLE_LADDER = [
    ("VP, Site Identification & Alliance Management", "VP",       "Operations",            35, 25),
    ("Director, Site Strategy",                       "Director", "Operations",            30, 20),
    ("Senior Manager, Site Selection",                "Manager",  "Operations",            25, 15),
    ("Head of Clinical Operations",                   "VP",       "Operations",            32, 25),
    ("VP, Clinical Development",                      "VP",       "Clinical Development",  22, 25),
]

CITIES = [
    ("Philadelphia, PA",  "United States"),
    ("Boston, MA",        "United States"),
    ("San Francisco, CA", "United States"),
    ("Cambridge, UK",     "Europe"),
    ("Frankfurt, DE",     "Europe"),
    ("Zurich, CH",        "Europe"),
]


def _email_domain(company_name: str) -> str:
    """'Precision for Medicine PLC' → 'precisionformedicine.com'."""
    s = re.sub(r"\b(plc|inc|corp|co|llc|ltd|gmbh)\b\.?", "", company_name.lower())
    s = re.sub(r"[^a-z]+", "", s)
    return (s or "example") + ".com"


def _geo_fit(opp_geography: str, contact_region: str) -> int:
    """Score 0-10 based on whether the contact's region overlaps the trial's footprint."""
    g = (opp_geography or "").lower()
    if not g:
        return 5
    if "global" in g:
        return 10
    if contact_region == "United States" and ("us" in g or "united states" in g or "america" in g):
        return 10
    if contact_region == "Europe" and ("europe" in g or "eu" in g or "uk" in g):
        return 10
    return 5


def make_dummy_contacts(
    opp: dict,
    n: int = 3,
    rng: Optional[random.Random] = None,
) -> list[tuple[ContactCreate, ScoreBreakdown]]:
    """
    Build n plausible (ContactCreate, ScoreBreakdown) pairs for a given opportunity.

    Deterministic: seeded by opportunity id so the same opportunity always
    yields the same contacts (good for stable demos).
    """
    rng = rng or random.Random(opp.get("id", 0))
    company = opp.get("cro_name") or opp.get("sponsor_name") or "Unknown Co"
    domain = _email_domain(company)

    pairs: list[tuple[ContactCreate, ScoreBreakdown]] = []
    used_names: set[tuple[str, str]] = set()

    for i in range(n):
        # Pick unique name
        for _ in range(20):
            first = rng.choice(FIRST_NAMES)
            last  = rng.choice(LAST_NAMES)
            if (first, last) not in used_names:
                used_names.add((first, last))
                break

        title_idx = min(i, len(TITLE_LADDER) - 1)
        title, seniority, dept, title_pts, sen_pts = TITLE_LADDER[title_idx]
        city, region = rng.choice(CITIES)

        geo_pts   = _geo_fit(opp.get("geography") or "", region)
        dept_pts  = 17 if dept == "Operations" else 10
        email_pts = 0   # dummy contacts are never verified

        score = ScoreBreakdown(
            title_relevance=title_pts,
            seniority=sen_pts,
            department=dept_pts,
            geography=geo_pts,
            email_verified=email_pts,
            rationale=(
                f"{first} {last} is {title} at {company}. "
                f"Title is {'a direct match' if title_pts >= 30 else 'adjacent'} "
                f"for site selection outreach. Geography ({city}) "
                f"{'aligns with' if geo_pts == 10 else 'is loosely relevant to'} "
                f"the trial's planned footprint. "
                f"Email unverified — confirm before send."
            ),
        )

        contact = ContactCreate(
            opportunity_id = opp["id"],
            first_name     = first,
            last_name      = last,
            email          = f"{first[0].lower()}.{last.lower()}@{domain}",
            email_verified = False,
            title          = title,
            seniority      = seniority,
            department     = dept,
            geography      = city,
            apollo_id      = f"dummy-{opp['id']}-{i}",
        )
        pairs.append((contact, score))

    return pairs

"""
services/ai_engine.py — Anthropic-powered email draft generation.

generate_draft(opp, contact) reads prompts/email_draft.txt, fills it with
the opportunity + contact context, calls Claude, and returns a DraftCreate.

Falls back to a hardcoded demo draft if ANTHROPIC_API_KEY is missing or
still set to the placeholder, so the demo flow works end-to-end without
the API key. Look for "[DEMO]" markers in the output to spot the fallback.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from anthropic import Anthropic

from models import DraftCreate

log = logging.getLogger(__name__)

PROMPT_PATH    = Path(__file__).resolve().parent.parent / "prompts" / "email_draft.txt"
PROMPT_VERSION = "email_draft_v1"


# ─────────────────────────────────────────────────────────────
# Prompt formatting
# ─────────────────────────────────────────────────────────────

class _SafeDict(dict):
    """str.format_map sees missing keys as empty strings instead of raising."""
    def __missing__(self, key: str) -> str:
        return ""


def _format_score_breakdown(score: Optional[dict]) -> str:
    if not score:
        return "  (not yet scored)"
    lines = []
    for key in ("title_relevance", "seniority", "department", "geography", "email_verified"):
        if key in score:
            lines.append(f"  - {key.replace('_', ' ').title()}: {score[key]}")
    if score.get("rationale"):
        lines.append(f"  Rationale: {score['rationale']}")
    return "\n".join(lines)


def _build_prompt(opp: dict, contact: dict, sender_name: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    score_str = _format_score_breakdown(contact.get("score_reasoning"))
    company   = opp.get("cro_name") or opp.get("sponsor_name") or ""

    fields = _SafeDict(
        trial_title       = opp.get("trial_title") or "",
        sponsor_name      = opp.get("sponsor_name") or "",
        cro_name          = opp.get("cro_name") or "",
        phase             = opp.get("phase") or "",
        indication        = opp.get("indication") or "",
        therapeutic_area  = opp.get("therapeutic_area") or "",
        sites_needed      = str(opp.get("sites_needed") or ""),
        geography         = opp.get("geography") or "",
        protocol_start    = str(opp.get("protocol_start") or ""),
        contact_first_name = contact.get("first_name") or "",
        contact_last_name  = contact.get("last_name") or "",
        contact_title      = contact.get("title") or "",
        contact_department = contact.get("department") or "",
        contact_seniority  = contact.get("seniority") or "",
        contact_company    = company,
        contact_geography  = contact.get("geography") or "",
        score_breakdown    = score_str,
        sender_name        = sender_name,
    )
    return template.format_map(fields)


# ─────────────────────────────────────────────────────────────
# Response parsing (Claude is usually well-behaved, but be defensive)
# ─────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    text = text.strip()
    # Strip ``` fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
        text = text.strip()
    return json.loads(text)


# ─────────────────────────────────────────────────────────────
# Fallback for demo without API key
# ─────────────────────────────────────────────────────────────

def _fallback_draft(opp: dict, contact: dict, sender_name: str) -> dict:
    name = contact.get("first_name") or "there"
    company = opp.get("cro_name") or opp.get("sponsor_name") or "your team"
    title = opp.get("trial_title") or "your upcoming trial"
    phase = opp.get("phase") or ""
    indication = opp.get("indication") or "the indication"
    sites = opp.get("sites_needed") or "the planned site count"
    geography = opp.get("geography") or "the planned geographies"

    body = (
        f"{name},\n\n"
        f"[DEMO DRAFT — Anthropic API key not configured. Set ANTHROPIC_API_KEY in .env "
        f"to generate the real version.]\n\n"
        f"{title} {('('+phase+') ') if phase else ''}is moving toward site identification, "
        f"with roughly {sites} planned across {geography} — and given {company}'s footprint "
        f"in {indication}, it seems worth a quick conversation.\n\n"
        f"At Denali Health we focus on identifying and qualifying clinical sites for "
        f"programs in {opp.get('therapeutic_area') or 'this therapeutic area'}, "
        f"with existing investigator relationships in {indication}.\n\n"
        f"Would a 15-minute call this week make sense?\n\n"
        f"Best,\n{sender_name}\nDenali Health"
    )
    return {
        "subject": f"{title[:55]} — site identification",
        "body": body,
        "personalization_signals": [
            f"[demo] Trial: {title[:60]}",
            f"[demo] Indication: {indication}",
            f"[demo] CRO/Sponsor: {company}",
        ],
        "quality_flags": [
            "⚠ DEMO MODE — no API call made",
            "⚠ Set ANTHROPIC_API_KEY in .env to enable real generation",
            "✓ Trial-specific data interpolated as placeholder",
        ],
    }


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def generate_draft(
    opp: dict,
    contact: dict,
    sender_name: str = "Maryam",
) -> DraftCreate:
    """
    Generate an email draft for one (opportunity, contact) pair.

    `opp` and `contact` are dicts (typically from sqlite3.Row → dict).
    The contact's score_reasoning, if present, should already be a dict
    (parsed from JSON by the caller).

    Returns a DraftCreate ready to insert into email_drafts.
    Raises on AI errors so the caller can decide how to surface them.
    """
    api_key  = os.getenv("ANTHROPIC_API_KEY", "").strip()
    model    = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    max_toks = int(os.getenv("ANTHROPIC_MAX_TOKENS", "1024"))

    # Detect placeholder / missing key
    if not api_key or "REPLACE" in api_key or api_key in ("xxx", "your-key-here"):
        log.warning("ANTHROPIC_API_KEY missing/placeholder — using fallback demo draft")
        result = _fallback_draft(opp, contact, sender_name)
    else:
        prompt = _build_prompt(opp, contact, sender_name)
        log.info("Calling Anthropic (model=%s, prompt_len=%d)", model, len(prompt))
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_toks,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        try:
            result = _extract_json(text)
        except json.JSONDecodeError as e:
            log.error("AI returned invalid JSON. Error=%s. First 500 chars: %s", e, text[:500])
            raise

    # Combine signals + flags into one quality_flags list (matches model schema)
    flags: list[str] = []
    for sig in (result.get("personalization_signals") or []):
        flags.append(f"📌 {sig}")
    for fl in (result.get("quality_flags") or []):
        flags.append(fl)

    return DraftCreate(
        opportunity_id = opp["id"],
        contact_id     = contact["id"],
        sequence_step  = 1,
        subject_line   = result["subject"],
        body_text      = result["body"],
        prompt_version = PROMPT_VERSION,
        quality_flags  = flags,
    )

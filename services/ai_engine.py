"""
services/ai_engine.py - Anthropic-powered email draft generation.

Two modes:
  1. PROMPT-ONLY (default) - reads prompts/email_draft.txt, calls Claude.
  2. TEMPLATE-DRIVEN - reads a .docx from templates/, substitutes {placeholders},
     and asks Claude to fill in [BRACKETED INSTRUCTIONS] using the Clinwire
     Full Text article as primary context.

Both fall back to a hardcoded demo draft if ANTHROPIC_API_KEY is missing.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from anthropic import Anthropic

from models import DraftCreate
from services.template_engine import (
    load_template_text, substitute_placeholders, has_ai_instructions,
)

log = logging.getLogger(__name__)

PROMPT_PATH    = Path(__file__).resolve().parent.parent / "prompts" / "email_draft.txt"
PROMPT_VERSION = "email_draft_v1"


# ─────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────

class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _build_template_vars(opp: dict, contact: dict, sender_name: str) -> dict:
    """Build the variable dict used for {placeholder} substitution in templates."""
    raw = opp.get("raw_data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    drugs = raw.get("Drugs") or raw.get("drugs") or ""
    source_url = raw.get("Source URL") or raw.get("source_url") or ""
    company = opp.get("cro_name") or opp.get("sponsor_name") or ""
    full_name = " ".join(filter(None, [contact.get("first_name"), contact.get("last_name")])).strip()
    return {
        "first_name":       contact.get("first_name") or "",
        "last_name":        contact.get("last_name") or "",
        "full_name":        full_name,
        "title":            contact.get("title") or "",
        "company":          company,
        "sponsor_name":     opp.get("sponsor_name") or "",
        "cro_name":         opp.get("cro_name") or "",
        "trial_title":      opp.get("trial_title") or "",
        "phase":            opp.get("phase") or "",
        "indication":       opp.get("indication") or "",
        "therapeutic_area": opp.get("therapeutic_area") or "",
        "geography":        opp.get("geography") or "",
        "sites_needed":     str(opp.get("sites_needed") or ""),
        "drug_names":       drugs,
        "source_url":       source_url,
        "sender_name":      sender_name,
    }


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


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
        text = text.strip()
    return json.loads(text)


def _split_subject_body(rendered: str) -> tuple[str, str]:
    """If rendered text starts with 'SUBJECT: ...' on the first non-empty line,
    extract subject and return (subject, body). Otherwise (rendered[:60], rendered)."""
    lines = rendered.split("\n")
    first_nonempty = None
    for i, line in enumerate(lines):
        if line.strip():
            first_nonempty = i
            break
    if first_nonempty is None:
        return ("(no subject)", rendered)
    first_line = lines[first_nonempty].strip()
    m = re.match(r"^SUBJECT:\s*(.+)$", first_line, re.IGNORECASE)
    if m:
        subject = m.group(1).strip()
        body = "\n".join(lines[first_nonempty + 1:]).lstrip("\n")
        return (subject, body)
    return (first_line[:80], rendered)


def _call_claude(prompt: str, api_key: str, model: str, max_tokens: int) -> dict:
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text
    try:
        return _extract_json(text)
    except json.JSONDecodeError as e:
        log.error("AI returned invalid JSON: %s. First 500 chars: %s", e, text[:500])
        raise


# ─────────────────────────────────────────────────────────────
# Prompt-only mode (legacy)
# ─────────────────────────────────────────────────────────────

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
# Template-driven mode (NEW)
# ─────────────────────────────────────────────────────────────

def _build_template_prompt(template_text: str, vars_dict: dict, opp: dict, contact: dict) -> str:
    """
    Construct the Claude prompt for template-driven generation.
    Includes the substituted template, full text from Clinwire, and explicit
    instructions to fill [BRACKETS] without touching the rest.
    """
    raw = opp.get("raw_data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    full_text = raw.get("Full Text") or raw.get("full_text") or ""
    # Cap full text at 8000 chars to control token usage
    if len(full_text) > 8000:
        full_text = full_text[:8000] + "\n[...truncated...]"

    contact_summary = (
        f"{vars_dict.get('full_name','(unknown)')} - {vars_dict.get('title','')} "
        f"at {vars_dict.get('company','')}"
    )

    return f"""You are an outbound BD email assistant for Denali Health (clinical-trial site identification).

A template has been prepared with placeholders already filled in. Your job is to
fill ONLY the [BRACKETED INSTRUCTIONS] using the source article below. Do NOT
modify anything outside the brackets - keep the subject line, opening, signoff,
and all already-substituted text exactly as written.

TEMPLATE (placeholders already substituted; replace bracketed instructions):
\"\"\"
{substitute_placeholders(template_text, vars_dict)}
\"\"\"

RECIPIENT:
{contact_summary}

SOURCE ARTICLE (the Clinwire Full Text - use as primary context for [OPEN] and [VALUE] sections):
\"\"\"
{full_text}
\"\"\"

RULES:
1. Replace every [BRACKETED INSTRUCTION] with appropriate prose following the instruction.
2. Keep brackets out of your output - they're guidance for you, not text to keep.
3. Do NOT change the SUBJECT line, the greeting, or the signoff.
4. Aim for 130-180 words in the body total.
5. No hollow openers ("Hope this finds you well") - the brackets specifically demand specificity.
6. If the source article does not give you enough to fill a [BRACKET], improvise tastefully using {{indication}}, {{phase}}, and {{company}} context - do NOT leave brackets in the output.

Return ONLY JSON in this exact shape (no markdown fences, no preamble):
{{
  "subject": "extracted from the SUBJECT: line",
  "body": "full filled-in body without the SUBJECT line",
  "personalization_signals": ["specific fact #1 used", "specific fact #2", "..."],
  "quality_flags": ["self-evaluation, prefix with ✓ or ⚠"]
}}
"""


# ─────────────────────────────────────────────────────────────
# Fallback (no API key)
# ─────────────────────────────────────────────────────────────

def _fallback_draft(opp: dict, contact: dict, sender_name: str, template_filename: Optional[str]) -> dict:
    name = contact.get("first_name") or "there"
    company = opp.get("cro_name") or opp.get("sponsor_name") or "your team"
    title = opp.get("trial_title") or "your upcoming trial"
    indication = opp.get("indication") or "the indication"
    suffix = f" (template={template_filename})" if template_filename else ""
    body = (
        f"{name},\n\n"
        f"[DEMO DRAFT{suffix} - Anthropic API key not configured.]\n\n"
        f"{title} is moving forward and {company}'s footprint in {indication} "
        f"makes a quick conversation worthwhile.\n\n"
        f"At Denali Health we identify and qualify clinical sites for "
        f"{opp.get('therapeutic_area') or 'this therapeutic area'} programs.\n\n"
        f"Would a 15-minute call this week make sense?\n\n"
        f"Best,\n{sender_name}\nDenali Health"
    )
    return {
        "subject": f"{title[:55]} - site identification",
        "body": body,
        "personalization_signals": [f"[demo] Trial: {title[:60]}", f"[demo] Indication: {indication}"],
        "quality_flags": ["⚠ DEMO MODE - no API call made"],
    }


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def generate_draft(
    opp: dict,
    contact: dict,
    sender_name: str = "Maryam",
    template_filename: Optional[str] = None,
) -> DraftCreate:
    """
    Generate an email draft. If template_filename is set, uses template-driven
    generation (AI fills [BRACKETS] using the Clinwire Full Text). Otherwise
    uses the legacy prompt-only path.
    """
    api_key  = os.getenv("ANTHROPIC_API_KEY", "").strip()
    model    = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    max_toks = int(os.getenv("ANTHROPIC_MAX_TOKENS", "1024"))

    use_fallback = not api_key or "REPLACE" in api_key or api_key in ("xxx", "your-key-here")

    if use_fallback:
        log.warning("ANTHROPIC_API_KEY missing/placeholder - using fallback demo draft")
        result = _fallback_draft(opp, contact, sender_name, template_filename)
        prompt_version_used = PROMPT_VERSION + ("+template:" + template_filename if template_filename else "")
    elif template_filename:
        template_text = load_template_text(template_filename)
        if template_text is None:
            raise ValueError(f"Template not found: {template_filename}")
        vars_dict = _build_template_vars(opp, contact, sender_name)
        prompt = _build_template_prompt(template_text, vars_dict, opp, contact)
        log.info("Calling Anthropic with template=%s (prompt_len=%d)", template_filename, len(prompt))
        result = _call_claude(prompt, api_key, model, max_toks)
        prompt_version_used = f"template:{template_filename}"
    else:
        prompt = _build_prompt(opp, contact, sender_name)
        log.info("Calling Anthropic with prompt-only (model=%s, prompt_len=%d)", model, len(prompt))
        result = _call_claude(prompt, api_key, model, max_toks)
        prompt_version_used = PROMPT_VERSION

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
        prompt_version = prompt_version_used,
        quality_flags  = flags,
    )

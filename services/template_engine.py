"""
services/template_engine.py - .docx email template loader and renderer.

Templates live in templates/*.docx. Each template can contain:
  - {placeholders} - direct substitution from contact + opportunity data
  - [BRACKETED INSTRUCTIONS] - filled in by the AI using the Full Text article

Workflow:
  1. list_templates()        - discover templates for the UI dropdown
  2. load_template_text()    - read a .docx, return plain text
  3. substitute_placeholders() - swap {var} tokens
  4. The AI step in ai_engine.py uses the substituted template + Full Text
     to fill in the [INSTRUCTIONS] sections.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from docx import Document

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def list_templates() -> list[dict]:
    """List all .docx templates. Skips Word's temporary lock files (~$...)."""
    if not TEMPLATES_DIR.exists():
        return []
    out = []
    for path in sorted(TEMPLATES_DIR.glob("*.docx")):
        if path.name.startswith("~$"):
            continue
        out.append({
            "filename": path.name,
            "display_name": _humanize(path.stem),
            "size_kb": round(path.stat().st_size / 1024, 1),
        })
    return out


def _humanize(stem: str) -> str:
    """first_touch_dr_chen -> 'First Touch Dr Chen'"""
    return stem.replace("_", " ").replace("-", " ").title()


def load_template_text(filename: str) -> Optional[str]:
    """Read a .docx and return paragraph text joined by newlines. None if missing."""
    if not filename.lower().endswith(".docx"):
        return None
    path = TEMPLATES_DIR / filename
    if not path.exists() or not path.is_file():
        return None
    try:
        doc = Document(str(path))
    except Exception as e:
        log.error("Failed to open template %s: %s", filename, e)
        return None
    return "\n".join(p.text for p in doc.paragraphs)


PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def substitute_placeholders(template_text: str, vars: dict) -> str:
    """
    Replace {placeholder} tokens with values from `vars`.
    Unknown placeholders are left untouched (so the AI can flag them).
    """
    def repl(match):
        key = match.group(1)
        if key in vars and vars[key] is not None and vars[key] != "":
            return str(vars[key])
        return match.group(0)
    return PLACEHOLDER_RE.sub(repl, template_text)


def has_ai_instructions(template_text: str) -> bool:
    """True if the template contains [BRACKETED] instructions for the AI to fill."""
    return bool(re.search(r"\[[A-Z][^\]]{3,}\]", template_text))

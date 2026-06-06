"""
Utility functions extracted from services.py for the pipeline.
These are pure functions with no LLM dependency.
"""
import re

# Re-export from services.py — single source of truth until full migration
from resumes.services import (
    extract_keywords,
    score_ats,
    validate_resume,
    extract_section,
    replace_section,
    STOPWORDS,
)

__all__ = [
    "extract_keywords",
    "score_ats",
    "validate_resume",
    "extract_section",
    "replace_section",
    "STOPWORDS",
    "count_words",
    "dedupe_bullets",
]


def count_words(text):
    """Count real words in text (letters/digits only)."""
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


def dedupe_bullets(lines):
    """Remove duplicate bullets by normalised content."""
    seen = set()
    out = []
    for b in lines:
        norm = re.sub(r"[^a-z0-9\s]+", "", (b or "").lower()).strip()
        words = [w for w in norm.split() if w and w not in STOPWORDS]
        key = " ".join(words)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out

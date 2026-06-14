"""Shared description mapping from RawJob evidence rows to synced Jobs."""

from __future__ import annotations

import re

from harvest.enrichments import clean_job_text


HTMLISH_RE = re.compile(r"<[^>]+>|&(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-f]+);", re.I)


def job_description_for_sync(raw_job, *, max_len: int = 50000) -> str:
    """
    Canonical text to store on jobs.Job.description for harvested postings.

    RawJob.description_clean is the source of truth when present. If an older row
    only has RawJob.description, normalize it through the same cleaner before it
    reaches the Job table. Job.description is plain text, not renderable HTML.
    """
    for value in (
        getattr(raw_job, "description_clean", "") or "",
        getattr(raw_job, "description", "") or "",
        getattr(raw_job, "title", "") or "",
    ):
        cleaned = clean_job_text(value, max_len=max_len).strip()
        if cleaned:
            return cleaned
    return ""


def looks_htmlish(value: str) -> bool:
    """True when stored text still appears to contain HTML markup/entities."""
    return bool(HTMLISH_RE.search(value or ""))

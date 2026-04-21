"""Phase 4: cross-company dedup via Job.url_hash.

Prevents duplicate Job rows when the same posting is discovered through
multiple ATS detections (e.g. SmartRecruiters + a company's career page both
pointing at the same URL).
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .models import Job


def url_hash_for(url: str) -> str:
    return hashlib.sha256((url or '').strip().encode('utf-8')).hexdigest() if url else ''


def find_existing_job_by_url(url: str) -> Optional[Job]:
    """Return an existing active Job with matching url_hash, or None."""
    h = url_hash_for(url)
    if not h:
        return None
    return Job.objects.filter(url_hash=h, is_archived=False).order_by('created_at').first()

"""Phase 3: compute Job.quality_score (0.0–1.0) from key-field population.

Single source of truth; used by stage gates to decide whether a Job is allowed
to advance (e.g. ENRICHED → SCORED only if quality_score >= threshold).
"""
from __future__ import annotations


_KEY_FIELDS = (
    'title',
    'company',
    'location',
    'description',
    'original_link',
    'salary_range',
    'job_type',
    'job_source',
)


def compute_quality_score(job) -> float:
    """Fraction of key fields that are non-trivially populated."""
    present = 0
    for f in _KEY_FIELDS:
        val = getattr(job, f, None) or ''
        if isinstance(val, str):
            val = val.strip()
        if val and val != 'UNKNOWN':
            present += 1
    return round(present / len(_KEY_FIELDS), 3)

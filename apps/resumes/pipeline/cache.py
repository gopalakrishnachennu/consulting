"""
Caching utilities for the resume pipeline.

Caches deterministic data that doesn't change between resume generations:
- Education section text (per consultant)
- Certification section text (per consultant)
- Compatibility matrices (per consultant+job pair)
"""
import hashlib
import json
from django.core.cache import cache


def _hash_key(*parts):
    """Create a short hash from variable parts for cache key uniqueness."""
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── Education / Certification text caching ──────────────────────────

def get_cached_education(consultant):
    """Get cached education section text, or None."""
    records = list(
        consultant.education.values_list("degree", "institution", "end_date")
    )
    key = f"resume_edu_{consultant.pk}_{_hash_key(records)}"
    return cache.get(key), key


def set_cached_education(key, text, timeout=3600):
    cache.set(key, text, timeout=timeout)


def get_cached_certifications(consultant):
    """Get cached certifications section text, or None."""
    records = list(
        consultant.certifications.values_list("name", "issuing_organization", "issue_date")
    )
    key = f"resume_cert_{consultant.pk}_{_hash_key(records)}"
    return cache.get(key), key


def set_cached_certifications(key, text, timeout=3600):
    cache.set(key, text, timeout=timeout)


# ── Compatibility matrix caching ────────────────────────────────────

def get_cached_matching(consultant, job):
    """Get cached compatibility matrix, or None."""
    skills_hash = _hash_key(
        sorted(consultant.skills or []),
        job.parsed_jd.get("required_skills", []) if job.parsed_jd else [],
    )
    key = f"match_{consultant.pk}_{job.pk}_{skills_hash}"
    return cache.get(key), key


def set_cached_matching(key, matrix, timeout=3600):
    cache.set(key, matrix, timeout=timeout)

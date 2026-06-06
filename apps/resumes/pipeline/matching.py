"""
Phase 2: Candidate-JD Matching — Build a structured compatibility matrix.

Deterministic (no LLM calls). Compares consultant skills, experience, and
base resume against the structured JD intelligence from Phase 1.
"""
import re
import logging
from .cache import get_cached_matching, set_cached_matching

logger = logging.getLogger("apps.resumes.pipeline")


def _tokenize(text):
    """Extract meaningful tokens from text for matching."""
    return set(re.findall(r"[a-z0-9][a-z0-9+.#/-]{1,}", (text or "").lower()))


def _build_consultant_skill_set(consultant):
    """
    Build a comprehensive set of all consultant skills from multiple sources:
    - skills JSON list
    - experience descriptions
    - base_resume_text
    """
    skills = set()

    # From skills list
    for skill in (consultant.skills or []):
        skills.update(_tokenize(skill))

    # From experience descriptions
    for exp in consultant.experience.all():
        if exp.description:
            skills.update(_tokenize(exp.description))

    # From base resume text
    if consultant.base_resume_text:
        skills.update(_tokenize(consultant.base_resume_text))

    return skills


def build_compatibility_matrix(consultant, jd_intel):
    """
    Build a structured matching report between consultant and JD.

    Uses cached result if available (1hr TTL).

    Returns dict with:
        matched_required, matched_preferred, missing_required, missing_preferred,
        transferable_skills, experience_overlap, match_pct, coaching_keywords
    """
    # Check cache first
    from jobs.models import Job
    # We need the job object for caching — jd_intel has enough info
    # but we fake a minimal object for the cache key
    class _FakeJob:
        def __init__(self, jd):
            self.pk = jd.get("_job_pk", 0)
            self.parsed_jd = jd
    fake_job = _FakeJob(jd_intel)

    cached, cache_key = get_cached_matching(consultant, fake_job)
    if cached:
        return cached

    consultant_skills = _build_consultant_skill_set(consultant)

    # Match against required skills
    required = set()
    for skill in jd_intel.get("required_skills", []):
        required.update(_tokenize(skill))

    preferred = set()
    for skill in jd_intel.get("preferred_skills", []):
        preferred.update(_tokenize(skill))

    tools = set()
    for tool in jd_intel.get("tools_and_technologies", []):
        tools.update(_tokenize(tool))

    # All JD technical terms
    all_jd_terms = required | preferred | tools

    matched_required = sorted(required & consultant_skills)
    missing_required = sorted(required - consultant_skills)
    matched_preferred = sorted(preferred & consultant_skills)
    missing_preferred = sorted(preferred - consultant_skills)
    matched_tools = sorted(tools & consultant_skills)

    # Match percentage (based on required + tools)
    must_have = required | tools
    matched_must = must_have & consultant_skills
    match_pct = round(len(matched_must) / len(must_have) * 100) if must_have else 0

    # Experience overlap — which roles have relevant keywords
    experience_overlap = []
    for exp in consultant.experience.all():
        exp_tokens = _tokenize(exp.description or "")
        overlap = all_jd_terms & exp_tokens
        if overlap:
            experience_overlap.append({
                "role": f"{exp.title} at {exp.company}",
                "relevant_keywords": sorted(overlap)[:10],
                "overlap_count": len(overlap),
            })
    experience_overlap.sort(key=lambda x: -x["overlap_count"])

    # Certification overlap
    jd_certs = [c.lower().strip() for c in jd_intel.get("certifications_preferred", [])]
    consultant_certs = [
        c.name.lower().strip()
        for c in consultant.certifications.all()
    ]
    cert_matched = [c for c in jd_certs if any(cc in c or c in cc for cc in consultant_certs)]

    # Coaching keywords — high-impact missing terms to weave into the resume
    # Prioritise: missing required > missing tools > missing preferred
    coaching = []
    for term in missing_required[:8]:
        coaching.append(term)
    for tool in sorted(tools - consultant_skills)[:4]:
        if tool not in coaching:
            coaching.append(tool)
    for term in missing_preferred[:4]:
        if term not in coaching:
            coaching.append(term)

    # Warnings
    warnings = []
    if match_pct < 40:
        warnings.append(
            f"Low match ({match_pct}%). Fewer than 40% of required technologies "
            f"match the consultant's profile."
        )
    elif match_pct < 60:
        warnings.append(
            f"Moderate match ({match_pct}%). Resume will emphasize transferable skills."
        )

    matrix = {
        "matched_required": matched_required,
        "matched_preferred": matched_preferred,
        "matched_tools": matched_tools,
        "missing_required": missing_required,
        "missing_preferred": missing_preferred,
        "experience_overlap": experience_overlap[:5],
        "cert_matched": cert_matched,
        "cert_jd_wants": jd_certs,
        "match_pct": match_pct,
        "coaching_keywords": coaching[:16],
        "warnings": warnings,
        "total_required": len(required),
        "total_matched_required": len(matched_required),
    }

    set_cached_matching(cache_key, matrix, timeout=3600)
    return matrix

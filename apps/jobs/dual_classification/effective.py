from __future__ import annotations

from typing import Any


def _approved_output(raw_job) -> dict[str, Any]:
    snapshot = getattr(raw_job, "classification_snapshot", None)
    if not snapshot:
        return {}
    approved = getattr(snapshot, "approved_output", None) or {}
    if not isinstance(approved, dict):
        return {}
    return approved


def effective_raw_job_classification(raw_job) -> dict[str, Any]:
    approved = _approved_output(raw_job)
    classification = approved.get("classification") if isinstance(approved.get("classification"), dict) else {}
    location = approved.get("location") if isinstance(approved.get("location"), dict) else {}
    requirements = approved.get("requirements") if isinstance(approved.get("requirements"), dict) else {}
    skills = approved.get("skills") if isinstance(approved.get("skills"), dict) else {}
    identity = approved.get("identity") if isinstance(approved.get("identity"), dict) else {}

    fallback_country_codes = getattr(raw_job, "country_codes", None) or []
    fallback_country = getattr(raw_job, "country", "") or (fallback_country_codes[0] if fallback_country_codes else "")

    return {
        "title": identity.get("title") or getattr(raw_job, "title", "") or "",
        "normalized_title": identity.get("normalized_title") or getattr(raw_job, "normalized_title", "") or getattr(raw_job, "title", "") or "",
        "country": location.get("country") or fallback_country or "",
        "country_codes": location.get("country_codes") or fallback_country_codes or [],
        "location_type": location.get("location_type") or getattr(raw_job, "location_type", "") or "",
        "is_remote": (
            location.get("is_remote")
            if location.get("is_remote") is not None
            else bool(getattr(raw_job, "is_remote", False))
        ),
        "job_category": classification.get("job_category") or getattr(raw_job, "job_category", "") or "",
        "job_domain": classification.get("job_domain") or getattr(raw_job, "job_domain", "") or "",
        "department_normalized": classification.get("department_normalized") or getattr(raw_job, "department_normalized", "") or getattr(raw_job, "department", "") or "",
        "role_category": classification.get("role_category") or getattr(raw_job, "role_category", "") or "",
        "skills": skills.get("skills") or getattr(raw_job, "skills", None) or [],
        "tech_stack": skills.get("tech_stack") or getattr(raw_job, "tech_stack", None) or [],
        "job_keywords": skills.get("job_keywords") or getattr(raw_job, "job_keywords", None) or [],
        "title_keywords": skills.get("title_keywords") or getattr(raw_job, "title_keywords", None) or [],
        "years_required": requirements.get("years_required", getattr(raw_job, "years_required", None)),
        "years_required_max": requirements.get("years_required_max", getattr(raw_job, "years_required_max", None)),
        "education_required": requirements.get("education_required") or getattr(raw_job, "education_required", "") or "",
        "visa_sponsorship": (
            requirements.get("visa_sponsorship")
            if requirements.get("visa_sponsorship") is not None
            else getattr(raw_job, "visa_sponsorship", None)
        ),
        "work_authorization": requirements.get("work_authorization") or getattr(raw_job, "work_authorization", "") or "",
        "clearance_required": (
            requirements.get("clearance_required")
            if requirements.get("clearance_required") is not None
            else bool(getattr(raw_job, "clearance_required", False))
        ),
        "clearance_level": requirements.get("clearance_level") or getattr(raw_job, "clearance_level", "") or "",
    }

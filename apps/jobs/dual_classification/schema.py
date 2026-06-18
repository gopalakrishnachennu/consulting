from __future__ import annotations

import hashlib
import json
from typing import Any

from harvest.services.enrichment_input import build_enrichment_input


FIELD_PATHS: tuple[str, ...] = (
    "classification.job_category",
    "classification.job_domain",
    "classification.department_normalized",
    "classification.role_category",
    "location.country",
    "location.country_codes",
    "location.location_type",
    "location.is_remote",
    "requirements.years_required",
    "requirements.years_required_max",
    "requirements.education_required",
    "requirements.visa_sponsorship",
    "requirements.work_authorization",
    "requirements.clearance_required",
    "requirements.clearance_level",
    "skills.skills",
    "skills.tech_stack",
)

BACKEND_PRIORITY_PATHS: frozenset[str] = frozenset(
    {
        "location.country",
        "location.country_codes",
        "location.location_type",
        "location.is_remote",
        "requirements.years_required",
        "requirements.years_required_max",
        "requirements.visa_sponsorship",
        "requirements.work_authorization",
        "requirements.clearance_required",
        "requirements.clearance_level",
    }
)


def stable_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def compute_input_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def build_raw_job_input(raw_job: Any) -> dict[str, Any]:
    payload = build_enrichment_input(raw_job, company_name=getattr(raw_job, "company_name", "") or "")
    payload.update(
        {
            "raw_job_id": raw_job.pk,
            "platform_slug": getattr(raw_job, "platform_slug", "") or "",
            "original_url": getattr(raw_job, "original_url", "") or "",
            "apply_url": getattr(raw_job, "apply_url", "") or "",
            "location_type": getattr(raw_job, "location_type", "") or "",
            "is_remote": bool(getattr(raw_job, "is_remote", False)),
            "role_category": getattr(raw_job, "role_category", "") or "",
            "country_code": getattr(raw_job, "country_code", "") or "",
            "scope_status": getattr(raw_job, "scope_status", "") or "",
        }
    )
    return payload


def build_approval_input(raw_job: Any) -> dict[str, Any]:
    payload = build_enrichment_input(
        raw_job,
        company_name=getattr(raw_job, "company_name", "") or "",
        overrides={"raw_payload": {}},
    )
    payload.pop("raw_payload", None)
    payload.update(
        {
            "raw_job_id": raw_job.pk,
            "platform_slug": getattr(raw_job, "platform_slug", "") or "",
            "original_url": getattr(raw_job, "original_url", "") or "",
            "apply_url": getattr(raw_job, "apply_url", "") or "",
            "location_type": getattr(raw_job, "location_type", "") or "",
            "is_remote": bool(getattr(raw_job, "is_remote", False)),
            "role_category": getattr(raw_job, "role_category", "") or "",
            "country_code": getattr(raw_job, "country_code", "") or "",
            "scope_status": getattr(raw_job, "scope_status", "") or "",
        }
    )
    return payload


def compute_approval_input_hash(raw_job: Any) -> str:
    return compute_input_hash(build_approval_input(raw_job))


def canonical_from_enrichment(raw_job: Any, enriched: dict[str, Any], input_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": {
            "raw_job_id": raw_job.pk,
            "title": raw_job.title or "",
            "normalized_title": enriched.get("normalized_title") or raw_job.normalized_title or "",
            "company_name": raw_job.company_name or "",
            "platform_slug": raw_job.platform_slug or "",
            "original_url": raw_job.original_url or "",
        },
        "location": {
            "country": enriched.get("country") or raw_job.country or "",
            "country_code": raw_job.country_code or "",
            "country_codes": enriched.get("country_codes") or raw_job.country_codes or [],
            "location_type": raw_job.location_type or "",
            "is_remote": bool(raw_job.is_remote),
            "location_raw": raw_job.location_raw or input_payload.get("location_raw") or "",
            "location_candidates": enriched.get("location_candidates") or raw_job.location_candidates or [],
        },
        "classification": {
            "job_category": enriched.get("job_category") or raw_job.job_category or "",
            "job_domain": enriched.get("job_domain") or raw_job.job_domain or "",
            "job_domain_candidates": enriched.get("job_domain_candidates") or raw_job.job_domain_candidates or [],
            "department_normalized": enriched.get("department_normalized") or raw_job.department_normalized or "",
            "role_category": raw_job.role_category or "",
        },
        "skills": {
            "skills": enriched.get("skills") or raw_job.skills or [],
            "tech_stack": enriched.get("tech_stack") or raw_job.tech_stack or [],
            "title_keywords": enriched.get("title_keywords") or raw_job.title_keywords or [],
            "job_keywords": enriched.get("job_keywords") or raw_job.job_keywords or [],
        },
        "requirements": {
            "years_required": enriched.get("years_required"),
            "years_required_max": enriched.get("years_required_max"),
            "education_required": enriched.get("education_required") or raw_job.education_required or "",
            "visa_sponsorship": enriched.get("visa_sponsorship") or raw_job.visa_sponsorship or "",
            "work_authorization": enriched.get("work_authorization") or raw_job.work_authorization or "",
            "clearance_required": enriched.get("clearance_required") if "clearance_required" in enriched else raw_job.clearance_required,
            "clearance_level": enriched.get("clearance_level") or raw_job.clearance_level or "",
            "travel_required": enriched.get("travel_required") if "travel_required" in enriched else raw_job.travel_required,
            "travel_pct_min": enriched.get("travel_pct_min"),
            "travel_pct_max": enriched.get("travel_pct_max"),
            "schedule_type": enriched.get("schedule_type") or raw_job.schedule_type or "",
            "shift_schedule": enriched.get("shift_schedule") or raw_job.shift_schedule or "",
            "shift_details": enriched.get("shift_details") or raw_job.shift_details or "",
            "hours_hint": enriched.get("hours_hint") or raw_job.hours_hint or "",
            "weekend_required": enriched.get("weekend_required") if "weekend_required" in enriched else raw_job.weekend_required,
            "certifications": enriched.get("certifications") or raw_job.certifications or [],
            "licenses_required": enriched.get("licenses_required") or raw_job.licenses_required or [],
            "languages_required": enriched.get("languages_required") or raw_job.languages_required or [],
        },
        "sections": {
            "requirements": enriched.get("requirements") or raw_job.requirements or "",
            "responsibilities": enriched.get("responsibilities") or raw_job.responsibilities or "",
            "benefits": enriched.get("benefits") or raw_job.benefits or "",
        },
        "scores": {
            "quality_score": enriched.get("quality_score", raw_job.quality_score),
            "jd_quality_score": enriched.get("jd_quality_score", raw_job.jd_quality_score),
            "classification_confidence": enriched.get("classification_confidence", raw_job.classification_confidence),
            "category_confidence": enriched.get("category_confidence", raw_job.category_confidence),
            "resume_ready_score": enriched.get("resume_ready_score", raw_job.resume_ready_score),
        },
        "provenance": {
            "classification_source": enriched.get("classification_source") or raw_job.classification_source or "",
            "enrichment_version": enriched.get("enrichment_version") or raw_job.enrichment_version or "",
            "domain_version": enriched.get("domain_version") or raw_job.domain_version or "",
            "classification_provenance": enriched.get("classification_provenance") or raw_job.classification_provenance or {},
            "field_confidence": enriched.get("field_confidence") or raw_job.field_confidence or {},
            "field_provenance": enriched.get("field_provenance") or raw_job.field_provenance or {},
        },
    }


def get_path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def validate_canonical_output(data: dict[str, Any]) -> list[str]:
    if not isinstance(data, dict):
        return ["Classification payload must be a JSON object."]

    errors: list[str] = []
    required_sections = ("identity", "classification", "skills", "requirements", "location")
    for section in required_sections:
        if section not in data or not isinstance(data.get(section), dict):
            errors.append(f"Missing required object section: {section}")

    identity = data.get("identity") or {}
    classification = data.get("classification") or {}
    if identity and not isinstance(identity.get("title", ""), str):
        errors.append("identity.title must be a string")
    if classification and not isinstance(classification.get("job_category", ""), str):
        errors.append("classification.job_category must be a string")
    if classification and not isinstance(classification.get("job_domain", ""), str):
        errors.append("classification.job_domain must be a string")

    for key in ("skills", "tech_stack"):
        value = (data.get("skills") or {}).get(key, [])
        if value not in (None, []) and not isinstance(value, list):
            errors.append(f"skills.{key} must be a list")

    return errors

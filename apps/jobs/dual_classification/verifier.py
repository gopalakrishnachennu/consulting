from __future__ import annotations

import re
from typing import Any


def _normalized_terms(value: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (value or "").lower())
    return {part for part in cleaned.split() if part}


def _contains_any(text: str, values: list[str]) -> bool:
    haystack = (text or "").lower()
    for value in values:
        needle = (value or "").strip().lower()
        if needle and needle in haystack:
            return True
    return False


def _unsupported_values(text: str, values: list[str]) -> list[str]:
    haystack = (text or "").lower()
    missing: list[str] = []
    for value in values:
        needle = (value or "").strip().lower()
        if needle and needle not in haystack:
            missing.append(value)
    return missing


def verify_output(raw_job: Any, normalized_output: dict[str, Any]) -> dict[str, Any]:
    title = (getattr(raw_job, "title", "") or "").strip()
    description = (getattr(raw_job, "description_clean", "") or getattr(raw_job, "description", "") or "").strip()
    full_text = f"{title}\n{description}".strip()
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    score = 1.0

    has_title = bool(title)
    has_description = bool(description)
    has_category = bool(normalized_output.get("classification", {}).get("job_category"))
    has_domain = bool(normalized_output.get("classification", {}).get("job_domain"))
    skills = normalized_output.get("skills", {}).get("skills") or []
    tech_stack = normalized_output.get("skills", {}).get("tech_stack") or []
    years_required = normalized_output.get("requirements", {}).get("years_required")

    checks.append({"key": "has_title", "passed": has_title})
    checks.append({"key": "has_description", "passed": has_description})
    checks.append({"key": "has_job_category", "passed": has_category})
    checks.append({"key": "has_job_domain", "passed": has_domain})

    if not has_title:
        score -= 0.35
        warnings.append("missing_title")
    if not has_description:
        score -= 0.35
        warnings.append("missing_description")
    if not has_category:
        score -= 0.15
        warnings.append("missing_job_category")
    if not has_domain:
        score -= 0.1
        warnings.append("missing_job_domain")

    supported_skills = _contains_any(full_text, [*skills, *tech_stack]) if full_text else False
    checks.append({"key": "skills_supported_by_text", "passed": supported_skills or not (skills or tech_stack)})
    if (skills or tech_stack) and not supported_skills:
        score -= 0.15
        warnings.append("skills_not_supported_by_text")
    unsupported_skills = _unsupported_values(full_text, [*skills, *tech_stack]) if full_text else [*skills, *tech_stack]
    if unsupported_skills and len(unsupported_skills) == len([*skills, *tech_stack]) and (skills or tech_stack):
        checks.append({"key": "all_skills_unsupported_by_text", "passed": False})
    elif unsupported_skills:
        checks.append({"key": "partial_skills_unsupported_by_text", "passed": False, "unsupported": unsupported_skills[:8]})

    title_and_description_terms = _normalized_terms(full_text)
    role_category = str(normalized_output.get("classification", {}).get("role_category") or "").strip().lower()
    category = str(normalized_output.get("classification", {}).get("job_category") or "").strip().lower()
    domain = str(normalized_output.get("classification", {}).get("job_domain") or "").strip().lower()
    classification_terms = _normalized_terms(f"{domain} {role_category}")
    semantic_mismatch = bool(
        classification_terms
        and title_and_description_terms
        and classification_terms.isdisjoint(title_and_description_terms)
    )
    checks.append({"key": "title_classification_alignment", "passed": not semantic_mismatch})
    if semantic_mismatch:
        score -= 0.05
        warnings.append("title_classification_mismatch")

    if years_required is not None:
        years_found = bool(re.search(r"\b\d{1,2}\+?\s*(?:years?|yrs?)\b", full_text.lower()))
        checks.append({"key": "years_supported_by_text", "passed": years_found})
        if not years_found:
            score -= 0.1
            warnings.append("years_not_supported_by_text")

    score = max(0.0, round(score, 3))
    status = "pass" if score >= 0.8 else "warn" if score >= 0.55 else "fail"
    return {
        "status": status,
        "score": score,
        "warnings": warnings,
        "checks": checks,
    }

"""
Phase 3d+3e: Education + Certifications — Deterministic section generation.

No LLM calls. Built directly from DB records. Cached per consultant.
"""
from ..cache import (
    get_cached_education, set_cached_education,
    get_cached_certifications, set_cached_certifications,
)


def generate_education(consultant):
    """
    Generate the EDUCATION section from consultant's education records.
    Returns plain text. Cached per consultant + records hash.
    """
    cached, cache_key = get_cached_education(consultant)
    if cached:
        return cached

    lines = ["EDUCATION"]
    records = list(consultant.education.all())
    if not records:
        lines.append("Not provided")
    else:
        for edu in records:
            end = edu.end_date.strftime("%Y") if edu.end_date else "Present"
            start = edu.start_date.strftime("%Y") if edu.start_date else ""
            date_range = f"{start} - {end}" if start else end
            lines.append(f"{edu.degree} in {edu.field_of_study}")
            lines.append(f"{edu.institution} | {date_range}")
            lines.append("")

    text = "\n".join(lines).strip()
    set_cached_education(cache_key, text)
    return text


def generate_certifications(consultant):
    """
    Generate the CERTIFICATIONS section from consultant's certification records.
    Returns plain text or empty string if none. Cached per consultant + records hash.
    """
    cached, cache_key = get_cached_certifications(consultant)
    if cached:
        return cached

    records = list(consultant.certifications.all())
    if not records:
        return ""

    lines = ["CERTIFICATIONS"]
    for cert in records:
        year = cert.issue_date.strftime("%Y") if cert.issue_date else ""
        line = f"- {cert.name}"
        if cert.issuing_organization:
            line += f" — {cert.issuing_organization}"
        if year:
            line += f" — {year}"
        lines.append(line)

    text = "\n".join(lines).strip()
    set_cached_certifications(cache_key, text)
    return text

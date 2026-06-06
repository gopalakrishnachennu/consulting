"""
Phase 1: JD Intelligence — Extract structured data from Job.parsed_jd.

Uses the already-parsed JD JSON on the Job model instead of re-sending
raw JD text to the LLM. Zero tokens consumed.
"""
import logging

logger = logging.getLogger("apps.resumes.pipeline")


def get_jd_intelligence(job):
    """
    Return structured JD data from the Job model's parsed_jd field.

    If parsed_jd is empty or not yet parsed, triggers parsing and caches it.

    Returns dict with keys: required_skills, preferred_skills, tools_and_technologies,
    platforms_and_services, certifications_preferred, keywords_for_ats,
    seniority_level, role_domain, responsibilities, job_title, company,
    location, job_type, raw_description, source.
    """
    parsed = job.parsed_jd or {}

    # If we have a valid parsed JD, use it
    if parsed and job.parsed_jd_status in ("OK", "OK_RULES", "completed"):
        return _build_intel_from_parsed(job, parsed, source="cached")

    # Try to parse the JD if not done yet
    if job.description and job.description.strip():
        try:
            from jobs.services import JDParserService
            parser = JDParserService()
            parsed = parser.parse_job(job)
            if parsed:
                return _build_intel_from_parsed(job, parsed, source="freshly_parsed")
        except Exception as e:
            logger.warning("JD parsing failed for job %s: %s", job.pk, e)

    # Fallback: extract what we can from raw fields
    return _build_intel_from_raw(job)


def _build_intel_from_parsed(job, parsed, source="cached"):
    """Build intelligence dict from parsed JD JSON."""
    return {
        "job_title": parsed.get("job_title") or job.title,
        "company": parsed.get("company") or job.company,
        "location": job.location or "Not specified",
        "job_type": job.get_job_type_display() if hasattr(job, 'get_job_type_display') else str(job.job_type),
        "seniority_level": parsed.get("seniority_level", "mid"),
        "role_domain": parsed.get("role_domain", ""),
        "required_skills": parsed.get("required_skills", []),
        "preferred_skills": parsed.get("preferred_skills", []),
        "tools_and_technologies": parsed.get("tools_and_technologies", []),
        "platforms_and_services": parsed.get("platforms_and_services", []),
        "certifications_preferred": parsed.get("certifications_preferred", []),
        "keywords_for_ats": parsed.get("keywords_for_ats", []),
        "responsibilities": parsed.get("responsibilities", []),
        "action_verbs": parsed.get("action_verbs", []),
        "soft_skills": parsed.get("soft_skills", []),
        "domain_specific_terms": parsed.get("domain_specific_terms", []),
        "exact_phrases": parsed.get("exact_phrases", []),
        "raw_description": job.description or "",
        "source": source,
    }


def _build_intel_from_raw(job):
    """Fallback: build minimal intelligence from raw job fields."""
    from .utils import extract_keywords

    desc = job.description or ""
    keywords = extract_keywords(desc, max_keywords=50)

    return {
        "job_title": job.title,
        "company": job.company,
        "location": job.location or "Not specified",
        "job_type": job.get_job_type_display() if hasattr(job, 'get_job_type_display') else "",
        "seniority_level": "mid",
        "role_domain": "",
        "required_skills": keywords[:20],
        "preferred_skills": [],
        "tools_and_technologies": keywords[:15],
        "platforms_and_services": [],
        "certifications_preferred": [],
        "keywords_for_ats": keywords[:30],
        "responsibilities": [],
        "action_verbs": [],
        "soft_skills": [],
        "domain_specific_terms": [],
        "exact_phrases": [],
        "raw_description": desc,
        "source": "raw_fallback",
    }

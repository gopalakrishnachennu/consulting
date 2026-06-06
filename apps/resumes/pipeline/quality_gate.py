"""
Phase 5: Quality Gate — ATS scoring + auto-retry weak sections.

If ATS score is below threshold or validation errors exist,
identifies the weakest section and retries ONLY that section.
"""
import re
import logging
from .utils import score_ats, extract_keywords

logger = logging.getLogger("apps.resumes.pipeline")

DEFAULT_ATS_THRESHOLD = 60
MAX_SECTION_RETRIES = 2


def run_quality_gate(
    content,
    job,
    jd_intel,
    validation_errors,
    validation_warnings,
    *,
    ats_threshold=DEFAULT_ATS_THRESHOLD,
    generators=None,
    llm_client=None,
    matching=None,
    consultant=None,
    section_prompts=None,
    actor=None,
):
    """
    Check resume quality. Retry weak sections if below threshold.

    Args:
        content: Assembled resume text
        job: Job model instance
        jd_intel: Phase 1 JD intelligence dict
        validation_errors: List from Phase 4 validation
        generators: Dict of {section_name: generator_function} for retries
        llm_client: PipelineLLMClient instance
        matching: Phase 2 compatibility matrix
        consultant: ConsultantProfile
        section_prompts: Dict of {section_type: SectionPrompt}
        actor: User who triggered generation

    Returns: {
        "passed": bool,
        "ats_score": int,
        "validation_errors": list,
        "validation_warnings": list,
        "retried_sections": list,
        "final_content": str,
        "retry_tokens": int,
    }
    """
    # Calculate ATS score using structured JD keywords
    ats_keywords = jd_intel.get("keywords_for_ats", [])
    if ats_keywords:
        # Build a synthetic "JD text" from keywords for score_ats
        keyword_text = " ".join(ats_keywords)
        ats = score_ats(keyword_text, content)
    else:
        ats = score_ats(jd_intel.get("raw_description", ""), content)

    result = {
        "passed": True,
        "ats_score": ats,
        "validation_errors": validation_errors,
        "validation_warnings": validation_warnings,
        "retried_sections": [],
        "final_content": content,
        "retry_tokens": 0,
    }

    # Check if quality gate passes
    has_critical_errors = any(
        "missing required section" in (e or "").lower() for e in validation_errors
    )
    needs_retry = ats < ats_threshold or has_critical_errors

    if not needs_retry:
        return result

    if not generators or not llm_client:
        result["passed"] = ats >= ats_threshold and not has_critical_errors
        return result

    logger.info(
        "Quality gate: ATS %d (threshold %d), %d errors — attempting retries for job %s",
        ats, ats_threshold, len(validation_errors), job.pk if job else "?"
    )

    # Identify weak sections
    weak_sections = _identify_weak_sections(validation_errors, ats, content, jd_intel)

    retried = []
    total_retry_tokens = 0

    for section_name in weak_sections[:MAX_SECTION_RETRIES]:
        if section_name not in generators:
            continue

        logger.info("Retrying section: %s", section_name)
        gen_fn = generators[section_name]
        sp = section_prompts.get(section_name) if section_prompts else None

        new_text, tokens, err = gen_fn(
            llm_client, jd_intel, matching, consultant,
            section_prompt=sp, job=job, actor=actor,
        )
        total_retry_tokens += tokens

        if err or not new_text:
            logger.warning("Retry failed for %s: %s", section_name, err)
            continue

        retried.append(section_name)
        # Replace section in content
        content = _replace_section_in_content(content, section_name, new_text)

    # Re-validate after retries
    if retried:
        from .utils import validate_resume
        new_errors, new_warnings = validate_resume(content)

        # Recalculate ATS
        if ats_keywords:
            ats = score_ats(" ".join(ats_keywords), content)
        else:
            ats = score_ats(jd_intel.get("raw_description", ""), content)

        result.update({
            "ats_score": ats,
            "validation_errors": new_errors,
            "validation_warnings": new_warnings,
            "retried_sections": retried,
            "final_content": content,
            "retry_tokens": total_retry_tokens,
        })

    result["passed"] = result["ats_score"] >= ats_threshold and not any(
        "missing required section" in (e or "").lower()
        for e in result["validation_errors"]
    )

    return result


def _identify_weak_sections(validation_errors, ats_score, content, jd_intel):
    """Identify which sections need retry based on validation errors."""
    weak = []
    error_text = " ".join(validation_errors).lower()

    if "professional summary" in error_text or "summary must be" in error_text:
        weak.append("summary")
    if "skills" in error_text and "key:value" in error_text:
        weak.append("skills")
    if "professional experience" in error_text or "bullet" in error_text:
        weak.append("experience")

    # If no specific section errors but ATS is low, retry experience (most impactful)
    if not weak and ats_score < 50:
        weak.append("experience")
    elif not weak:
        weak.append("summary")

    return weak


def _replace_section_in_content(content, section_name, new_text):
    """Replace a section's content in the assembled resume."""
    heading_map = {
        "summary": "PROFESSIONAL SUMMARY",
        "skills": "SKILLS",
        "experience": "PROFESSIONAL EXPERIENCE",
    }
    heading = heading_map.get(section_name)
    if not heading:
        return content

    next_headings = ["PROFESSIONAL SUMMARY", "SKILLS", "PROFESSIONAL EXPERIENCE",
                     "EDUCATION", "CERTIFICATIONS"]

    # Find this section's bounds
    start = content.find(heading)
    if start == -1:
        return content

    after = content[start + len(heading):]
    # Find where next section starts
    end = len(content)
    for h in next_headings:
        if h == heading:
            continue
        idx = after.find(h)
        if idx != -1:
            end = start + len(heading) + idx
            break

    return (
        content[:start] + heading + "\n" + new_text.strip() + "\n\n" + content[end:]
    ).strip()

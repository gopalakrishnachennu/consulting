"""
Phase 3b: Skills section generation.

Wraps the existing skills_extractor.py 7-phase pipeline where possible,
with a direct LLM fallback for simpler cases.
"""
import logging

from ..prompts import SKILLS_SYSTEM, build_skills_prompt

logger = logging.getLogger("apps.resumes.pipeline")


def generate_skills(llm_client, jd_intel, matching, consultant,
                    section_prompt=None, job=None, actor=None):
    """
    Generate SKILLS section in key:value format.

    Tries the existing skills_extractor first. Falls back to direct LLM call.

    Returns: (text, tokens_used, error)
    """
    # Try the existing skills_extractor pipeline
    try:
        from resumes.skills_extractor import generate_skills_from_jd
        skills_text = generate_skills_from_jd(
            jd_text=jd_intel.get("raw_description", ""),
            consultant=consultant,
            job=job,
        )
        if skills_text and skills_text.strip() and ":" in skills_text:
            # Clean up: remove heading if present
            cleaned = skills_text.strip()
            for prefix in ["SKILLS", "Skills", "TECHNICAL SKILLS"]:
                if cleaned.upper().startswith(prefix.upper()):
                    cleaned = cleaned[len(prefix):].strip().lstrip(":").strip()
            logger.info("Skills generated via skills_extractor for job %s", job.pk if job else "?")
            return cleaned, 0, None
    except Exception as e:
        logger.warning("skills_extractor failed, falling back to direct LLM: %s", e)

    # Fallback: direct LLM call with structured data
    section_rules = section_prompt.generation_rules if section_prompt else ""
    system = section_prompt.get_system_prompt() if section_prompt else SKILLS_SYSTEM
    user_prompt = build_skills_prompt(jd_intel, matching, consultant, section_rules)

    temp = section_prompt.get_temperature(0.1) if section_prompt else 0.1
    max_tok = section_prompt.get_max_tokens(800) if section_prompt else 800

    content, tokens, error = llm_client.call(
        system, user_prompt,
        request_type="pipeline_skills",
        temperature=temp,
        max_tokens=max_tok,
        job=job,
        consultant=consultant,
        actor=actor,
    )

    if error:
        return None, tokens, error

    # Clean up heading
    if content:
        content = content.strip()
        for prefix in ["SKILLS", "Skills", "TECHNICAL SKILLS"]:
            if content.upper().startswith(prefix.upper()):
                content = content[len(prefix):].strip().lstrip(":").strip()

    return content, tokens, None

"""
Phase 3c: Professional Experience generation.

The most complex section. Generates per-role bullets using structured
JD data and matching intelligence. ~1200 input tokens.
"""
import logging

from ..prompts import EXPERIENCE_SYSTEM, build_experience_prompt

logger = logging.getLogger("apps.resumes.pipeline")


def generate_experience(llm_client, jd_intel, matching, consultant,
                        section_prompt=None, job=None, actor=None):
    """
    Generate PROFESSIONAL EXPERIENCE section with per-role bullets.

    Returns: (text, tokens_used, error)
    """
    experiences = list(consultant.experience.all())

    if not experiences:
        # No experience records — build from base resume if available
        if consultant.base_resume_text:
            return _generate_from_base_resume(
                llm_client, jd_intel, matching, consultant,
                section_prompt, job, actor
            )
        return "No experience listed.", 0, None

    section_rules = section_prompt.generation_rules if section_prompt else ""
    system = section_prompt.get_system_prompt() if section_prompt else EXPERIENCE_SYSTEM
    user_prompt = build_experience_prompt(jd_intel, matching, consultant, section_rules)

    temp = section_prompt.get_temperature(0.5) if section_prompt else 0.5
    max_tok = section_prompt.get_max_tokens(2500) if section_prompt else 2500

    content, tokens, error = llm_client.call(
        system, user_prompt,
        request_type="pipeline_experience",
        temperature=temp,
        max_tokens=max_tok,
        job=job,
        consultant=consultant,
        actor=actor,
    )

    if error:
        return None, tokens, error

    # Clean up: remove heading if LLM added one
    if content:
        content = content.strip()
        for prefix in ["PROFESSIONAL EXPERIENCE", "Professional Experience", "WORK EXPERIENCE"]:
            if content.upper().startswith(prefix.upper()):
                content = content[len(prefix):].strip().lstrip(":").strip()

    return content, tokens, None


def _generate_from_base_resume(llm_client, jd_intel, matching, consultant,
                                section_prompt, job, actor):
    """
    Generate experience from base_resume_text when no structured Experience records exist.
    This is the fallback path — sends base resume as context.
    """
    system = section_prompt.get_system_prompt() if section_prompt else EXPERIENCE_SYSTEM

    base = consultant.base_resume_text or ""
    coaching = matching.get("coaching_keywords", [])
    responsibilities = jd_intel.get("responsibilities", [])[:8]

    user_prompt = f"""Generate PROFESSIONAL EXPERIENCE bullets from this candidate's base resume,
targeting this role:

ROLE: {jd_intel.get('job_title', 'N/A')}
SENIORITY: {jd_intel.get('seniority_level', 'mid')}

BASE RESUME:
{base[:3000]}

JD RESPONSIBILITIES TO REFLECT:
{chr(10).join(f'- {r}' for r in responsibilities) if responsibilities else '- General role responsibilities'}

OUTPUT FORMAT:
Title | Company | Start – End
- Bullet (22-25 words each)

RULES:
- Keep all companies, titles, dates EXACTLY as they appear in the base resume
- Do NOT invent companies or dates
- 7-10 bullets for most recent role, 6 for all others
- Each bullet: Action Verb + Technology + Outcome, 22-25 words
- Do NOT copy JD text verbatim — rephrase as accomplishments"""

    if coaching:
        user_prompt += f"\n\nKEY TERMS TO WEAVE IN: {', '.join(coaching)}"

    if section_prompt and section_prompt.generation_rules:
        user_prompt += f"\n\nADDITIONAL RULES:\n{section_prompt.generation_rules}"

    temp = section_prompt.get_temperature(0.5) if section_prompt else 0.5
    max_tok = section_prompt.get_max_tokens(2500) if section_prompt else 2500

    content, tokens, error = llm_client.call(
        system, user_prompt,
        request_type="pipeline_experience_base_resume",
        temperature=temp,
        max_tokens=max_tok,
        job=job,
        consultant=consultant,
        actor=actor,
    )

    if error:
        return None, tokens, error

    if content:
        content = content.strip()
        for prefix in ["PROFESSIONAL EXPERIENCE", "Professional Experience"]:
            if content.upper().startswith(prefix.upper()):
                content = content[len(prefix):].strip().lstrip(":").strip()

    return content, tokens, None

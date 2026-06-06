"""
Phase 3a: Professional Summary generation.

Focused LLM call with ~400 input tokens. Structured data only, no raw JD text.
Auto-retries if word count is outside 70-80.
"""
import re
import logging

from ..prompts import SUMMARY_SYSTEM, build_summary_prompt

logger = logging.getLogger("apps.resumes.pipeline")


def generate_summary(llm_client, jd_intel, matching, consultant,
                     section_prompt=None, job=None, actor=None):
    """
    Generate PROFESSIONAL SUMMARY section.

    Returns: (text, tokens_used, error)
    """
    # Build prompt
    section_rules = section_prompt.generation_rules if section_prompt else ""
    system = section_prompt.get_system_prompt() if section_prompt else SUMMARY_SYSTEM
    user_prompt = build_summary_prompt(jd_intel, matching, consultant, section_rules)

    # Temperature + max_tokens
    temp = section_prompt.get_temperature(0.7) if section_prompt else 0.7
    max_tok = section_prompt.get_max_tokens(300) if section_prompt else 300

    content, tokens, error = llm_client.call(
        system, user_prompt,
        request_type="pipeline_summary",
        temperature=temp,
        max_tokens=max_tok,
        job=job,
        consultant=consultant,
        actor=actor,
    )

    if error:
        return None, tokens, error

    # Validate word count and retry once if needed
    word_count = len(re.findall(r"[A-Za-z0-9']+", content or ""))
    if word_count < 65 or word_count > 90:
        logger.info(
            "Summary word count %d outside 70-80, retrying for job %s",
            word_count, job.pk if job else "?"
        )
        retry_prompt = (
            user_prompt + f"\n\nPREVIOUS ATTEMPT WAS {word_count} WORDS. "
            f"Rewrite to be EXACTLY 70-80 words. Count carefully."
        )
        content2, tokens2, error2 = llm_client.call(
            system, retry_prompt,
            request_type="pipeline_summary_retry",
            temperature=max(temp - 0.1, 0.3),
            max_tokens=max_tok,
            job=job,
            consultant=consultant,
            actor=actor,
        )
        if not error2 and content2:
            tokens += tokens2
            content = content2

    # Clean up: remove any heading the LLM might have added
    if content:
        content = content.strip()
        for prefix in ["PROFESSIONAL SUMMARY", "Professional Summary", "Summary:"]:
            if content.upper().startswith(prefix.upper()):
                content = content[len(prefix):].strip().lstrip(":").strip()

    return content, tokens, None

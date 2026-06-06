"""
Phase 4: Assembly — Combine generated sections into final resume text.

Deterministic. Validates format and runs deduplication.
"""
import logging
from .utils import validate_resume, dedupe_bullets

logger = logging.getLogger("apps.resumes.pipeline")


def assemble_resume(header, summary, skills, experience, education, certifications=""):
    """
    Combine all sections into the final resume plain text.
    Validates format and runs bullet deduplication.

    Returns: (content, validation_errors, validation_warnings)
    """
    sections = []

    # Header (no heading needed)
    if header:
        sections.append(header.strip())

    # Professional Summary
    sections.append("")
    sections.append("PROFESSIONAL SUMMARY")
    sections.append(summary.strip() if summary else "Summary not generated.")

    # Skills
    sections.append("")
    sections.append("SKILLS")
    sections.append(skills.strip() if skills else "Skills not generated.")

    # Professional Experience
    sections.append("")
    sections.append("PROFESSIONAL EXPERIENCE")
    if experience:
        # Deduplicate bullets within experience
        exp_lines = experience.strip().split("\n")
        deduped = _dedupe_experience_bullets(exp_lines)
        sections.append("\n".join(deduped))
    else:
        sections.append("Experience not generated.")

    # Education
    if education:
        sections.append("")
        # Education text already includes the heading
        if not education.strip().upper().startswith("EDUCATION"):
            sections.append("EDUCATION")
        sections.append(education.strip())

    # Certifications
    if certifications and certifications.strip():
        sections.append("")
        if not certifications.strip().upper().startswith("CERTIFICATIONS"):
            sections.append("CERTIFICATIONS")
        sections.append(certifications.strip())

    content = "\n".join(sections).strip()

    # Run validation
    errors, warnings = validate_resume(content)

    return content, errors, warnings


def _dedupe_experience_bullets(lines):
    """
    Deduplicate bullet lines within experience, preserving role headers.
    """
    result = []
    bullet_lines = []
    non_bullet_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            bullet_lines.append(line)
        else:
            # Flush deduped bullets before the next header
            if bullet_lines:
                result.extend(dedupe_bullets(bullet_lines))
                bullet_lines = []
            result.append(line)

    # Flush remaining bullets
    if bullet_lines:
        result.extend(dedupe_bullets(bullet_lines))

    return result

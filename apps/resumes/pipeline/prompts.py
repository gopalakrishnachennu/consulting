"""
Prompt builder for the resume pipeline V3.1.

The admin writes ONE prompt template in MasterPrompt with {variables}.
This module fills in the variables with real data from JD intelligence,
matching, and consultant profile.

Available variables the admin can use in their prompt:
    {header}              — Name + contact line (copy exactly)
    {job_title}           — Target job title
    {company}             — Target company
    {seniority}           — junior/mid/senior/lead/principal
    {domain}              — Role domain (DevOps, Data Engineering, etc.)
    {location}            — Job location
    {required_skills}     — Comma-separated required skills from JD
    {preferred_skills}    — Comma-separated preferred skills from JD
    {tools}               — Comma-separated tools & technologies from JD
    {ats_keywords}        — ATS keywords to weave in
    {responsibilities}    — Key responsibilities from JD (bulleted)
    {match_pct}           — Skill match percentage
    {matched_skills}      — Skills the consultant HAS that JD requires
    {missing_skills}      — Skills the consultant LACKS that JD requires
    {coaching_keywords}   — Important terms to weave in where truthful
    {warnings}            — Match warnings (low overlap, etc.)
    {years}               — Total years of experience
    {consultant_skills}   — All consultant skills
    {experience_records}  — Formatted experience with titles, companies, dates, descriptions
    {education}           — Education section (copy exactly)
    {certifications}      — Certifications section (copy exactly)
"""
from django.utils import timezone


def _format_years(consultant):
    """Calculate total years of experience."""
    total_months = 0
    for exp in consultant.experience.all():
        start = exp.start_date
        end = exp.end_date or timezone.now().date()
        months = (end.year - start.year) * 12 + (end.month - start.month)
        total_months += max(months, 0)
    years = total_months // 12
    if years < 1 and total_months > 0:
        return "1"
    return str(years) if years else "1"


def _format_experience_records(consultant):
    """Format experience records for the prompt."""
    experiences = list(consultant.experience.all())
    if not experiences and consultant.base_resume_text:
        return f"BASE RESUME TEXT (extract roles from this):\n{consultant.base_resume_text[:3000]}"
    if not experiences:
        return "No experience provided."

    lines = []
    for i, exp in enumerate(experiences):
        start = exp.start_date.strftime("%b %Y") if exp.start_date else "N/A"
        end = exp.end_date.strftime("%b %Y") if exp.end_date else "Present"
        is_most_recent = (i == 0)
        bullet_target = "7-10 bullets" if is_most_recent else "6 bullets"

        lines.append(f"Role {i+1} ({'MOST RECENT' if is_most_recent else 'PREVIOUS'}) — {bullet_target}:")
        lines.append(f"  Title: {exp.title}")
        lines.append(f"  Company: {exp.company}")
        lines.append(f"  Dates: {start} - {end}")
        if exp.description:
            lines.append(f"  Description: {exp.description[:600].strip()}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_responsibilities(jd_intel):
    """Format JD responsibilities as bulleted list."""
    resp = jd_intel.get("responsibilities", [])
    if not resp:
        return "Not specified"
    return "\n".join(f"- {r}" for r in resp[:10])


def _format_warnings(matching):
    """Format matching warnings."""
    warnings = matching.get("warnings", [])
    if not warnings:
        return "None"
    return "\n".join(f"- {w}" for w in warnings)


# ── Default prompt (used if MasterPrompt has no content) ────────────

DEFAULT_PROMPT_TEMPLATE = """You are a Senior Resume Architect. Generate a complete, ATS-optimized, human-authentic resume.

ABSOLUTE RULES:
1. Every word must trace to the Candidate Profile or Job Intelligence. Nothing invented.
2. NEVER add companies, titles, dates, or certifications not in the Candidate Profile.
3. The resume must read as if the candidate wrote it — no AI filler, no fluff.
4. Use the EXACT header provided — do not change it.
5. Use the EXACT education and certifications provided — do not change them.
6. Output plain text only. No markdown, no bold, no tables, no code fences.

HEADER (copy exactly):
{header}

TARGET JOB:
Title: {job_title}
Company: {company}
Seniority: {seniority}
Domain: {domain}
Location: {location}
Required Skills: {required_skills}
Preferred Skills: {preferred_skills}
Tools & Technologies: {tools}
ATS Keywords: {ats_keywords}
Responsibilities:
{responsibilities}

SKILL MATCH ({match_pct}% match):
Matched: {matched_skills}
Missing (do NOT fake): {missing_skills}
Coaching Keywords (weave where truthful): {coaching_keywords}
{warnings}

CANDIDATE PROFILE:
Experience: {years} years
Skills: {consultant_skills}

EXPERIENCE RECORDS:
{experience_records}

EDUCATION & CERTIFICATIONS (copy exactly):
{education}
{certifications}

GENERATION RULES:
PROFESSIONAL SUMMARY:
- Single paragraph, exactly 70-80 words
- Start with the job title
- Include "{years} years" of experience
- Weave in 3-4 matched skills naturally
- No pronouns, no company names, no buzzwords

SKILLS:
- Key:value format, 6-10 categories
- Example: Cloud Platforms: AWS (EC2, S3, Lambda), Azure
- JD-required skills first in each category
- Only include skills the candidate actually has

PROFESSIONAL EXPERIENCE:
- Title | Company | Start - End format
- Keep titles, companies, dates EXACTLY as provided
- Most recent role: 7-10 bullets; others: 6 bullets
- Each bullet: 22-25 words
- Structure: [Action Verb] + [Technology] + [Outcome]
- Do NOT copy JD verbatim — rephrase as accomplishments
- Do NOT repeat phrases across bullets

EDUCATION & CERTIFICATIONS:
- Copy EXACTLY as provided. Do not modify."""


def build_prompt(jd_intel, matching, consultant, header, education, certifications,
                 prompt_template=None):
    """
    Build the complete prompt by filling {variables} in the template.

    If prompt_template is None, uses DEFAULT_PROMPT_TEMPLATE.
    The admin sets the template via MasterPrompt.system_prompt in the admin UI.
    """
    template = prompt_template or DEFAULT_PROMPT_TEMPLATE

    years = _format_years(consultant)
    consultant_skills = consultant.skills or []

    # Build all variable values
    variables = {
        "header": header or "",
        "job_title": jd_intel.get("job_title", "N/A"),
        "company": jd_intel.get("company", "N/A"),
        "seniority": jd_intel.get("seniority_level", "mid"),
        "domain": jd_intel.get("role_domain", "Technology"),
        "location": jd_intel.get("location", "N/A"),
        "required_skills": ", ".join(jd_intel.get("required_skills", [])) or "Not specified",
        "preferred_skills": ", ".join(jd_intel.get("preferred_skills", [])) or "Not specified",
        "tools": ", ".join(jd_intel.get("tools_and_technologies", [])) or "Not specified",
        "ats_keywords": ", ".join(jd_intel.get("keywords_for_ats", [])[:20]) or "Not specified",
        "responsibilities": _format_responsibilities(jd_intel),
        "match_pct": str(matching.get("match_pct", 0)),
        "matched_skills": ", ".join(matching.get("matched_required", [])) or "None",
        "missing_skills": ", ".join(matching.get("missing_required", [])) or "None",
        "coaching_keywords": ", ".join(matching.get("coaching_keywords", [])) or "None",
        "warnings": _format_warnings(matching),
        "years": years,
        "consultant_skills": ", ".join(consultant_skills[:40]) or "Not provided",
        "experience_records": _format_experience_records(consultant),
        "education": education or "Not provided",
        "certifications": certifications or "",
    }

    # Fill variables — use safe replacement (don't break on missing vars)
    prompt = template
    for key, value in variables.items():
        prompt = prompt.replace("{" + key + "}", str(value))

    return prompt

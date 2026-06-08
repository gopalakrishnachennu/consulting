"""
Prompt builder for the resume pipeline V3.

The code ALWAYS builds the data sections (JD intel, matching, experience, education).
The admin ONLY writes the generation rules in MasterPrompt.generation_rules.

Admin controls HOW the resume is written.
Code controls WHAT data goes in.
"""
from django.utils import timezone


def _format_years(consultant):
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


# ── Default rules (used if admin hasn't written any) ────────────────

DEFAULT_RULES = """PROFESSIONAL SUMMARY:
- Single paragraph, exactly 70-80 words
- Start with the job title
- Include total years of experience
- Weave in 3-4 matched skills naturally
- No pronouns (I, my, we), no company names
- No generic phrases like 'proven track record' or 'innovative solutions'
- Active voice, confident, grounded

SKILLS:
- Key:value format, 6-10 categories
- Example: Cloud Platforms: AWS (EC2, S3, Lambda), Azure (AKS)
- Put JD-required skills first in each category
- Only include skills the candidate actually has
- No bullets — only key:value lines

PROFESSIONAL EXPERIENCE:
- Use format: Title | Company | Start - End
- Keep titles, companies, dates EXACTLY as provided
- Most recent role: 7-10 bullets; all other roles: exactly 6 bullets
- Each bullet: 22-25 words exactly
- Each bullet structure: [Action Verb] + [Technology/Method] + [Outcome]
- Prefer concrete verbs: Built, Deployed, Configured, Automated, Reduced
- At most 1 elevated verb (Architected, Orchestrated) in entire section
- Do NOT copy JD text verbatim — rephrase as accomplishments
- Do NOT repeat phrases across bullets
- Most recent role: up to 2 quantified bullets (%, $, time)
- Older roles: max 1 quantified bullet each
- Do NOT keyword-stuff or list JD terms at end of bullets

EDUCATION & CERTIFICATIONS:
- Copy EXACTLY as provided. Do not modify, reorder, or add."""


SYSTEM_MESSAGE = "Generate a resume. Follow the instructions exactly. Output plain text only. No markdown, no bold, no code fences."


def build_prompt(jd_intel, matching, consultant, header, education, certifications,
                 admin_rules=None):
    """
    Build the full prompt. Code builds ALL data sections.
    admin_rules is the ONLY part the admin writes (from MasterPrompt.generation_rules).
    """
    years = _format_years(consultant)
    rules = admin_rules or DEFAULT_RULES

    parts = []

    # ── Identity ────────────────────────────────────────────────────
    parts.append("You are a Senior Resume Architect. Generate a complete, ATS-optimized resume.")
    parts.append("")
    parts.append("ABSOLUTE RULES:")
    parts.append("1. Every word must trace to the data below. Nothing invented.")
    parts.append("2. NEVER add companies, titles, dates, or certifications not provided.")
    parts.append("3. Output plain text only. No markdown, no bold, no code fences.")
    parts.append("4. Copy the HEADER, EDUCATION, and CERTIFICATIONS exactly as given.")
    parts.append("5. Output ONLY the resume. No preamble, NOTES, commentary, or explanation "
                 "before or after it. Begin directly with the candidate's name from the HEADER.")

    # ── Header ──────────────────────────────────────────────────────
    parts.append("")
    parts.append("HEADER (copy exactly):")
    parts.append(header)

    # ── Job Intelligence ────────────────────────────────────────────
    parts.append("")
    parts.append("TARGET JOB:")
    parts.append(f"Title: {jd_intel.get('job_title', 'N/A')}")
    parts.append(f"Company: {jd_intel.get('company', 'N/A')}")
    parts.append(f"Seniority: {jd_intel.get('seniority_level', 'mid')}")
    parts.append(f"Domain: {jd_intel.get('role_domain', 'Technology')}")

    req = jd_intel.get("required_skills", [])
    pref = jd_intel.get("preferred_skills", [])
    tools = jd_intel.get("tools_and_technologies", [])
    ats = jd_intel.get("keywords_for_ats", [])
    resp = jd_intel.get("responsibilities", [])

    if req:
        parts.append(f"Required Skills: {', '.join(req)}")
    if pref:
        parts.append(f"Preferred Skills: {', '.join(pref)}")
    if tools:
        parts.append(f"Tools: {', '.join(tools)}")
    if ats:
        parts.append(f"ATS Keywords (weave naturally): {', '.join(ats[:20])}")
    if resp:
        parts.append("Responsibilities:")
        for r in resp[:10]:
            parts.append(f"  - {r}")

    # ── Matching ────────────────────────────────────────────────────
    parts.append("")
    parts.append(f"SKILL MATCH: {matching.get('match_pct', 0)}%")

    matched = matching.get("matched_required", [])
    missing = matching.get("missing_required", [])
    coaching = matching.get("coaching_keywords", [])

    if matched:
        parts.append(f"Candidate HAS: {', '.join(matched)}")
    if missing:
        parts.append(f"Candidate LACKS (do NOT fake): {', '.join(missing)}")
    if coaching:
        parts.append(f"Weave these where truthful: {', '.join(coaching)}")
    for w in matching.get("warnings", []):
        parts.append(f"WARNING: {w}")

    # ── Candidate Profile ───────────────────────────────────────────
    parts.append("")
    parts.append("CANDIDATE:")
    parts.append(f"Experience: {years} years")

    consultant_skills = consultant.skills or []
    if consultant_skills:
        parts.append(f"Skills: {', '.join(consultant_skills[:40])}")

    experiences = list(consultant.experience.all())
    if experiences:
        parts.append("")
        parts.append("EXPERIENCE RECORDS:")
        for i, exp in enumerate(experiences):
            start = exp.start_date.strftime("%b %Y") if exp.start_date else "N/A"
            end = exp.end_date.strftime("%b %Y") if exp.end_date else "Present"
            is_recent = (i == 0)
            count = "7-10 bullets" if is_recent else "6 bullets"
            parts.append(f"  {'MOST RECENT' if is_recent else 'PREVIOUS'} ({count}):")
            parts.append(f"    {exp.title} | {exp.company} | {start} - {end}")
            if exp.description:
                parts.append(f"    {exp.description[:600].strip()}")
            parts.append("")
    elif consultant.base_resume_text:
        parts.append("")
        parts.append("BASE RESUME (extract roles from this):")
        parts.append(consultant.base_resume_text[:3000])
    else:
        parts.append("No experience provided.")

    # ── Education + Certs ───────────────────────────────────────────
    parts.append("")
    parts.append("EDUCATION & CERTIFICATIONS (copy exactly):")
    parts.append(education or "Not provided")
    if certifications:
        parts.append(certifications)

    # ── Admin Rules (THE ONLY PART THE ADMIN WRITES) ────────────────
    parts.append("")
    parts.append("GENERATION RULES:")
    parts.append(rules)

    return "\n".join(parts)

"""
Prompt builder for the resume pipeline V3.1.

System prompt + generation rules come from MasterPrompt (admin-editable).
This file only handles building the structured USER prompt with candidate
data, JD intelligence, and matching results.

The admin controls the entire LLM behavior from:
  Admin → Master Prompts → system_prompt (who the LLM is)
  Admin → Master Prompts → generation_rules (section rules)
"""
from django.utils import timezone


def _format_years(consultant):
    """Calculate total years of experience from experience records."""
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


# ── Fallback prompts (used only if MasterPrompt is not configured) ──

DEFAULT_SYSTEM_PROMPT = """You are a Senior Resume Architect. Generate a complete, ATS-optimized, human-authentic resume.

ABSOLUTE RULES:
1. Every word must trace to the Candidate Profile or Job Intelligence. Nothing invented.
2. NEVER add companies, titles, dates, or certifications not in the Candidate Profile.
3. The resume must read as if the candidate wrote it — no AI filler, no fluff.
4. Use the EXACT header (name, location, contact) provided — do not change it.
5. Use the EXACT education and certifications provided — do not change them.
6. Output plain text only. No markdown, no bold, no tables, no code fences, no separator lines.

OUTPUT STRUCTURE (use UPPERCASE headings exactly):
[Header — provided, copy exactly]

PROFESSIONAL SUMMARY
[Single paragraph, 70-80 words]

SKILLS
[Key:value format, 6-10 categories]

PROFESSIONAL EXPERIENCE
[Role headers + bullets]

EDUCATION
[Provided, copy exactly]

CERTIFICATIONS
[If provided, copy exactly]"""


DEFAULT_GENERATION_RULES = """PROFESSIONAL SUMMARY rules:
- Single paragraph, exactly 70-80 words
- Start with the job title
- Include total years of experience
- Weave in 3-4 matched skills naturally
- No pronouns (I, my, we), no company names
- No generic phrases ('proven track record', 'innovative solutions')
- Active voice, confident, grounded

SKILLS rules:
- Key:value format, 6-10 categories
- Example: Cloud Platforms: AWS (EC2, S3, Lambda), Azure (AKS)
- Put JD-required skills first in each category
- Only include skills the candidate actually has
- No bullets — only key:value lines

PROFESSIONAL EXPERIENCE rules:
- Use role Title | Company | Start - End format for each header
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


def get_system_prompt(master_prompt=None):
    """
    Get system prompt from MasterPrompt (admin-editable) or fallback.
    This is what the admin edits from the UI.
    """
    if master_prompt and master_prompt.system_prompt:
        return master_prompt.system_prompt
    return DEFAULT_SYSTEM_PROMPT


def get_generation_rules(master_prompt=None):
    """
    Get generation rules from MasterPrompt (admin-editable) or fallback.
    This is what the admin edits from the UI.
    """
    if master_prompt and master_prompt.generation_rules:
        return master_prompt.generation_rules
    return DEFAULT_GENERATION_RULES


def build_single_call_prompt(jd_intel, matching, consultant, header, education, certifications,
                              generation_rules=None):
    """
    Build ONE comprehensive user prompt with structured intelligence.

    The generation_rules parameter comes from MasterPrompt.generation_rules
    (admin-editable). If None, uses DEFAULT_GENERATION_RULES.
    """
    years = _format_years(consultant)
    experiences = list(consultant.experience.all())
    consultant_skills = consultant.skills or []
    rules = generation_rules or DEFAULT_GENERATION_RULES

    parts = []

    # ── Section 1: Header (copy exactly) ────────────────────────────
    parts.extend([
        "HEADER (copy this exactly — do not modify):",
        header,
    ])

    # ── Section 2: Job Intelligence ─────────────────────────────────
    parts.extend([
        "",
        "TARGET JOB INTELLIGENCE:",
        f"Title: {jd_intel.get('job_title', 'N/A')}",
        f"Company: {jd_intel.get('company', 'N/A')}",
        f"Seniority: {jd_intel.get('seniority_level', 'mid')}",
        f"Domain: {jd_intel.get('role_domain', 'Technology')}",
        f"Location: {jd_intel.get('location', 'N/A')}",
    ])

    req_skills = jd_intel.get("required_skills", [])
    pref_skills = jd_intel.get("preferred_skills", [])
    tools = jd_intel.get("tools_and_technologies", [])
    responsibilities = jd_intel.get("responsibilities", [])
    ats_keywords = jd_intel.get("keywords_for_ats", [])

    if req_skills:
        parts.append(f"Required Skills: {', '.join(req_skills)}")
    if pref_skills:
        parts.append(f"Preferred Skills: {', '.join(pref_skills)}")
    if tools:
        parts.append(f"Tools & Technologies: {', '.join(tools)}")
    if responsibilities:
        parts.append("Key Responsibilities:")
        for r in responsibilities[:10]:
            parts.append(f"  - {r}")
    if ats_keywords:
        parts.append(f"ATS Keywords (weave naturally): {', '.join(ats_keywords[:20])}")

    # ── Section 3: Skill Match Analysis ─────────────────────────────
    parts.extend([
        "",
        "SKILL MATCH ANALYSIS:",
        f"Match Score: {matching.get('match_pct', 0)}%",
    ])

    matched_req = matching.get("matched_required", [])
    missing_req = matching.get("missing_required", [])
    coaching = matching.get("coaching_keywords", [])

    if matched_req:
        parts.append(f"Matched Required: {', '.join(matched_req)}")
    if missing_req:
        parts.append(f"Missing Required (do NOT fake): {', '.join(missing_req)}")
    if coaching:
        parts.append(f"Coaching Keywords (weave where truthful): {', '.join(coaching)}")

    warnings = matching.get("warnings", [])
    for w in warnings:
        parts.append(f"WARNING: {w}")

    # ── Section 4: Candidate Profile ────────────────────────────────
    parts.extend([
        "",
        "CANDIDATE PROFILE:",
        f"Total Experience: {years} years",
    ])

    if consultant_skills:
        parts.append(f"Skills: {', '.join(consultant_skills[:40])}")

    if experiences:
        parts.append("")
        parts.append("EXPERIENCE RECORDS:")
        for i, exp in enumerate(experiences):
            start = exp.start_date.strftime("%b %Y") if exp.start_date else "N/A"
            end = exp.end_date.strftime("%b %Y") if exp.end_date else "Present"
            is_most_recent = (i == 0)
            bullet_target = "7-10 bullets" if is_most_recent else "6 bullets"

            parts.append(f"  Role {i+1} ({'MOST RECENT' if is_most_recent else 'PREVIOUS'}) — {bullet_target}:")
            parts.append(f"    Title: {exp.title}")
            parts.append(f"    Company: {exp.company}")
            parts.append(f"    Dates: {start} - {end}")
            if exp.description:
                desc = exp.description[:600].strip()
                parts.append(f"    Description: {desc}")
            parts.append("")
    elif consultant.base_resume_text:
        parts.append("")
        parts.append("BASE RESUME TEXT (extract roles from this):")
        parts.append(consultant.base_resume_text[:3000])
    else:
        parts.append("")
        parts.append("NO EXPERIENCE PROVIDED — generate realistic bullets based on skills and JD.")

    # ── Section 5: Education + Certs (copy exactly) ─────────────────
    parts.extend([
        "",
        "EDUCATION & CERTIFICATIONS (copy exactly — do not modify):",
    ])
    if education:
        parts.append(education)
    else:
        parts.append("EDUCATION\nNot provided")
    if certifications:
        parts.append("")
        parts.append(certifications)

    # ── Section 6: Generation Rules (from MasterPrompt) ─────────────
    parts.extend([
        "",
        "GENERATION RULES:",
        rules,
    ])

    return "\n".join(parts)

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
    parts.append("ABSOLUTE TRUTH RULES — these override every other instruction, including the generation rules and any length target:")
    parts.append("1. SKILLS: You may ONLY mention a technology, tool, platform, or skill if it "
                 "appears in the candidate's 'Skills' list or 'EXPERIENCE RECORDS' below. If a "
                 "skill from the TARGET JOB is NOT in the candidate's data, DO NOT use it anywhere "
                 "— not in the summary, not in SKILLS, not in any bullet. The job's required/ATS "
                 "keywords are targets, NOT facts about this candidate.")
    parts.append("2. METRICS: NEVER invent numbers. Use a percentage, dollar amount, time, or "
                 "count ONLY if that exact figure appears in the candidate's experience description "
                 "or base resume. If there is no real number, describe the impact in words with NO "
                 "number. Do not estimate, approximate, or make a number 'plausible'.")
    parts.append("3. NEVER add companies, titles, dates, certifications, or responsibilities not "
                 "in the data. Copy HEADER, EDUCATION, and CERTIFICATIONS exactly. Keep the "
                 "candidate's location exactly as in HEADER — never use the job's city.")
    parts.append("4. TRUTH OVER LENGTH: Reach length only by elaborating REAL work in depth. If "
                 "the candidate's real data only supports a shorter resume, produce the shorter "
                 "resume. A truthful 1-page resume is REQUIRED over a 2-page resume with any "
                 "invented skill, metric, or experience.")
    parts.append("5. Output plain text only — no markdown, bold, or code fences. Output ONLY the "
                 "resume itself: no preamble, no NOTES, no commentary before or after. Begin "
                 "directly with the candidate's name from the HEADER. (Skill gaps are shown to the "
                 "recruiter separately — do not write them here, and do not paper over them by "
                 "claiming the missing skill.)")

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

    parts.append("(The lists below describe what the JOB wants. They are NOT the candidate's "
                 "skills. Use a term from these lists ONLY if it also appears in the candidate's "
                 "data further down. Otherwise ignore it.)")
    if req:
        parts.append(f"Job wants (required): {', '.join(req)}")
    if pref:
        parts.append(f"Job wants (preferred): {', '.join(pref)}")
    if tools:
        parts.append(f"Job tools: {', '.join(tools)}")
    if ats:
        parts.append(f"ATS keywords (use ONLY those the candidate genuinely has): {', '.join(ats[:20])}")
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
        parts.append(f"Candidate HAS (safe to feature): {', '.join(matched)}")
    if missing:
        parts.append(f"Candidate LACKS — FORBIDDEN to mention anywhere in the resume "
                     f"(not summary, not skills, not bullets): {', '.join(missing)}")
    if coaching:
        parts.append(f"May use ONLY if the candidate genuinely has them: {', '.join(coaching)}")
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
            label = "MOST RECENT ROLE" if is_recent else "PREVIOUS ROLE"
            parts.append(f"  {label} (bullet count: follow the generation rules; write bullets "
                         f"ONLY from the description below — if it is short, write fewer bullets, "
                         f"do NOT invent work to fill space):")
            parts.append(f"    {exp.title} | {exp.company} | {start} - {end}")
            if exp.description:
                parts.append(f"    {exp.description[:2000].strip()}")
            parts.append("")
    elif consultant.base_resume_text:
        parts.append("")
        parts.append("BASE RESUME (extract roles and real detail from this — do not invent beyond it):")
        parts.append(consultant.base_resume_text[:5000])
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

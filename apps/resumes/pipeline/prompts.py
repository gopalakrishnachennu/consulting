"""
Section-specific prompt templates for the resume pipeline.

Each function builds a focused user prompt using structured JD data
and matching results — no raw JD text dumped into prompts.
"""


def _format_years(consultant):
    """Calculate total years of experience from experience records."""
    from django.utils import timezone
    total_months = 0
    for exp in consultant.experience.all():
        start = exp.start_date
        end = exp.end_date or timezone.now().date()
        months = (end.year - start.year) * 12 + (end.month - start.month)
        total_months += max(months, 0)
    years = total_months // 12
    if years < 1:
        return "1 year"
    return f"{years} years"


# ── Summary Prompt ──────────────────────────────────────────────────

SUMMARY_SYSTEM = (
    "You are a professional resume writer. Generate ONLY a PROFESSIONAL SUMMARY "
    "paragraph. Output the paragraph text only — no heading, no bullets, no extra text."
)

def build_summary_prompt(jd_intel, matching, consultant, section_rules=""):
    """Build user prompt for professional summary generation (~400 tokens input)."""
    years = _format_years(consultant)
    top_skills = matching.get("matched_required", [])[:8]
    responsibilities = jd_intel.get("responsibilities", [])[:5]
    coaching = matching.get("coaching_keywords", [])[:6]

    prompt = f"""Write a PROFESSIONAL SUMMARY for a resume targeting this role:

TARGET ROLE: {jd_intel.get('job_title', 'N/A')}
SENIORITY: {jd_intel.get('seniority_level', 'mid')}
DOMAIN: {jd_intel.get('role_domain', 'Technology')}

CANDIDATE PROFILE:
- Total experience: {years}
- Top matched skills: {', '.join(top_skills) if top_skills else 'N/A'}
- Key technologies: {', '.join(matching.get('matched_tools', [])[:6]) if matching.get('matched_tools') else 'N/A'}

KEY RESPONSIBILITIES TO REFLECT:
{chr(10).join(f'- {r}' for r in responsibilities) if responsibilities else '- General role responsibilities'}

RULES:
- Single paragraph, exactly 70-80 words
- Start with the job title from TARGET ROLE
- Include "{years}" of experience
- Naturally weave in at least 3-4 of the matched skills
- No pronouns (I, my, we), no company names, no buzzwords
- Do NOT use generic phrases like "innovative solutions" or "proven track record"
- Do NOT mention salary, location, or work arrangement
- Active voice, confident but grounded
- Mention collaboration with cross-functional teams
- Include a measurable outcome if possible"""

    if coaching:
        prompt += f"\n- Try to naturally include these terms: {', '.join(coaching[:4])}"

    if section_rules:
        prompt += f"\n\nADDITIONAL RULES:\n{section_rules}"

    return prompt


# ── Skills Prompt ───────────────────────────────────────────────────

SKILLS_SYSTEM = (
    "You are a resume skills section writer. Output ONLY key:value formatted "
    "skill categories. No heading, no bullets, no extra text."
)

def build_skills_prompt(jd_intel, matching, consultant, section_rules=""):
    """Build user prompt for skills section generation (~300 tokens input)."""
    matched_req = matching.get("matched_required", [])
    matched_pref = matching.get("matched_preferred", [])
    matched_tools = matching.get("matched_tools", [])
    coaching = matching.get("coaching_keywords", [])
    consultant_skills = consultant.skills or []

    prompt = f"""Generate a SKILLS section for a resume targeting:
ROLE: {jd_intel.get('job_title', 'N/A')}
DOMAIN: {jd_intel.get('role_domain', 'Technology')}

FORMAT: key:value lines (6-10 categories). Example:
Cloud Platforms: AWS (EC2, S3, Lambda), Azure (AKS, Functions)
Programming: Python, Java, Go, SQL
CI/CD: Jenkins, GitHub Actions, ArgoCD

CANDIDATE'S ACTUAL SKILLS:
{', '.join(consultant_skills[:30]) if consultant_skills else 'Not provided'}

JD REQUIRED SKILLS (must appear if candidate has them):
{', '.join(matched_req[:15]) if matched_req else 'None matched'}

JD PREFERRED SKILLS:
{', '.join(matched_pref[:10]) if matched_pref else 'None matched'}

TOOLS & TECHNOLOGIES FROM JD:
{', '.join(matched_tools[:15]) if matched_tools else 'None matched'}

RULES:
- Only include skills the candidate actually has (from their skills list)
- Group related skills into logical categories
- Put JD-critical skills first in each category
- No bullets — use key:value format only
- 6-10 categories total
- Do NOT invent skills the candidate does not have"""

    if coaching:
        prompt += f"\n- These JD terms are important but missing from candidate — include ONLY if candidate has adjacent experience: {', '.join(coaching[:6])}"

    if section_rules:
        prompt += f"\n\nADDITIONAL RULES:\n{section_rules}"

    return prompt


# ── Experience Prompt ───────────────────────────────────────────────

EXPERIENCE_SYSTEM = (
    "You are a professional resume writer specializing in experience bullets. "
    "Generate responsibilities bullets for each role provided. "
    "Return plain text with role headers and bullets only — no extra commentary."
)

def build_experience_prompt(jd_intel, matching, consultant, section_rules=""):
    """Build user prompt for experience section generation (~1200 tokens input)."""
    experiences = list(consultant.experience.all())
    coaching = matching.get("coaching_keywords", [])
    responsibilities = jd_intel.get("responsibilities", [])[:8]
    action_verbs = jd_intel.get("action_verbs", [])[:10]

    # Format experience records
    roles_text = []
    for i, exp in enumerate(experiences):
        start = exp.start_date.strftime("%b %Y") if exp.start_date else "N/A"
        end = exp.end_date.strftime("%b %Y") if exp.end_date else "Present"
        is_current = (i == 0)
        bullet_count = "7-10" if is_current else "6"

        role_block = f"ROLE {i+1} ({'MOST RECENT' if is_current else 'PREVIOUS'}):\n"
        role_block += f"  Title: {exp.title}\n"
        role_block += f"  Company: {exp.company}\n"
        role_block += f"  Dates: {start} – {end}\n"
        role_block += f"  Bullets needed: {bullet_count}\n"

        if exp.description:
            # Truncate to avoid bloating the prompt
            desc = exp.description[:500]
            role_block += f"  Description: {desc}\n"

        # Find which coaching keywords are relevant to this role
        if matching.get("experience_overlap"):
            for eo in matching["experience_overlap"]:
                if exp.company in eo.get("role", ""):
                    role_block += f"  Relevant JD keywords: {', '.join(eo['relevant_keywords'][:8])}\n"
                    break

        roles_text.append(role_block)

    prompt = f"""Generate PROFESSIONAL EXPERIENCE bullets for a resume targeting:
ROLE: {jd_intel.get('job_title', 'N/A')} at {jd_intel.get('company', 'N/A')}
SENIORITY: {jd_intel.get('seniority_level', 'mid')}
DOMAIN: {jd_intel.get('role_domain', 'Technology')}

OUTPUT FORMAT (use exactly this structure):
Title | Company | Start – End
- Bullet text here (22-25 words)
- Another bullet (22-25 words)

{chr(10).join(roles_text)}

JD RESPONSIBILITIES TO REFLECT:
{chr(10).join(f'- {r}' for r in responsibilities) if responsibilities else '- General role responsibilities'}

BULLET RULES:
- Each bullet: [Action Verb] + [Specific Technology/Method] + [Outcome/Impact]
- Each bullet must be 22-25 words — not fewer, not more
- Most recent role: 7-10 bullets; all other roles: exactly 6 bullets
- Use ONLY the provided role descriptions and JD as sources
- Do NOT invent companies, titles, dates, or technologies not in the description
- Do NOT copy JD responsibilities word-for-word — rephrase as personal accomplishments
- Do NOT repeat the same verb or phrase across bullets
- Prefer concrete verbs: Built, Deployed, Configured, Automated, Reduced, Improved
- At most 1 elevated verb (Architected, Orchestrated) in the entire section
- Most recent role: up to 2 quantified bullets (%, $, time reduction)
- Older roles: max 1 quantified bullet each
- Do NOT keyword-stuff or append lists of JD terms at the end of bullets
- Keep role title, company, and dates EXACTLY as provided"""

    if coaching:
        prompt += f"\n\nKEY JD TERMS TO WEAVE IN NATURALLY (where truthful):\n{', '.join(coaching)}"

    if section_rules:
        prompt += f"\n\nADDITIONAL RULES:\n{section_rules}"

    return prompt

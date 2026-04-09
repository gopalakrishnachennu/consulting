"""
Parse plain-text resume (LLM output) into structured JSON sections.

Expected format (matches master prompt OUTPUT FORMAT):

    FULL NAME
    City, State | email@... | 555-123-4567

    PROFESSIONAL SUMMARY
    Senior DevOps Engineer with 5 years...

    CORE SKILLS
    Cloud Platforms: AWS, Azure, GCP
    CI/CD & DevOps: Jenkins, GitHub Actions

    PROFESSIONAL EXPERIENCE

    Senior DevOps Engineer
    ExxonMobil | Jan 2022 - Present
    - Bullet 1...

    EDUCATION
    Bachelor of Technology in Computer Science
    JNTU Hyderabad | 2016 - 2020

    CERTIFICATIONS
    - AWS Certified Solutions Architect – Associate
"""
import re


# ─── Section boundary patterns ───────────────────────────────────────────────

_SECTION_PATTERNS = {
    'summary':        re.compile(r'^PROFESSIONAL\s+SUMMARY', re.I),
    'skills':         re.compile(r'^(CORE\s+SKILLS?|TECHNICAL\s+SKILLS?|KEY\s+SKILLS?|SKILLS?)', re.I),
    'experience':     re.compile(r'^PROFESSIONAL\s+EXPERIENCE|^WORK\s+EXPERIENCE|^EXPERIENCE', re.I),
    'education':      re.compile(r'^EDUCATION', re.I),
    'certifications': re.compile(r'^CERTIFICATIONS?', re.I),
}

_DATE_TOKEN = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|'
    r'January|February|March|April|June|July|August|September|October|November|December'
    r'|\d{4}|Present)\b', re.I
)


# ─── Public API ──────────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """
    Parse LLM-generated plain-text resume into structured dict.

    Returns:
        {
            "name": str,
            "contact": str,
            "summary": str,
            "skills": [{"category": str, "items": str}, ...],
            "experience": [
                {"title": str, "company": str, "dates": str, "bullets": [str, ...]},
                ...
            ],
            "education": [
                {"degree": str, "school": str, "dates": str},
                ...
            ],
            "certifications": [str, ...],
        }
    """
    lines = [ln.rstrip() for ln in text.strip().splitlines()]

    # ── Find section boundaries ───────────────────────────────────────
    section_starts: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        for key, pattern in _SECTION_PATTERNS.items():
            if key not in section_starts and pattern.match(stripped):
                section_starts[key] = i

    first_section_line = min(section_starts.values()) if section_starts else len(lines)

    # ── Name + Contact (header block before first section) ───────────
    header_lines = [ln.strip() for ln in lines[:first_section_line] if ln.strip()]
    name    = header_lines[0] if header_lines else ''
    contact = header_lines[1] if len(header_lines) >= 2 else ''

    # ── Helper: slice lines for one section ──────────────────────────
    def section_lines(key: str) -> list[str]:
        if key not in section_starts:
            return []
        start = section_starts[key] + 1   # skip the heading itself
        later = [v for k, v in section_starts.items() if v > section_starts[key]]
        end   = min(later) if later else len(lines)
        return lines[start:end]

    # ── Parse each section ───────────────────────────────────────────
    return {
        'name':           name,
        'contact':        contact,
        'summary':        _parse_summary(section_lines('summary')),
        'skills':         _parse_skills(section_lines('skills')),
        'experience':     _parse_experience(section_lines('experience')),
        'education':      _parse_education(section_lines('education')),
        'certifications': _parse_certifications(section_lines('certifications')),
    }


# ─── Section parsers ─────────────────────────────────────────────────────────

def _parse_summary(lines: list[str]) -> str:
    parts = []
    for ln in lines:
        s = ln.strip()
        if s:
            parts.append(s)
    return ' '.join(parts)


def _parse_skills(lines: list[str]) -> list[dict]:
    skills = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        # Strip leading bullets/asterisks
        s = re.sub(r'^[-•*]\s*', '', s)
        # Bold markdown: **Category**: items  or  **Category** items
        s = re.sub(r'\*{1,2}', '', s)
        if ':' in s:
            cat, _, items = s.partition(':')
            skills.append({'category': cat.strip(), 'items': items.strip()})
        elif s:
            skills.append({'category': '', 'items': s})
    return skills


def _parse_experience(lines: list[str]) -> list[dict]:
    """
    Each role block:
        Job Title               ← new role title (no bullet, no |+date)
        Company | Date - Date   ← company|dates line  (has | AND a date token)
        - Bullet ...            ← bullet lines
    """
    roles: list[dict] = []
    current: dict | None = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue

        is_bullet = bool(re.match(r'^[-•*·]\s+', s))
        is_company_dates = ('|' in s) and bool(_DATE_TOKEN.search(s))

        if is_bullet:
            if current is not None:
                bullet_text = re.sub(r'^[-•*·]\s*', '', s)
                current['bullets'].append(bullet_text)
            continue

        if is_company_dates and current is not None:
            parts = [p.strip() for p in s.split('|')]
            current['company'] = parts[0]
            current['dates']   = ' | '.join(parts[1:]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else '')
            continue

        # New role title
        current = {'title': s, 'company': '', 'dates': '', 'bullets': []}
        roles.append(current)

    # Remove empty ghost roles (just a title, no bullets or company)
    return [r for r in roles if r['bullets'] or r['company']]


def _parse_education(lines: list[str]) -> list[dict]:
    """
    Education blocks:
        Bachelor of Technology in Computer Science
        JNTU Hyderabad | 2016 - 2020
    or single-line:
        B.S. Computer Science | MIT | 2014
    """
    edu: list[dict] = []
    pending_degree: str | None = None

    for ln in lines:
        s = ln.strip()
        if not s:
            continue

        # Remove bullet markers
        s = re.sub(r'^[-•*]\s*', '', s)

        if '|' in s and _DATE_TOKEN.search(s):
            parts = [p.strip() for p in s.split('|')]
            if pending_degree:
                edu.append({
                    'degree': pending_degree,
                    'school': parts[0],
                    'dates':  ' | '.join(parts[1:]),
                })
                pending_degree = None
            else:
                # All-in-one: degree | school | dates
                edu.append({
                    'degree': parts[0],
                    'school': parts[1] if len(parts) > 1 else '',
                    'dates':  parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else ''),
                })
        else:
            if pending_degree:
                edu.append({'degree': pending_degree, 'school': '', 'dates': ''})
            pending_degree = s

    if pending_degree:
        edu.append({'degree': pending_degree, 'school': '', 'dates': ''})

    return edu


def _parse_certifications(lines: list[str]) -> list[str]:
    certs = []
    for ln in lines:
        s = re.sub(r'^[-•*]\s*', '', ln.strip())
        if s:
            certs.append(s)
    return certs

"""
Clean Resume Generation Engine (Phase 6 rewrite).

Replaces the multi-call pipeline with:
  1. Pre-flight compatibility check (before LLM call)
  2. Structured candidate input assembly (matches Master Prompt format)
  3. Single LLM call with the active MasterPrompt
  4. ATS scoring (reuses existing keyword scorer)

The old pipeline (services.py) is preserved for backward compatibility
but new generation flows should use this engine.
"""
import json
import re
import time
import logging
import openai

from django.utils import timezone
from django.db.models import Sum

from core.models import LLMConfig, LLMUsageLog
from core.security import decrypt_value
from core.llm_services import calculate_cost
from .models import MasterPrompt

logger = logging.getLogger("apps.resumes.engine")

# ─── Location Resolution ──────────────────────────────────────────────

# Major cities per state — ordered by size/relevance.
# When the JD city matches one of these, we pick the next one in the list.
_STATE_CITIES = {
    'AL': ['Birmingham', 'Montgomery', 'Huntsville', 'Mobile', 'Tuscaloosa'],
    'AK': ['Anchorage', 'Fairbanks', 'Juneau', 'Sitka'],
    'AZ': ['Phoenix', 'Tucson', 'Mesa', 'Scottsdale', 'Tempe', 'Chandler', 'Gilbert'],
    'AR': ['Little Rock', 'Fort Smith', 'Fayetteville', 'Springdale'],
    'CA': ['Los Angeles', 'San Diego', 'San Jose', 'San Francisco', 'Fresno', 'Sacramento', 'Oakland', 'Irvine', 'Fremont', 'Santa Ana'],
    'CO': ['Denver', 'Colorado Springs', 'Aurora', 'Fort Collins', 'Boulder', 'Lakewood'],
    'CT': ['Hartford', 'Bridgeport', 'New Haven', 'Stamford', 'Waterbury'],
    'DE': ['Wilmington', 'Dover', 'Newark', 'Middletown'],
    'FL': ['Jacksonville', 'Miami', 'Tampa', 'Orlando', 'St. Petersburg', 'Hialeah', 'Fort Lauderdale', 'Tallahassee'],
    'GA': ['Atlanta', 'Augusta', 'Columbus', 'Savannah', 'Athens', 'Sandy Springs'],
    'HI': ['Honolulu', 'Pearl City', 'Hilo', 'Kailua'],
    'ID': ['Boise', 'Nampa', 'Meridian', 'Idaho Falls'],
    'IL': ['Chicago', 'Aurora', 'Naperville', 'Joliet', 'Rockford', 'Springfield', 'Peoria'],
    'IN': ['Indianapolis', 'Fort Wayne', 'Evansville', 'South Bend', 'Carmel'],
    'IA': ['Des Moines', 'Cedar Rapids', 'Davenport', 'Sioux City'],
    'KS': ['Wichita', 'Overland Park', 'Kansas City', 'Olathe', 'Topeka'],
    'KY': ['Louisville', 'Lexington', 'Bowling Green', 'Owensboro'],
    'LA': ['New Orleans', 'Baton Rouge', 'Shreveport', 'Lafayette', 'Metairie'],
    'ME': ['Portland', 'Lewiston', 'Bangor', 'South Portland'],
    'MD': ['Baltimore', 'Frederick', 'Rockville', 'Gaithersburg', 'Bowie', 'Annapolis'],
    'MA': ['Boston', 'Worcester', 'Springfield', 'Cambridge', 'Lowell', 'Newton', 'Quincy'],
    'MI': ['Detroit', 'Grand Rapids', 'Warren', 'Sterling Heights', 'Ann Arbor', 'Lansing', 'Flint'],
    'MN': ['Minneapolis', 'Saint Paul', 'Rochester', 'Duluth', 'Bloomington', 'Plymouth'],
    'MS': ['Jackson', 'Gulfport', 'Southaven', 'Hattiesburg'],
    'MO': ['Kansas City', 'Saint Louis', 'Springfield', 'Columbia', 'Independence'],
    'MT': ['Billings', 'Missoula', 'Great Falls', 'Bozeman'],
    'NE': ['Omaha', 'Lincoln', 'Bellevue', 'Grand Island'],
    'NV': ['Las Vegas', 'Henderson', 'Reno', 'North Las Vegas', 'Sparks'],
    'NH': ['Manchester', 'Nashua', 'Concord', 'Dover'],
    'NJ': ['Newark', 'Jersey City', 'Paterson', 'Elizabeth', 'Edison', 'Woodbridge', 'Trenton', 'Cherry Hill'],
    'NM': ['Albuquerque', 'Las Cruces', 'Rio Rancho', 'Santa Fe', 'Roswell'],
    'NY': ['New York City', 'Buffalo', 'Rochester', 'Yonkers', 'Syracuse', 'Albany', 'White Plains', 'New Rochelle', 'Mount Vernon'],
    'NC': ['Charlotte', 'Raleigh', 'Greensboro', 'Durham', 'Winston-Salem', 'Cary', 'High Point'],
    'ND': ['Fargo', 'Bismarck', 'Grand Forks', 'Minot'],
    'OH': ['Columbus', 'Cleveland', 'Cincinnati', 'Toledo', 'Akron', 'Dayton'],
    'OK': ['Oklahoma City', 'Tulsa', 'Norman', 'Broken Arrow', 'Lawton'],
    'OR': ['Portland', 'Eugene', 'Salem', 'Gresham', 'Hillsboro', 'Beaverton'],
    'PA': ['Philadelphia', 'Pittsburgh', 'Allentown', 'Erie', 'Reading', 'King of Prussia', 'Bethlehem'],
    'RI': ['Providence', 'Warwick', 'Cranston', 'Pawtucket'],
    'SC': ['Columbia', 'Charleston', 'North Charleston', 'Mount Pleasant', 'Greenville'],
    'SD': ['Sioux Falls', 'Rapid City', 'Aberdeen'],
    'TN': ['Memphis', 'Nashville', 'Knoxville', 'Chattanooga', 'Clarksville', 'Murfreesboro'],
    'TX': ['Houston', 'San Antonio', 'Dallas', 'Fort Worth', 'El Paso', 'Arlington', 'Plano', 'Irving', 'Frisco', 'Garland'],
    'UT': ['Salt Lake City', 'West Valley City', 'Provo', 'West Jordan', 'Sandy', 'Orem'],
    'VT': ['Burlington', 'South Burlington', 'Rutland', 'Montpelier'],
    'VA': ['Virginia Beach', 'Norfolk', 'Chesapeake', 'Richmond', 'Arlington', 'Alexandria', 'Reston'],
    'WA': ['Seattle', 'Spokane', 'Tacoma', 'Vancouver', 'Bellevue', 'Kirkland', 'Redmond', 'Renton', 'Everett'],
    'WV': ['Charleston', 'Huntington', 'Morgantown', 'Parkersburg'],
    'WI': ['Milwaukee', 'Madison', 'Green Bay', 'Kenosha', 'Racine'],
    'WY': ['Cheyenne', 'Casper', 'Laramie'],
    'DC': ['Washington', 'Arlington', 'Alexandria', 'Bethesda'],
}

_STATE_NAME_TO_ABBR = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR',
    'California': 'CA', 'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE',
    'Florida': 'FL', 'Georgia': 'GA', 'Hawaii': 'HI', 'Idaho': 'ID',
    'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS',
    'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS',
    'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV',
    'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY',
    'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK',
    'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT',
    'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV',
    'Wisconsin': 'WI', 'Wyoming': 'WY', 'District of Columbia': 'DC',
}


def _parse_state_from_location(location_str):
    """
    Extract 2-letter state abbreviation from a location string.
    Handles formats like: "Seattle, WA", "Seattle WA", "Washington", "remote"
    Returns (city, state_abbr) or (None, None) if cannot parse.
    """
    if not location_str:
        return None, None
    loc = location_str.strip()

    # "Remote" / "Anywhere" / "United States" — no specific state
    if re.search(r'\bremote\b|\banywhere\b|\bunited states\b|\bus\b', loc, re.I):
        return None, None

    # Pattern: "City, ST" or "City, State"
    m = re.search(r',\s*([A-Z]{2})\b', loc)
    if m:
        state_abbr = m.group(1).upper()
        city = loc[:m.start()].strip()
        if state_abbr in _STATE_CITIES:
            return city, state_abbr

    # Full state name in string
    for full_name, abbr in _STATE_NAME_TO_ABBR.items():
        if full_name.lower() in loc.lower():
            # Try to extract city part before the state name
            city_part = re.split(re.escape(full_name), loc, flags=re.I)[0].strip().rstrip(',').strip()
            return city_part or None, abbr

    # 2-letter abbreviation anywhere (e.g. "Seattle WA")
    m = re.search(r'\b([A-Z]{2})\b', loc)
    if m:
        abbr = m.group(1)
        if abbr in _STATE_CITIES:
            city = loc[:m.start()].strip().rstrip(',').strip()
            return city or None, abbr

    return None, None


def get_resume_location(consultant, job=None, use_preferred=False):
    """
    Resolve the City, State to display in the resume personal header.

    Decision tree (matches UI checkbox logic):

    use_preferred=True  (checkbox CHECKED):
        → consultant.preferred_location is set?
            YES → use it exactly: "Jersey City, NJ"
            NO  → fall through to JD-derived logic below

    use_preferred=False (checkbox UNCHECKED, the DEFAULT):
        → always skip profile, go straight to JD-derived logic:
            JD city "Seattle, WA"       → pick a DIFFERENT WA city: "Spokane, WA"
            JD city "Austin, TX"        → pick a DIFFERENT TX city: "Houston, TX"
            JD city "San Francisco, CA" → pick a DIFFERENT CA city: "Los Angeles, CA"
            JD is Remote / no state     → "United States"

    Returns: (location_str, source)  where source is 'profile' or 'jd'
    """
    # ── BRANCH A: checkbox is checked → try preferred_location first ──────
    if use_preferred:
        pref = (getattr(consultant, 'preferred_location', '') or '').strip()
        if pref:
            return pref, 'profile'
        # preferred_location blank → fall through to JD-derived

    # ── BRANCH B: derive from JD state, strictly DIFFERENT city ───────────
    if job:
        jd_location = getattr(job, 'location', '') or ''
        jd_city, state_abbr = _parse_state_from_location(jd_location)
        if state_abbr and state_abbr in _STATE_CITIES:
            cities = _STATE_CITIES[state_abbr]
            jd_city_lower = (jd_city or '').lower().strip()
            for city in cities:
                if city.lower() != jd_city_lower:
                    return f"{city}, {state_abbr}", 'jd'
            # All cities exhausted (shouldn't happen) — take first regardless
            return f"{cities[0]}, {state_abbr}", 'jd'

    # ── FALLBACK: remote / no state info ──────────────────────────────────
    return 'United States', 'jd'


# Keys for which blocks go into the Master Prompt user message (candidate profile).
INPUT_SECTION_KEYS = (
    "personal",
    "experience",
    "education",
    "certifications",
    "skills",
    "total_years",
    "base_resume",
    "use_preferred_location",   # True = use profile location, False = derive from JD state
)
DEFAULT_INPUT_SECTIONS = {k: True for k in INPUT_SECTION_KEYS}
# use_preferred_location OFF by default — derive from JD state (different city) unless user checks the box
DEFAULT_INPUT_SECTIONS["use_preferred_location"] = False


def merge_input_sections(master, override_dict=None):
    """
    Start with all-True defaults, apply MasterPrompt.default_input_sections, then POST overrides.
    override_dict: full or partial dict from the generate page (values coerced to bool).
    """
    result = DEFAULT_INPUT_SECTIONS.copy()
    if master and getattr(master, "default_input_sections", None):
        for k, v in master.default_input_sections.items():
            if k in DEFAULT_INPUT_SECTIONS:
                result[k] = bool(v)
    if override_dict is not None:
        for k in INPUT_SECTION_KEYS:
            if k in override_dict:
                result[k] = bool(override_dict[k])
    return result


def parse_input_sections_from_request(request):
    """
    Read JSON from POST input_sections_json. Returns None if missing/invalid (use master defaults only).
    """
    raw = (request.POST.get("input_sections_json") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return {k: bool(data.get(k, DEFAULT_INPUT_SECTIONS[k])) for k in INPUT_SECTION_KEYS}
    except json.JSONDecodeError:
        return None


def validate_input_sections(sections):
    """Return error message string or None if OK."""
    if not sections.get("personal"):
        return "Personal details must be included for resume generation."
    if not any(
        sections.get(k) for k in ("experience", "education", "skills", "base_resume")
    ):
        return (
            "Select at least one of: Work experience, Education, Skills pool, or Base resume text."
        )
    return None


# ─── Pre-flight compatibility check ─────────────────────────────────

def preflight_check(job, consultant):
    """
    Compare consultant skills against JD keywords.
    Returns dict with match_pct, matched, missing, warnings.
    """
    from .services import extract_keywords

    jd_keywords = set(extract_keywords(job.description or "", max_keywords=50))
    consultant_skills = set()
    for skill in (consultant.skills or []):
        # Tokenize each skill into individual words for matching
        tokens = re.findall(r"[a-z0-9][a-z0-9+.#/-]{1,}", skill.lower())
        consultant_skills.update(tokens)

    # Also include tech from base_resume_text
    if consultant.base_resume_text:
        base_tokens = re.findall(r"[a-z0-9][a-z0-9+.#/-]{1,}", consultant.base_resume_text.lower())
        consultant_skills.update(base_tokens)

    # Also include tech from experience descriptions
    for exp in consultant.experience.all():
        if exp.description:
            exp_tokens = re.findall(r"[a-z0-9][a-z0-9+.#/-]{1,}", exp.description.lower())
            consultant_skills.update(exp_tokens)

    matched = jd_keywords & consultant_skills
    missing = jd_keywords - consultant_skills
    match_pct = round(len(matched) / len(jd_keywords) * 100) if jd_keywords else 0

    warnings = []
    if match_pct < 40:
        warnings.append(
            f"Low match ({match_pct}%). This JD may be a poor fit — "
            f"fewer than 40% of required technologies match the consultant's profile."
        )
    elif match_pct < 60:
        warnings.append(
            f"Moderate match ({match_pct}%). Resume will emphasize transferable skills."
        )

    return {
        "match_pct": match_pct,
        "matched": sorted(matched),
        "missing": sorted(missing),
        "jd_keyword_count": len(jd_keywords),
        "warnings": warnings,
    }


# ─── Structured input assembly ───────────────────────────────────────

def build_candidate_input(consultant, sections=None, master=None, location=None):
    """
    Build the CANDIDATE BASE PROFILE block matching the Master Prompt's
    expected INPUT 1 format. Pulled entirely from DB — no guessing.

    sections: merged dict of INPUT_SECTION_KEYS → bool. If None, uses merge_input_sections(master, None).
    location: pre-resolved location string from get_resume_location(). If None, falls back to preferred_location.
    """
    if sections is None:
        sections = merge_input_sections(master, None)

    user = consultant.user
    name = user.get_full_name() or user.username
    email = user.email or "Not provided"
    phone = consultant.phone or "Not provided"
    location = location or getattr(consultant, 'preferred_location', '') or "Not provided"

    # Education
    edu_lines = []
    for edu in consultant.education.all():
        end = edu.end_date.strftime('%Y') if edu.end_date else 'Present'
        edu_lines.append(f"  - {edu.degree} in {edu.field_of_study} — {edu.institution} ({end})")

    # Certifications
    cert_lines = []
    for cert in consultant.certifications.all():
        cert_lines.append(f"  - {cert.name} — {cert.issuing_organization} — {cert.issue_date.strftime('%Y')}")

    # Experience
    exp_blocks = []
    for exp in consultant.experience.all():
        end = "Present" if exp.is_current else (exp.end_date.strftime('%b %Y') if exp.end_date else 'N/A')
        start = exp.start_date.strftime('%b %Y') if exp.start_date else 'N/A'
        block = (
            f"  Role: {exp.title}\n"
            f"    Company: {exp.company}\n"
            f"    Start Date: {start}\n"
            f"    End Date: {end}\n"
            f"    Key Responsibilities: {exp.description or 'Not provided'}\n"
        )
        exp_blocks.append(block)

    # Calculate total years (from experience records; can be shown without full narrative)
    total_years = 0
    for exp in consultant.experience.all():
        end_date = exp.end_date or timezone.now().date()
        start_date = exp.start_date
        if start_date:
            total_years += (end_date - start_date).days / 365.25
    total_years = round(total_years)

    # Skills (master technology pool)
    skills_text = ", ".join(consultant.skills) if consultant.skills else "Not provided"

    parts = ["=== CANDIDATE BASE PROFILE ===\n\n"]

    if sections.get("personal", True):
        parts.append(
            f"PERSONAL DETAILS:\n"
            f"  Full Name: {name}\n"
            f"  Location: {location}\n"
            f"  Email: {email}\n"
            f"  Phone: {phone}\n\n"
        )

    if sections.get("education", True):
        parts.append(
            f"EDUCATION:\n"
            f"{chr(10).join(edu_lines) if edu_lines else '  Not provided'}\n\n"
        )

    if sections.get("certifications", True):
        parts.append(
            f"CERTIFICATIONS:\n"
            f"{chr(10).join(cert_lines) if cert_lines else '  None'}\n\n"
        )

    if sections.get("experience", True):
        parts.append(
            f"PROFESSIONAL EXPERIENCE:\n"
            f"{chr(10).join(exp_blocks) if exp_blocks else '  Not provided'}\n\n"
        )

    if sections.get("total_years", True):
        parts.append(f"TOTAL YEARS OF EXPERIENCE: {total_years}\n\n")

    if sections.get("skills", True):
        parts.append(
            f"MASTER TECHNOLOGY POOL:\n"
            f"  {skills_text}\n\n"
        )

    if sections.get("base_resume", True) and consultant.base_resume_text and consultant.base_resume_text.strip():
        parts.append(
            f"BASE RESUME TEXT (use as primary source for experience details):\n"
            f"{consultant.base_resume_text.strip()}\n\n"
        )

    parts.append("===\n")
    return "".join(parts)


def build_jd_input(job):
    """Build the JD input block matching the Master Prompt's INPUT 2 format."""
    return (
        f"=== JOB DESCRIPTION ===\n\n"
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location or 'Not specified'}\n"
        f"Type: {job.get_job_type_display()}\n\n"
        f"{job.description or 'No description provided.'}\n\n"
        f"===\n"
    )


# ─── Single-call generation ──────────────────────────────────────────

def generate_resume(job, consultant, actor=None, input_sections=None):
    """
    Generate a resume using a single LLM call with the active MasterPrompt.

    input_sections: optional dict (INPUT_SECTION_KEYS → bool) from the generate UI, merged with
    MasterPrompt.default_input_sections. None = use master defaults only.

    Returns: (content, tokens_used, error, metadata)
      - content: the generated resume text (or None on error)
      - tokens_used: total tokens consumed
      - error: error message string (or None on success)
      - metadata: dict with system_prompt, user_prompt, model, preflight info
    """
    # Get active master prompt
    master = MasterPrompt.get_active()
    if not master:
        return None, 0, "No active Master Prompt configured. Go to Settings → Master Prompt to set one up.", {}

    effective_sections = merge_input_sections(master, input_sections)

    # Resolve resume header location BEFORE building prompts
    # use_preferred_location toggle: True = use profile preferred_location, False = JD-derived
    use_pref = effective_sections.get("use_preferred_location", False)
    resolved_location, location_source = get_resume_location(consultant, job, use_preferred=use_pref)

    # Get LLM config
    config = LLMConfig.load()
    api_key = decrypt_value(config.encrypted_api_key)

    if not api_key or api_key.startswith('sk-your') or not config.generation_enabled:
        return None, 0, "Resume generation not available. Configure a valid OpenAI API key in Settings → LLM Config.", {}

    # Check token cap
    if config.monthly_token_cap:
        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total_month = LLMUsageLog.objects.filter(
            created_at__gte=month_start
        ).aggregate(total=Sum('total_tokens'))['total'] or 0
        if total_month >= config.monthly_token_cap and config.auto_disable_on_cap:
            config.generation_enabled = False
            config.save()
            return None, 0, "Monthly token cap reached. Generation disabled.", {}

    # Build prompts
    system_prompt = master.system_prompt

    candidate_input = build_candidate_input(consultant, sections=effective_sections, master=master, location=resolved_location)
    jd_input = build_jd_input(job)

    user_prompt = candidate_input + "\n" + jd_input
    if master.generation_rules:
        user_prompt += "\n\n" + master.generation_rules

    # Pre-flight
    pf = preflight_check(job, consultant)

    model = config.active_model or "gpt-4o-mini"
    temperature = float(config.temperature)
    max_tokens = config.max_output_tokens or 4000

    metadata = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "preflight": pf,
        "master_prompt_id": master.pk,
        "master_prompt_name": master.name,
        "input_sections": effective_sections,
        "resolved_location": resolved_location,
        "location_source": location_source,
    }

    # Single LLM call
    client = openai.OpenAI(api_key=api_key)
    try:
        start = time.time()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        latency_ms = int((time.time() - start) * 1000)

        content = response.choices[0].message.content
        prompt_tokens = response.usage.prompt_tokens if response.usage else 0
        completion_tokens = response.usage.completion_tokens if response.usage else 0
        total_tokens = response.usage.total_tokens if response.usage else 0

        costs = calculate_cost(model, prompt_tokens, completion_tokens)
        LLMUsageLog.objects.create(
            request_type='master_resume_generation',
            model_name=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            request_payload={"model": model, "temperature": temperature, "max_tokens": max_tokens},
            response_text=content or "",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_input=costs['input'],
            cost_output=costs['output'],
            cost_total=costs['total'],
            latency_ms=latency_ms,
            success=True,
            job=job,
            consultant=consultant,
            actor=actor,
        )
        return content, total_tokens, None, metadata

    except Exception as e:
        LLMUsageLog.objects.create(
            request_type='master_resume_generation',
            model_name=model,
            success=False,
            error_message=str(e),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            job=job,
            consultant=consultant,
            actor=actor,
        )
        return None, 0, str(e), metadata


# ─── Single-section targeted update ─────────────────────────────────

def generate_section(system_prompt, user_prompt, actor=None):
    """
    Single focused LLM call to regenerate one section of a draft.
    Used by DraftRegenerateSectionView.
    Returns: (content, tokens_used, error)
    """
    config = LLMConfig.load()
    api_key = decrypt_value(config.encrypted_api_key)

    if not api_key or api_key.startswith('sk-your') or not config.generation_enabled:
        return None, 0, "Resume generation not available. Configure API key in Settings → LLM Config."

    model = config.active_model or "gpt-4o-mini"
    temperature = float(config.temperature)
    max_tokens = min(config.max_output_tokens or 2000, 1500)  # sections are shorter

    client = openai.OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content
        total_tokens = response.usage.total_tokens if response.usage else 0
        return content, total_tokens, None
    except Exception as e:
        return None, 0, str(e)


# ─── ATS Scoring (reuse existing) ────────────────────────────────────

def score_resume(jd_text, resume_text):
    """Score keyword overlap between JD and generated resume."""
    from .services import score_ats
    return score_ats(jd_text, resume_text)

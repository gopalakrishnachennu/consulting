import json
import re
from typing import Dict, List, Tuple

from .services import LLMService
from .services import extract_keywords
from .prompt_strings import (
    REQUIRED_TERMS_SYSTEM_PROMPT,
    REQUIRED_TERMS_USER_PROMPT,
)


def extract_required_terms_from_jd(jd_text: str) -> List[str]:
    jd_text = jd_text or ""
    if not jd_text.strip():
        return []

    llm = LLMService()
    if llm.client:
        system_prompt = REQUIRED_TERMS_SYSTEM_PROMPT
        user_prompt = REQUIRED_TERMS_USER_PROMPT.format(jd_text=jd_text)
        content, _, error = llm.generate_with_prompts(None, None, system_prompt, user_prompt, force_new=True)
        if not error and content:
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    return [str(x).strip().lower() for x in data if str(x).strip()]
            except Exception:
                pass

    # Fallback: keyword extraction only (no hardcoded list)
    return extract_keywords(jd_text, max_keywords=40)


def _normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def _parse_skills_block(text: str) -> Dict[str, List[str]]:
    """
    Parse a SKILLS block of key:value lines.
    Returns {category: [items]}.
    """
    lines = [_normalize_line(l) for l in (text or "").splitlines() if _normalize_line(l)]
    out: Dict[str, List[str]] = {}
    for line in lines:
        if line.upper() == "SKILLS":
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        items = [i.strip() for i in value.split(",") if i.strip()]
        if items:
            out[key] = items
    return out


def _skills_has_soft_terms(cat: str, items: List[str]) -> bool:
    soft_terms = {
        "communication", "presentation", "soft skills", "documentation skills",
        "teamwork", "collaboration", "support", "skills", "excellent",
        "good", "strong", "proven", "passionate", "dynamic"
    }
    blob = (cat + " " + " ".join(items)).lower()
    return any(term in blob for term in soft_terms)


def _skills_validate_block(skills: Dict[str, List[str]]) -> Tuple[bool, List[str]]:
    reasons = []
    if not skills:
        reasons.append("empty")
        return False, reasons
    abstract_markers = {"pipeline", "warehousing", "infrastructure", "management", "aggregation", "standardization", "validation"}
    for cat, items in skills.items():
        if len(items) < 2 or len(items) > 8:
            reasons.append(f"bad_item_count:{cat}")
        if re.search(r"\b(tools|other|misc|required terms)\b", cat.lower()):
            reasons.append(f"generic_category:{cat}")
        if _skills_has_soft_terms(cat, items):
            reasons.append(f"soft_terms:{cat}")
        # Reject umbrella items
        umbrella = ["tools", "services", "platforms", "technologies", "solutions", "infrastructure", "processes"]
        for it in items:
            if any(u in it.lower() for u in umbrella):
                reasons.append(f"umbrella_item:{cat}")
            # Reject vague phrases without concrete tool/tech tokens
            if not re.search(r"[A-Z]{2,}|\\b([A-Za-z]+[0-9]*[A-Za-z]*|[A-Za-z]+\\.[A-Za-z]+)\\b", it):
                reasons.append(f"nonconcrete_item:{cat}")
            if any(m in it.lower() for m in abstract_markers):
                reasons.append(f"abstract_item:{cat}")
    return (len(reasons) == 0), reasons


def _skills_only_from_jd(skills: Dict[str, List[str]], jd_text: str) -> Dict[str, List[str]]:
    jd_lower = (jd_text or "").lower()
    jd_terms = set(extract_keywords(jd_text or "", max_keywords=400))
    cleaned: Dict[str, List[str]] = {}
    for category, items in skills.items():
        kept = []
        for item in items:
            item_l = item.lower()
            # keep if item (or its tokens) appear in JD
            tokens = set(extract_keywords(item, max_keywords=40))
            if item_l in jd_lower or tokens & jd_terms:
                kept.append(item)
        if kept:
            cleaned[category] = kept
    return cleaned


def _format_skills_block(skills: Dict[str, List[str]]) -> str:
    lines = ["SKILLS"]
    for category, items in skills.items():
        lines.append(f"{category}: {', '.join(items)}")
    return "\n".join(lines)


def _skills_only_from_experience(skills: Dict[str, List[str]], exp_text: str) -> Dict[str, List[str]]:
    exp_lower = (exp_text or "").lower()
    cleaned: Dict[str, List[str]] = {}
    for category, items in skills.items():
        kept = []
        for item in items:
            item_l = item.lower()
            tokens = set(extract_keywords(item, max_keywords=40))
            if item_l in exp_lower or tokens & set(extract_keywords(exp_text, max_keywords=400)):
                kept.append(item)
        if kept:
            cleaned[category] = kept
    return cleaned


def _drop_abstract_items(skills: Dict[str, List[str]]) -> Dict[str, List[str]]:
    abstract_markers = {"pipeline", "warehousing", "infrastructure", "management", "aggregation", "standardization", "validation"}
    cleaned: Dict[str, List[str]] = {}
    for cat, items in skills.items():
        concrete = [it for it in items if not any(m in it.lower() for m in abstract_markers)]
        if len(concrete) >= 2:
            cleaned[cat] = concrete
        else:
            cleaned[cat] = items
    return cleaned


def generate_skills_from_jd(job, required_terms=None, consultant=None) -> str:
    jd_text = (job.description or "").strip()
    if not jd_text:
        return "SKILLS\nSkills unavailable (JD not provided)."

    llm = LLMService()
    if not llm.client:
        return "SKILLS\nSkills unavailable (LLM not configured)."

    candidate_skills = []
    exp_text = ""
    if consultant is not None:
        candidate_skills = consultant.skills or []
        exp_parts = []
        for exp in consultant.experience.all():
            if exp.title:
                exp_parts.append(exp.title)
            if exp.description:
                exp_parts.append(exp.description)
        if getattr(consultant, "base_resume_text", ""):
            exp_parts.append(consultant.base_resume_text)
        exp_text = "\n".join(exp_parts)

    job_titles = []
    if consultant is not None:
        for exp in consultant.experience.all():
            if exp.title:
                job_titles.append(exp.title)
    job_titles_text = ", ".join(job_titles)
    seniority = "entry"
    if job_titles:
        title_blob = " ".join(job_titles).lower()
        if any(k in title_blob for k in ["lead", "principal", "staff", "architect"]):
            seniority = "lead"
        elif any(k in title_blob for k in ["senior", "sr"]):
            seniority = "senior"
        elif any(k in title_blob for k in ["ii", "iii"]):
            seniority = "mid"

    system_prompt = (
        "You are an expert resume skills extractor.\n"
        "Your job is to generate a clean, professional, ATS-optimized Technical Skills section for ANY "
        "resume in ANY field or domain.\n"
        "Return ONLY valid JSON. No explanation. No markdown. No extra text."
    )
    user_prompt = (
        "PHASE 1 — MINE ALL 4 SOURCES AGGRESSIVELY\n"
        "Extract clean skill names from ALL 4 sources.\n"
        "Do NOT skip any source.\n"
        "\nSOURCE 1 → JD Required Skills\n"
        "SOURCE 2 → JD Preferred/Bonus Skills\n"
        "SOURCE 3 → Candidate Experience Bullets\n"
        "           (mine every tool, tech, platform\n"
        "            mentioned inside bullet points)\n"
        "SOURCE 4 → Candidate Raw Skills List\n"
        "\nCLEANING RULE — Strip these phrases, keep name only:\n"
        "\"Basic understanding of X\"      → X\n"
        "\"Familiarity with X\"            → X\n"
        "\"Willingness to learn X\"        → X\n"
        "\"Knowledge of X\"                → X\n"
        "\"Experience with X\"             → X\n"
        "\"Proficiency in X\"              → X\n"
        "\"at least one X (e.g. A, B)\"   → A, B\n"
        "\"including X, Y, Z\"             → X, Y, Z separately\n"
        "\"such as X, Y\"                  → X, Y separately\n"
        "\"X and administration\"          → X\n"
        "\"X concepts\"                    → X\n"
        "\nNEVER extract:\n"
        "❌ Action verbs (monitor, deploy, troubleshoot,\n"
        "   implement, manage, support, develop)\n"
        "❌ Vague phrases (cloud services, scripting tools,\n"
        "   development tools, general concepts)\n"
        "❌ Soft skills (communication, teamwork,\n"
        "   leadership, collaboration, presentation)\n"
        "❌ Descriptions over 3 words that are not a\n"
        "   recognized tool, standard, or methodology name\n"
        "❌ Category names themselves as values\n"
        "\nPHASE 2 — UNDERSTAND THE CANDIDATE\n"
        "Before building categories, analyze:\n"
        "1. What is the JD role domain?\n"
        "2. What is the candidate's strongest area?\n"
        "3. What tools appear in BOTH JD and experience?\n"
        "4. What tools appear in experience only?\n"
        "5. What is the seniority level?\n"
        "\nPHASE 3 — FILL THE 9 SHELL CONCEPTS\n"
        "You have 9 fixed CONCEPTS.\n"
        "Each concept gets a DYNAMIC NAME (see Phase 4).\n"
        "Fill each concept with relevant skills from Phase 1.\n"
        "SKIP a concept if fewer than 2 relevant items found.\n"
        "MANDATORY: Minimum 6 shells. Maximum 10 shells.\n"
        "\nCONCEPT 1 → Cloud & Platforms\n"
        "CONCEPT 2 → Languages & Scripting\n"
        "CONCEPT 3 → Frameworks & Tools\n"
        "CONCEPT 4 → IaC & Automation\n"
        "CONCEPT 5 → Databases & Storage\n"
        "CONCEPT 6 → Monitoring & Operations\n"
        "CONCEPT 7 → Security & Compliance\n"
        "CONCEPT 8 → Methodologies & Practices\n"
        "CONCEPT 9 → Documentation & Collaboration\n"
        "\nPHASE 4 — NAME EACH SHELL DYNAMICALLY\n"
        "Do NOT use the concept name directly as category.\n"
        "Generate a UNIQUE name per candidate per shell.\n"
        "\nPHASE 5 — VALUE ORDERING RULES\n"
        "Inside each shell, order values like this:\n"
        "1st → Skills in BOTH JD and experience\n"
        "2nd → Skills in JD only (required first, then preferred)\n"
        "3rd → Skills in experience only (if relevant to role)\n"
        "4th → Candidate raw skills (if not already listed)\n"
        "\nEach shell: minimum 2 items, maximum 8 items\n"
        "Format: comma separated clean names only\n"
        "No umbrella terms as items\n"
        "No item can be same as category name\n"
        "\nPHASE 6 — SELF CHECK (mandatory before output)\n"
        "Verify ALL of these before returning:\n"
        "□ Minimum 6 shells populated?\n"
        "□ Maximum 10 shells not exceeded?\n"
        "□ Every item is a proper tool/tech/standard name?\n"
        "□ Zero sentences or JD phrases in values?\n"
        "□ Zero soft skills in any shell?\n"
        "□ Zero action verbs in any shell?\n"
        "□ No \"Required Skills\" or \"Preferred Skills\" shells created?\n"
        "□ Each shell has minimum 2 items?\n"
        "□ Category names are unique and dynamic?\n"
        "□ Did I mine experience bullets for tools?\n"
        "□ IaC & Automation shell checked and filled if applicable?\n"
        "□ Documentation shell populated for this candidate?\n"
        "\nPHASE 7 — OUTPUT FORMAT\n"
        "Return ONLY this JSON. Nothing else.\n"
        "{\n"
        "  \"skills\": [\n"
        "    {\n"
        "      \"category\": \"dynamic category name here\",\n"
        "      \"values\": \"Item1, Item2, Item3, Item4\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        f"\nJD Text: {jd_text}\n"
        f"Candidate Raw Skills: {', '.join(candidate_skills) if candidate_skills else ''}\n"
        f"Candidate Experience Bullets: {exp_text}\n"
        f"Candidate Job Titles: {job_titles_text}\n"
        f"Seniority Level: {seniority}\n"
    )

    # One generation attempt
    content, _, error = llm.generate_with_prompts(
        job,
        consultant,
        system_prompt,
        user_prompt,
        force_new=True,
        temperature_override=0.1,
    )
    if error or not content:
        return "SKILLS\nSkills unavailable (LLM error)."

    def _parse_json_block(text: str):
        try:
            return json.loads(text)
        except Exception:
            pass
        # Try to salvage JSON block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                return json.loads(snippet)
            except Exception:
                return None
        return None

    data = _parse_json_block(content)
    if data is None:
        # One repair attempt to coerce JSON-only output
        repair_prompt = (
            "Return ONLY valid JSON for this schema:\n"
            "{\n"
            "  \"skills\": [\n"
            "    {\"category\": \"category name\", \"values\": \"Item1, Item2\"}\n"
            "  ]\n"
            "}\n"
            "No explanation, no markdown.\n"
            f"RAW OUTPUT:\n{content}\n"
        )
        content_fix, _, error_fix = llm.generate_with_prompts(
            job,
            consultant,
            system_prompt,
            repair_prompt,
            force_new=True,
            temperature_override=0.1,
        )
        if not error_fix and content_fix:
            data = _parse_json_block(content_fix)
    if data is None:
        return "SKILLS\nSkills unavailable (parser output invalid)."

    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        skills = []

    def _extract_jd_tech_terms(text: str) -> List[str]:
        # Extract likely tool/tech terms from JD text without hardcoding lists
        if not text:
            return []
        terms = set()
        # Acronyms and service tokens (AWS, EC2, CI/CD, etc.)
        for m in re.findall(r"\b[A-Z][A-Z0-9/+-]{1,}\b", text):
            terms.add(m)
        # Title case phrases like "Windows Administration", "Linux Administration", "IIS Management"
        for m in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(Administration|Management|Services|Tools|Frameworks))\b", text):
            terms.add(m[0])
        # Common tech words in Title Case (e.g., Docker, Terraform)
        for m in re.findall(r"\b([A-Z][a-zA-Z0-9+.-]{1,})\b", text):
            if len(m) >= 3:
                terms.add(m)
        # Clean and filter
        cleaned = []
        for t in terms:
            t = t.strip()
            if not t:
                continue
            if _is_generic_item(t):
                continue
            cleaned.append(t)
        return _dedupe(cleaned)

    def _dedupe(values):
        seen = set()
        out = []
        for v in values:
            v = v.strip()
            if not v:
                continue
            if v.lower() in seen:
                continue
            seen.add(v.lower())
            out.append(v)
        return out

    def _normalize_item(item: str) -> str:
        text = item.strip()
        # Drop leading qualifier phrases to keep core skill
        text = re.sub(r"^(Basic understanding of|Knowledge of|Familiarity with|Willingness to learn)\s+", "", text, flags=re.I)
        text = re.sub(r"^general\s+", "", text, flags=re.I)
        # Collapse double spaces
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    def _is_generic_item(item: str) -> bool:
        item_l = item.lower().strip()
        if not item_l:
            return True
        if len(item.split()) > 3:
            return True
        # Filter vague/umbrella phrases that reduce realism
        generic_phrases = [
            "knowledge of", "understanding of", "familiarity with", "willingness to learn",
            "concepts", "principles", "skills", "tools", "services", "technologies",
            "cloud computing", "cloud infrastructure", "infrastructure automation",
            "automation", "ci/cd tools", "documentation", "presentation", "communication",
            "certification", "best practices", "continuous improvement",
            "security architecture", "network connectivity", "firewall concepts",
        ]
        if any(p in item_l for p in generic_phrases):
            return True
        # Single generic tokens are not helpful
        if item_l in {"automation", "ci/cd", "cloud", "infrastructure"}:
            return True
        return False

    def _filter_allowed_items(items: List[str]) -> List[str]:
        # Remove action verbs and vague terms; keep only tool/tech/methodology-like tokens
        banned = {
            "monitor", "monitoring", "troubleshoot", "troubleshooting", "deliver", "support",
            "implement", "manage", "develop", "deploy", "optimize", "configure",
            "devops", "cloud", "infrastructure", "automation",
        }
        cleaned = []
        for it in items:
            it = it.strip()
            if not it:
                continue
            if _is_generic_item(it):
                continue
            if it.lower() in banned:
                continue
            cleaned.append(it)
        return cleaned

    def _prefer_shorter(items):
        # Prefer shorter items when one contains the other (case-insensitive)
        cleaned = []
        for it in items:
            it_l = it.lower()
            if any(it_l != c.lower() and it_l in c.lower() for c in cleaned):
                continue
            # Remove longer variants already covered by this shorter item
            cleaned = [c for c in cleaned if not (c.lower() in it_l and c.lower() != it_l)]
            cleaned.append(it)
        return cleaned

    # Build a category map from LLM output first
    cat_map = {}
    for item in skills:
        category = (item.get("category") or "").strip()
        values = (item.get("values") or "").strip()
        if not category or not values:
            continue
        cat_map.setdefault(category, [])
        cat_map[category].extend([v.strip() for v in values.split(",") if v.strip()])

    # Apply allowed-item filter early
    for k in list(cat_map.keys()):
        cat_map[k] = _filter_allowed_items(cat_map[k])
        if not cat_map[k]:
            cat_map.pop(k, None)

    # Enforce JD-critical terms presence using a repair pass if needed
    jd_terms = _extract_jd_tech_terms(jd_text)
    if jd_terms and cat_map:
        current_values = " ".join([", ".join(v) for v in cat_map.values()]).lower()
        missing = [t for t in jd_terms if t.lower() not in current_values]
        if missing:
            repair_prompt = (
                "Fix the SKILLS JSON below to include ALL missing terms.\n"
                "Rules:\n"
                "- Keep 6–10 categories.\n"
                "- Each category must have 2–8 items.\n"
                "- Items must be tools/technologies/methodologies only.\n"
                "- Keep items comma-separated in values.\n"
                "- Do not add soft skills or vague phrases.\n"
                "- Return ONLY JSON.\n\n"
                f"MISSING TERMS: {', '.join(missing)}\n\n"
                f"CURRENT JSON:\n{json.dumps({'skills': skills})}\n"
            )
            content_fix, _, error_fix = llm.generate_with_prompts(
                job,
                consultant,
                system_prompt,
                repair_prompt,
                force_new=True,
                temperature_override=0.1,
            )
            if not error_fix and content_fix:
                repaired = _parse_json_block(content_fix)
                if isinstance(repaired, dict) and isinstance(repaired.get("skills"), list):
                    skills = repaired.get("skills")
                    cat_map = {}
                    for item in skills:
                        category = (item.get("category") or "").strip()
                        values = (item.get("values") or "").strip()
                        if not category or not values:
                            continue
                        cat_map.setdefault(category, [])
                        cat_map[category].extend([v.strip() for v in values.split(",") if v.strip()])

    # Build experience text for filtering items to JD/experience scope
    max_categories = 10
    min_items_per_category = 2
    max_items_per_category = 8

    def _build_lines_from_map(source_map: Dict[str, List[str]], min_items: int) -> List[str]:
        lines_out = ["SKILLS"]
        jd_terms = set(extract_keywords(jd_text, max_keywords=400))
        exp_terms = set(extract_keywords(exp_text, max_keywords=400)) if exp_text else set()
        for category, values in source_map.items():
            values = [_normalize_item(v) for v in values]
            filtered = []
            for v in values:
                if not v:
                    continue
                if _is_generic_item(v):
                    continue
                v_tokens = set(extract_keywords(v, max_keywords=20))
                if v_tokens & jd_terms or v_tokens & exp_terms:
                    filtered.append(v)
            values = _dedupe(filtered)
            values = _prefer_shorter(values)
            if len(values) < min_items:
                continue
            values = values[:max_items_per_category]
            lines_out.append(f"{category}: {', '.join(values)}")
            if len(lines_out) - 1 >= max_categories:
                break
        return lines_out

    # First pass
    lines = _build_lines_from_map(cat_map, min_items_per_category)

    # If too sparse, regroup from JD/experience pool using LLM (no hardcoded categories)
    if len(lines) - 1 < 6:
        pool_terms = _dedupe(
            [
                t
                for t in extract_keywords(f"{jd_text}\n{exp_text}", max_keywords=200)
                if t and not _is_generic_item(t)
            ]
        )
        if pool_terms:
            regroup_prompt = (
                "Group these terms into 6-10 ATS-friendly technical skill categories. "
                "Only include tools, technologies, or methodologies. Each category must have 2-8 items. "
                "Return ONLY JSON in this shape: {\"skills\":[{\"category\":\"...\",\"values\":\"A, B, C\"}]}\n"
                f"TERMS:\n{', '.join(pool_terms)}\n"
            )
            content2, _, error2 = llm.generate_with_prompts(
                job,
                consultant,
                system_prompt,
                regroup_prompt,
                force_new=True,
                temperature_override=0.1,
            )
            if not error2 and content2:
                try:
                    data2 = json.loads(content2)
                    skills2 = data2.get("skills") if isinstance(data2, dict) else None
                    if isinstance(skills2, list):
                        regroup_map: Dict[str, List[str]] = {}
                        for item in skills2:
                            category = (item.get("category") or "").strip()
                            values = (item.get("values") or "").strip()
                            if not category or not values:
                                continue
                            regroup_map.setdefault(category, [])
                            regroup_map[category].extend([v.strip() for v in values.split(",") if v.strip()])
                        if regroup_map:
                            lines = _build_lines_from_map(regroup_map, min_items_per_category)
                            if len(lines) - 1 < 6:
                                lines = _build_lines_from_map(regroup_map, 2)
                except Exception:
                    pass

    if len(lines) == 1:
        return "SKILLS\nSkills unavailable (no skills parsed)."
    return "\n".join(lines)

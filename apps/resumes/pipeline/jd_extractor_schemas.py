"""
JD Extraction Engine — schema, prompt, and structured-output validation.

P1a of the Resume Engine V4 plan. The extractor reads what the JD actually says
(no fixed vocabulary) and returns a rich, evidence-backed JSON. This module owns:
  - SCHEMA_VERSION / PROMPT_VERSION (drive the content-hash cache)
  - EXTRACTOR_SYSTEM_PROMPT (the contract the LLM must follow)
  - validate_parsed_jd() — the VAL_001..VAL_010 checks with pass/retry/warn outcomes

Nothing here calls the LLM; that lives in jd_extractor.py.
"""
from __future__ import annotations

SCHEMA_VERSION = "jd_v4.0"
PROMPT_VERSION = "jd_extract_v1"

# Status values stored on Job.parsed_jd_status (max_length=20 — all fit).
STATUS_OK_LLM = "OK_LLM"
STATUS_OK_LLM_WARN = "OK_LLM_WITH_WARNINGS"  # 20 chars — exactly at the limit
STATUS_RULES_FALLBACK = "OK_RULES_FALLBACK"
STATUS_FAILED = "FAILED_EXTRACTION"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"

VALID_SENIORITY = {
    "intern", "graduate", "entry", "junior", "mid",
    "senior", "lead", "manager", "director", "unknown",
}

EXTRACTOR_SYSTEM_PROMPT = """\
You are a JD Extraction Engine for a tech staffing firm. Extract ONLY what the job
description actually says into strict JSON. Do not invent. Do not squeeze the JD into
a fixed skill list — capture whatever stack/tools the JD uses, even uncommon or new ones.

HARD RULES
- Return ONE JSON object. No prose, no markdown fences.
- Every high-importance item (must_have skills, screen-out requirements, special resume
  requirements) MUST include a short verbatim `evidence_text` quoted from the JD.
- Keep both `raw_term` (as written in the JD) and `normalized_term` (clean canonical form).
- Separate resume-relevant content from noise. Put benefits / legal / pay-transparency /
  diversity / company-marketing text under `ignored_sections`, NOT under skills.
- If the JD lists alternatives ("Python OR Java OR Go"), express them as an entry in
  `alternative_requirement_groups` with type "one_of" — do NOT list them as all mandatory.
- If seniority is unclear, set it to "unknown" and add a warning. Never invent seniority.
- Detect mixed roles: set primary_role_family and any secondary_role_families with weights.
- Capture explicit resume instructions (graduation date, availability, portfolio, clearance,
  work authorization, GPA, relocation) in `special_resume_requirements`.
- Provide a confidence (0..1) on extracted groups and an overall extraction confidence.

OUTPUT SHAPE (fill what the JD supports; use [] / null / "unknown" when absent):
{
  "job_metadata": {"company_name": null, "job_title": "", "location": null,
    "employment_type": null, "job_code": null},
  "role_classification": {
    "primary_role_family": "", "sub_role": null,
    "secondary_role_families": [{"role_family": "", "weight": 0.0, "evidence_text": "", "confidence": 0.0}],
    "role_blend_summary": "", "seniority": "unknown", "seniority_evidence": null,
    "required_years": null, "resume_positioning_hint": ""},
  "degree_requirement": {"degree_required": false, "acceptable_levels": [], "fields": [],
    "equivalent_experience_allowed": false, "evidence_text": null, "confidence": 0.0},
  "requirements": {
    "screen_out_requirements": [{"requirement": "", "category": "work_authorization|location|degree|certification|clearance|availability|other", "evidence_text": "", "confidence": 0.0}],
    "must_have_skills": [{"raw_term": "", "normalized_term": "", "category": "", "importance": "screen_out|must_have|strongly_preferred|preferred|nice_to_have|context_only", "source_section": "", "evidence_text": "", "confidence": 0.0}],
    "nice_to_have_skills": [{"raw_term": "", "normalized_term": "", "category": "", "importance": "strongly_preferred|preferred|nice_to_have|context_only", "source_section": "", "evidence_text": "", "confidence": 0.0}],
    "alternative_requirement_groups": [{"group_label": "", "type": "one_of|any_of|all_of|at_least_n_of", "minimum_required": 1, "options": [{"raw_term": "", "normalized_term": "", "category": ""}], "importance": "must_have|strongly_preferred|preferred|nice_to_have", "evidence_text": "", "confidence": 0.0}]},
  "skill_categories": {"programming_languages": [], "cloud_platforms": [], "databases": [],
    "frameworks": [], "tools": [], "orchestration": [], "operating_systems": [],
    "testing_tools": [], "data_tools": [], "ml_ai_tools": [], "security_tools": [],
    "observability_tools": [], "other_technical_terms": []},
  "responsibility_themes": [{"theme": "", "depth": "assist|support|contribute|own|lead|design|maintain|develop|troubleshoot|optimize|analyze|document", "importance": "high|medium|low", "evidence_text": "", "confidence": 0.0}],
  "domain": {"primary_domain": null, "domain_keywords": [], "business_context": [], "evidence_text": null, "confidence": 0.0},
  "ats_keywords": [{"keyword": "", "category": "technical|role_title|domain|soft_skill|responsibility|tool|platform", "importance": "high|medium|low", "should_use_in_resume": true, "max_recommended_repetitions": 2}],
  "soft_skills": [{"skill": "", "importance": "high|medium|low", "evidence_text": "", "confidence": 0.0}],
  "special_resume_requirements": [{"requirement": "graduation_date|availability|portfolio|github|gpa|work_authorization|relocation|security_clearance|certification|other", "required": true, "resume_section": "Header|Education|Summary|Certifications|Projects|Other", "evidence_text": "", "confidence": 0.0}],
  "ignored_sections": [{"section_name": "Benefits|Pay Transparency|Diversity|Legal Notice|Company Marketing|Accommodation|Other", "reason": "", "content_summary": ""}],
  "exact_phrase_controls": [{"phrase": "", "use_in_resume": true, "max_repetitions": 2, "reason": ""}],
  "hidden_priorities": [{"priority": "", "evidence_text": "", "resume_relevance": "high|medium|low", "confidence": 0.0}],
  "extraction_quality": {"overall_extraction_confidence": 0.0, "needs_human_review": false,
    "low_confidence_items": [],
    "extraction_warnings": [{"type": "ambiguous_requirement|broad_skill_group|mixed_role|seniority_unclear|special_requirement_detected|noise_heavy_jd|other", "message": "", "severity": "low|medium|high"}]}
}
"""


def _is_list(v) -> bool:
    return isinstance(v, list)


def validate_parsed_jd(data) -> dict:
    """Run VAL_001..VAL_010. Returns:
        {"ok": bool, "needs_retry": bool, "errors": [...], "warnings": [...]}
    `errors` are hard failures (retry-worthy); `warnings` are allowed-with-warning.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # VAL_001 — JSON object shape
    if not isinstance(data, dict):
        return {"ok": False, "needs_retry": True, "errors": ["VAL_001: not a JSON object"], "warnings": []}

    role = data.get("role_classification") or {}
    reqs = data.get("requirements") or {}

    # VAL_002 — primary_role_family present
    if not (role.get("primary_role_family") or "").strip():
        errors.append("VAL_002: role_classification.primary_role_family missing")

    # VAL_003 — seniority present or 'unknown' (warn)
    sen = (role.get("seniority") or "").strip().lower()
    if not sen:
        warnings.append("VAL_003: seniority missing — defaulting to unknown")
    elif sen not in VALID_SENIORITY:
        warnings.append(f"VAL_003: seniority '{sen}' not in known set")

    # VAL_004 — at least one requirement / theme / skill extracted
    has_signal = (
        _is_list(reqs.get("must_have_skills")) and reqs["must_have_skills"]
    ) or (
        _is_list(reqs.get("nice_to_have_skills")) and reqs["nice_to_have_skills"]
    ) or (
        _is_list(data.get("responsibility_themes")) and data["responsibility_themes"]
    ) or (
        _is_list(reqs.get("alternative_requirement_groups")) and reqs["alternative_requirement_groups"]
    )
    if not has_signal:
        errors.append("VAL_004: no requirements, themes, or skills extracted")

    # VAL_005 — high-importance items include evidence_text
    missing_evidence = 0
    for item in (reqs.get("must_have_skills") or []):
        if (item.get("importance") in {"screen_out", "must_have"}) and not (item.get("evidence_text") or "").strip():
            missing_evidence += 1
    for item in (reqs.get("screen_out_requirements") or []):
        if not (item.get("evidence_text") or "").strip():
            missing_evidence += 1
    if missing_evidence:
        errors.append(f"VAL_005: {missing_evidence} high-importance item(s) lack evidence_text")

    # VAL_006 — noise not misfiled as must-have skills
    noise_terms = ("benefit", "401k", "pto", "equal opportunity", "diversity", "pay transparency")
    for item in (reqs.get("must_have_skills") or []):
        term = (item.get("normalized_term") or item.get("raw_term") or "").lower()
        if any(n in term for n in noise_terms):
            errors.append(f"VAL_006: noise term '{term}' misfiled as must_have skill")
            break

    # VAL_007 — alternative groups represented as groups (warn only)
    for grp in (reqs.get("alternative_requirement_groups") or []):
        if not _is_list(grp.get("options")) or len(grp.get("options") or []) < 2:
            warnings.append("VAL_007: alternative_requirement_group has <2 options")

    # VAL_008 — special requirements captured (advisory)
    if not _is_list(data.get("special_resume_requirements")):
        warnings.append("VAL_008: special_resume_requirements not a list")

    # VAL_009 — overall extraction confidence present
    eq = data.get("extraction_quality") or {}
    if eq.get("overall_extraction_confidence") is None:
        errors.append("VAL_009: extraction_quality.overall_extraction_confidence missing")

    # VAL_010 — handled in jd_extractor (parser_metadata is attached there)

    ok = not errors
    return {"ok": ok, "needs_retry": not ok, "errors": errors, "warnings": warnings}

"""
Legacy-vs-LLM parser comparison (Resume Engine V4 — P1a).

Used during migration to build confidence that the LLM extractor is at least as
good as `rule_parse_jd`. Pure function — no DB, no LLM. Sampled by the caller, not
run on every parse forever.
"""
from __future__ import annotations


def _llm_skill_terms(llm: dict) -> set[str]:
    out: set[str] = set()
    reqs = (llm or {}).get("requirements") or {}
    for key in ("must_have_skills", "nice_to_have_skills"):
        for item in (reqs.get(key) or []):
            term = (item.get("normalized_term") or item.get("raw_term") or "").strip().lower()
            if term:
                out.add(term)
    for grp in (reqs.get("alternative_requirement_groups") or []):
        for opt in (grp.get("options") or []):
            term = (opt.get("normalized_term") or opt.get("raw_term") or "").strip().lower()
            if term:
                out.add(term)
    cats = (llm or {}).get("skill_categories") or {}
    for vals in cats.values():
        for v in (vals or []):
            term = (v.get("normalized_term") or v.get("raw_term") or "").strip().lower() if isinstance(v, dict) else str(v).strip().lower()
            if term:
                out.add(term)
    return out


def _legacy_terms(legacy: dict) -> set[str]:
    out: set[str] = set()
    for key in ("required_skills", "preferred_skills", "tools_and_technologies", "keywords_for_ats"):
        for v in (legacy or {}).get(key, []) or []:
            if isinstance(v, str) and v.strip():
                out.add(v.strip().lower())
    return out


def diff_parsers(legacy: dict, llm: dict) -> dict:
    """Compare a legacy rule-parse dict against an LLM parsed_jd. Returns a small
    summary of what the LLM found that the legacy parser missed (and vice versa)."""
    legacy = legacy or {}
    llm = llm or {}
    legacy_set = _legacy_terms(legacy)
    llm_set = _llm_skill_terms(llm)

    role = (llm.get("role_classification") or {})
    specials = [s.get("requirement") for s in (llm.get("special_resume_requirements") or []) if s.get("requirement")]

    return {
        "new_skills_found_by_llm": sorted(llm_set - legacy_set)[:50],
        "legacy_only_terms": sorted(legacy_set - llm_set)[:50],
        "special_requirements_added_by_llm": specials,
        "llm_primary_role_family": role.get("primary_role_family") or "",
        "llm_seniority": role.get("seniority") or "unknown",
        "llm_has_alternative_groups": bool((llm.get("requirements") or {}).get("alternative_requirement_groups")),
        "llm_skill_count": len(llm_set),
        "legacy_skill_count": len(legacy_set),
        "notes": [
            f"LLM found {len(llm_set - legacy_set)} terms the rule parser missed",
        ],
    }

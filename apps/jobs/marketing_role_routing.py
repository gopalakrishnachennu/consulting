from __future__ import annotations

import logging
from typing import Iterable

from users.models import MarketingRole

logger = logging.getLogger(__name__)

_MAX_AUTO_ROLE_SLUGS = 3
_ROLE_MAP_CACHE_KEY = "marketing_role_map_v1"
_ROLE_MAP_CACHE_TTL = 300  # 5 minutes — new roles picked up automatically

_DEPARTMENT_FALLBACK_SLUGS: dict[str, list[str]] = {
    "software_dev": ["software-developer", "general-it"],
    "data_analytics": ["data-engineer", "data-analyst", "general-it"],
    "devops_cloud": ["devops-cloud", "general-it"],
    "security": ["cybersecurity", "general-it"],
    "it_support": ["it-support", "general-it"],
    "qa_testing": ["qa-test-engineer", "general-it"],
    "systems_network": ["network-systems", "general-it"],
    "it_management": ["it-management", "general-it"],
    "healthcare_it": ["healthcare-it", "general-it"],
    "management": ["operations", "general-business"],
    "sales": ["sales", "general-business"],
    "marketing": ["marketing-specialist", "general-business"],
    "hr": ["hr-recruiter", "general-business"],
    "finance": ["finance-accounting"],
    "operations": ["operations", "general-business"],
    "legal": ["general-business", "other-generalist"],
    "customer_success": ["customer-success", "general-business"],
    "design": ["general-business", "other-generalist"],
    "admin": ["administrative", "general-business"],
    "civil_eng": ["civil-engineer", "general-engineering"],
    "healthcare": ["clinical-nursing", "general-healthcare"],
    "other": ["other-generalist"],
}

_CATEGORY_FALLBACK_SLUGS: dict[str, list[str]] = {
    "AI / ML": ["ml-ai-engineer", "general-it"],
    "Data & Analytics": ["data-engineer", "data-analyst", "general-it"],
    "Security": ["cybersecurity", "general-it"],
    "DevOps / SRE": ["devops-cloud", "general-it"],
    "Engineering": ["software-developer", "general-it"],
    "Product": ["product-manager", "general-business"],
    "Design": ["general-business", "other-generalist"],
    "Marketing": ["marketing-specialist", "general-business"],
    "Sales": ["sales", "general-business"],
    "Customer Success": ["customer-success", "general-business"],
    "Finance": ["finance-accounting"],
    "HR & People": ["hr-recruiter", "general-business"],
    "Legal": ["general-business", "other-generalist"],
    "Operations": ["operations", "general-business"],
    "Healthcare": ["general-healthcare"],
    "Education": ["other-generalist"],
}

_TOP_CATEGORY_FALLBACK_SLUGS: dict[str, list[str]] = {
    "IT": ["general-it"],
    "ENGINEERING": ["general-engineering"],
    "HEALTHCARE": ["general-healthcare"],
    "NON_IT": ["general-business"],
    "FINANCE": ["finance-accounting"],
    "OTHER": ["other-generalist"],
}

_CATEGORY_TOP_CATEGORY: dict[str, str] = {
    "AI / ML": "IT",
    "Data & Analytics": "IT",
    "Security": "IT",
    "DevOps / SRE": "IT",
    "Engineering": "IT",
    "Product": "NON_IT",
    "Design": "NON_IT",
    "Marketing": "NON_IT",
    "Sales": "NON_IT",
    "Customer Success": "NON_IT",
    "Finance": "FINANCE",
    "HR & People": "NON_IT",
    "Legal": "NON_IT",
    "Operations": "NON_IT",
    "Healthcare": "HEALTHCARE",
    "Education": "OTHER",
}

_DEPARTMENT_TOP_CATEGORY: dict[str, str] = {
    "software_dev": "IT",
    "data_analytics": "IT",
    "devops_cloud": "IT",
    "security": "IT",
    "it_support": "IT",
    "qa_testing": "IT",
    "systems_network": "IT",
    "it_management": "IT",
    "healthcare_it": "IT",
    "management": "NON_IT",
    "sales": "NON_IT",
    "marketing": "NON_IT",
    "hr": "NON_IT",
    "finance": "FINANCE",
    "operations": "NON_IT",
    "legal": "NON_IT",
    "customer_success": "NON_IT",
    "design": "NON_IT",
    "admin": "NON_IT",
    "civil_eng": "ENGINEERING",
    "healthcare": "HEALTHCARE",
    "other": "OTHER",
}


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        slug = (item or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def _text_for_keyword_match(*parts: str) -> str:
    combined = " ".join((part or "").lower() for part in parts)
    return " ".join(combined.split())


def _active_role_map() -> dict[str, MarketingRole]:
    """
    Load all active MarketingRoles keyed by slug.
    Cached for 5 minutes so new roles added via GUI are picked up automatically
    within one cache window — no worker restart needed.
    """
    from django.core.cache import cache
    cached = cache.get(_ROLE_MAP_CACHE_KEY)
    if cached is not None:
        return cached
    role_map = {
        role.slug: role
        for role in MarketingRole.objects.filter(is_active=True)
    }
    cache.set(_ROLE_MAP_CACHE_KEY, role_map, _ROLE_MAP_CACHE_TTL)
    return role_map


def clear_marketing_role_cache() -> None:
    """Call this after creating/updating/deleting a MarketingRole so the cache
    refreshes on the next call rather than waiting the full 5 minutes."""
    from django.core.cache import cache
    cache.delete(_ROLE_MAP_CACHE_KEY)


def _role_keyword_matches(text: str, role_map: dict[str, MarketingRole]) -> list[str]:
    if not text:
        return []
    matched: list[str] = []
    for slug, role in role_map.items():
        keywords = role.match_keywords or []
        if any(keyword and keyword.lower() in text for keyword in keywords):
            matched.append(slug)
    return matched


def _fallback_slugs_for_top_category(top_category: str) -> list[str]:
    return _TOP_CATEGORY_FALLBACK_SLUGS.get((top_category or "").upper(), ["other-generalist"])


def _infer_top_category(job_category: str, department_normalized: str) -> str:
    department_key = (department_normalized or "").strip().lower()
    if department_key in _DEPARTMENT_TOP_CATEGORY:
        return _DEPARTMENT_TOP_CATEGORY[department_key]
    category_key = (job_category or "").strip()
    if category_key in _CATEGORY_TOP_CATEGORY:
        return _CATEGORY_TOP_CATEGORY[category_key]
    return "OTHER"


def infer_marketing_role_slugs(
    *,
    title: str = "",
    description: str = "",
    job_category: str = "",
    department_normalized: str = "",
    primary_domain: str = "",
    max_roles: int = _MAX_AUTO_ROLE_SLUGS,
) -> list[str]:
    """
    Resolve one or more MarketingRole slugs for a harvested job.

    The routing contract is:
    1. Prefer explicit domain classification / regex detection.
    2. Supplement with DB-configured MarketingRole.match_keywords.
    3. Fall back to broad department/category roles.
    4. Guarantee at least one catch-all role.
    """
    from harvest.enrichments import detect_job_domains

    role_map = _active_role_map()

    candidates: list[str] = []
    if primary_domain:
        candidates.append(primary_domain)

    candidates.extend(
        detect_job_domains(
            title or "",
            description or "",
            job_category or "",
            department_normalized or "",
            max_matches=max_roles,
        )
    )

    keyword_text = _text_for_keyword_match(title, description[:2000], job_category, department_normalized)
    candidates.extend(_role_keyword_matches(keyword_text, role_map))

    candidates.extend(_DEPARTMENT_FALLBACK_SLUGS.get((department_normalized or "").strip().lower(), []))
    candidates.extend(_CATEGORY_FALLBACK_SLUGS.get((job_category or "").strip(), []))
    candidates.extend(_fallback_slugs_for_top_category(_infer_top_category(job_category, department_normalized)))

    available = [slug for slug in _dedupe_preserve_order(candidates) if slug in role_map]
    if not available:
        available = [slug for slug in _fallback_slugs_for_top_category("OTHER") if slug in role_map]
    return available[:max_roles]


def infer_marketing_role_slugs_from_raw_job(raw_job, *, max_roles: int = _MAX_AUTO_ROLE_SLUGS) -> list[str]:
    from harvest.services.job_descriptions import job_description_for_sync

    return infer_marketing_role_slugs(
        title=raw_job.title or "",
        description=job_description_for_sync(raw_job),
        job_category=raw_job.job_category or "",
        department_normalized=raw_job.department_normalized or "",
        primary_domain=raw_job.job_domain or "",
        max_roles=max_roles,
    )


def assign_marketing_roles_to_job(job, *, raw_job=None, role_slugs: Iterable[str] | None = None) -> list[str]:
    """
    Assign auto-detected roles without wiping manually-added roles.

    We store the current auto-assigned role slugs on Job so re-sync/backfill can
    replace only the generated roles while preserving any manual additions.
    """
    role_map = _active_role_map()

    if role_slugs is None:
        if raw_job is not None:
            role_slugs = infer_marketing_role_slugs_from_raw_job(raw_job)
        else:
            role_slugs = infer_marketing_role_slugs(
                title=getattr(job, "title", "") or "",
                description=getattr(job, "description", "") or "",
                job_category="",
                department_normalized=getattr(job, "department", "") or "",
                primary_domain="",
            )

    auto_slugs = [slug for slug in _dedupe_preserve_order(role_slugs or []) if slug in role_map]
    if not auto_slugs:
        auto_slugs = [slug for slug in _fallback_slugs_for_top_category("OTHER") if slug in role_map]

    current_auto = set(getattr(job, "auto_marketing_role_slugs", []) or [])
    current_slugs = set(job.marketing_roles.values_list("slug", flat=True))
    manual_slugs = current_slugs - current_auto
    final_slugs = _dedupe_preserve_order([*manual_slugs, *auto_slugs])

    job.marketing_roles.set([role_map[slug] for slug in final_slugs if slug in role_map])

    if list(getattr(job, "auto_marketing_role_slugs", []) or []) != auto_slugs:
        job.auto_marketing_role_slugs = auto_slugs
        job.save(update_fields=["auto_marketing_role_slugs", "updated_at"])

    logger.debug(
        "Assigned marketing roles to job %s: auto=%s final=%s",
        getattr(job, "pk", None),
        auto_slugs,
        final_slugs,
    )
    return auto_slugs

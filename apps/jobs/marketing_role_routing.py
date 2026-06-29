from __future__ import annotations

import logging
from typing import Iterable

from django.utils import timezone

from .dual_classification.effective import effective_raw_job_classification
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
    effective = effective_raw_job_classification(raw_job)

    return infer_marketing_role_slugs(
        title=effective["title"] or "",
        description=job_description_for_sync(raw_job),
        job_category=effective["job_category"] or "",
        department_normalized=effective["department_normalized"] or "",
        primary_domain=effective["job_domain"] or "",
        max_roles=max_roles,
    )


def _approved_primary_role_slug(raw_job, role_map: dict[str, MarketingRole]) -> tuple[str, bool, str]:
    snapshot = getattr(raw_job, "classification_snapshot", None)
    if not snapshot:
        return "", False, ""
    slug = (getattr(snapshot, "approved_primary_role_slug", "") or "").strip()
    if slug and slug not in role_map:
        slug = ""
    return (
        slug,
        bool(getattr(snapshot, "primary_role_locked", False)),
        (getattr(snapshot, "primary_role_source", "") or "").strip(),
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
    snapshot_primary_slug = ""
    snapshot_primary_locked = False
    snapshot_primary_source = ""
    if raw_job is not None:
        snapshot_primary_slug, snapshot_primary_locked, snapshot_primary_source = _approved_primary_role_slug(raw_job, role_map)

    primary_slug = ""
    primary_source = ""
    primary_locked = False

    if getattr(job, "primary_marketing_role_locked", False) and getattr(job, "primary_marketing_role_id", None):
        locked_slug = getattr(getattr(job, "primary_marketing_role", None), "slug", "") or ""
        if locked_slug in role_map:
            primary_slug = locked_slug
            primary_source = (getattr(job, "primary_marketing_role_source", "") or "manual_override").strip() or "manual_override"
            primary_locked = True
    elif snapshot_primary_slug:
        primary_slug = snapshot_primary_slug
        primary_source = snapshot_primary_source or "approved_snapshot"
        primary_locked = snapshot_primary_locked
    elif auto_slugs:
        primary_slug = auto_slugs[0]
        primary_source = "auto"

    final_slugs = _dedupe_preserve_order([*( [primary_slug] if primary_slug else [] ), *manual_slugs, *auto_slugs])

    job.marketing_roles.set([role_map[slug] for slug in final_slugs if slug in role_map])

    update_fields: list[str] = []
    if list(getattr(job, "auto_marketing_role_slugs", []) or []) != auto_slugs:
        job.auto_marketing_role_slugs = auto_slugs
        update_fields.append("auto_marketing_role_slugs")

    resolved_primary_role = role_map.get(primary_slug) if primary_slug else None
    if getattr(job, "primary_marketing_role_id", None) != getattr(resolved_primary_role, "id", None):
        job.primary_marketing_role = resolved_primary_role
        update_fields.append("primary_marketing_role")
    if (getattr(job, "primary_marketing_role_source", "") or "") != (primary_source or ""):
        job.primary_marketing_role_source = primary_source or ""
        update_fields.append("primary_marketing_role_source")
    if bool(getattr(job, "primary_marketing_role_locked", False)) != bool(primary_locked):
        job.primary_marketing_role_locked = bool(primary_locked)
        update_fields.append("primary_marketing_role_locked")
    if update_fields:
        job.primary_marketing_role_updated_at = timezone.now()
        update_fields.extend(["primary_marketing_role_updated_at", "updated_at"])
        job.save(update_fields=update_fields)

    logger.debug(
        "Assigned marketing roles to job %s: auto=%s primary=%s final=%s",
        getattr(job, "pk", None),
        auto_slugs,
        primary_slug,
        final_slugs,
    )
    return auto_slugs

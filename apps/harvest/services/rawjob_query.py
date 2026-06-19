from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping

from django.conf import settings
from django.db import connection
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.timezone import make_aware

from harvest.models import RawJob
from harvest.runtime_config import get_ready_stage_min_confidence


DUPLICATE_SKIP_REASONS = ("DUPLICATE_RISK", "DUPLICATE_EXISTING")
POOL_ALLOWED_FILTER_DECISIONS = ("", "STRONG", "POSSIBLE")
FILTERED_OUT_DECISIONS = ("COLD", "NO_MATCH")
WEAK_OR_NON_TARGET_DOMAINS = (
    "",
    "general-business",
    "marketing-specialist",
    "other-generalist",
    "uncategorized",
    "unknown",
)

# Shared filter keys used by both Raw Jobs views and stats/funnel drill-downs.
FILTER_STATE_KEYS = (
    "q",
    "platform",
    "location_type",
    "employment_type",
    "experience_level",
    "department",
    "country",
    "state",
    "education_required",
    "years_min",
    "years_max",
    "salary_min_from",
    "salary_max_to",
    "clearance_required",
    "clearance_level",
    "language",
    "license",
    "encouraged",
    "certification",
    "benefit",
    "shift_schedule",
    "schedule_type",
    "weekend_required",
    "travel_min",
    "travel_max",
    "company_industry",
    "company_stage",
    "company_size",
    "company_funding",
    "resume_ready_min",
    "classification_min_conf",
    "classification_bucket",
    "founded_from",
    "founded_to",
    "sync_status",
    "stage",
    "pending_age_bucket",
    "is_remote",
    "is_active",
    "has_jd",
    "resume_jd",
    "fetched_from",
    "fetched_to",
    "last_hours",
    "date_from",
    "date_to",
    "company_id",
    "label_pk",
    "scope_status",
    "country_code",
    "marketing_role",
    "role_category",
    "filter_decision",
    "include_test",
    "search_by",
)


def _get(params: Mapping[str, str], key: str) -> str:
    return (params.get(key, "") or "").strip()


def effective_classification_q(min_conf: float = 0.01) -> Q:
    """
    Effective classification confidence filter.
    Prefer category_confidence when present; fallback to legacy
    classification_confidence for old rows.
    """
    return Q(category_confidence__gte=min_conf) | (
        Q(category_confidence__isnull=True) & Q(classification_confidence__gte=min_conf)
    )


def ready_stage_q(min_conf: float | None = None) -> Q:
    """READY rows for workflow board (active + JD present + confidence)."""
    if min_conf is None:
        min_conf = get_ready_stage_min_confidence()
    return (
        Q(is_test_run=False)
        & Q(has_description=True, is_active=True, is_cold=False, jd_fetch_skipped=False)
        & effective_classification_q(min_conf=min_conf)
        & (Q(filter_decision__isnull=True) | Q(filter_decision__in=POOL_ALLOWED_FILTER_DECISIONS))
        & ~Q(job_domain__in=WEAK_OR_NON_TARGET_DOMAINS)
    )


def filtered_out_q() -> Q:
    """Rows intentionally retained for audit but blocked from JD/pool flow."""
    return Q(is_cold=True) | Q(jd_fetch_skipped=True) | Q(filter_decision__in=FILTERED_OUT_DECISIONS)


def production_rawjobs_queryset() -> QuerySet[RawJob]:
    """RawJob base queryset for production dashboards and sync-facing views."""
    return RawJob.objects.filter(is_test_run=False)


def json_array_contains_q(field_name: str, value: str) -> Q:
    """
    JSON array membership predicate.

    PostgreSQL supports JSONField contains natively. Local SQLite does not, so
    use a quoted string fallback that still works for development/test probes.
    """
    clean = (value or "").strip()
    if not clean:
        return Q(pk__in=[])
    if connection.features.supports_json_field_contains:
        return Q(**{f"{field_name}__contains": [clean]})
    return Q(**{f"{field_name}__icontains": f'"{clean}"'})


def duplicate_rawjob_q() -> Q:
    """
    Canonical duplicate predicate.

    New rows should use SKIPPED plus sync_skip_reason. The DUPLICATE status is
    kept here only so legacy rows remain visible in dashboards.
    """
    return Q(sync_status=RawJob.SyncStatus.DUPLICATE) | Q(
        sync_status=RawJob.SyncStatus.SKIPPED,
        sync_skip_reason__in=DUPLICATE_SKIP_REASONS,
    )


def apply_stage_filter(qs: QuerySet[RawJob], stage: str) -> QuerySet[RawJob]:
    """Canonical stage predicates for funnel card click-through and stats."""
    stage_value = (stage or "").strip().upper()
    if stage_value == "FETCHED":
        return qs
    if stage_value == "PARSED":
        return qs.filter(has_description=True)
    if stage_value == "ENRICHED":
        return qs.filter(Q(quality_score__isnull=False) | Q(jd_quality_score__isnull=False))
    if stage_value == "CLASSIFIED":
        return qs.filter(effective_classification_q(min_conf=0.01))
    if stage_value == "READY":
        return qs.filter(ready_stage_q())
    if stage_value == "SYNCED":
        return qs.filter(sync_status=RawJob.SyncStatus.SYNCED)
    if stage_value == "FAILED":
        return qs.filter(sync_status=RawJob.SyncStatus.FAILED)
    if stage_value == "DUPLICATE":
        return qs.filter(duplicate_rawjob_q())
    return qs


def build_funnel_counts(base_qs: QuerySet[RawJob] | None = None) -> dict[str, int]:
    qs = base_qs if base_qs is not None else RawJob.objects.all()
    return {
        "fetched": qs.count(),
        "parsed": apply_stage_filter(qs, "PARSED").count(),
        "enriched": apply_stage_filter(qs, "ENRICHED").count(),
        "classified": apply_stage_filter(qs, "CLASSIFIED").count(),
        "ready": apply_stage_filter(qs, "READY").count(),
        "synced": apply_stage_filter(qs, "SYNCED").count(),
    }


def apply_rawjob_filters(qs: QuerySet[RawJob], params: Mapping[str, str]) -> QuerySet[RawJob]:
    """Canonical RawJob filters shared by HTML + JSON + funnel drill-downs."""

    include_test = _get(params, "include_test").lower()
    if include_test in {"only", "test"}:
        qs = qs.filter(is_test_run=True)
    elif include_test not in {"1", "true", "yes", "all"}:
        qs = qs.filter(is_test_run=False)

    q = _get(params, "q")
    search_by = _get(params, "search_by") or "title"
    if q:
        if search_by == "company":
            qs = qs.filter(Q(company_name__icontains=q))
        else:
            qs = qs.filter(Q(title__icontains=q))

    company_id_f = _get(params, "company_id")
    if company_id_f.isdigit():
        qs = qs.filter(company_id=int(company_id_f))

    label_pk_f = _get(params, "label_pk")
    if label_pk_f.isdigit():
        qs = qs.filter(platform_label_id=int(label_pk_f))

    platform_f = _get(params, "platform")
    if platform_f:
        qs = qs.filter(platform_slug=platform_f)

    location_f = _get(params, "location_type")
    if location_f:
        qs = qs.filter(location_type=location_f)

    employment_f = _get(params, "employment_type")
    if employment_f:
        qs = qs.filter(employment_type=employment_f)

    exp_f = _get(params, "experience_level")
    if exp_f:
        qs = qs.filter(experience_level=exp_f)

    dept_f = _get(params, "department")
    if dept_f:
        qs = qs.filter(Q(department_normalized__icontains=dept_f) | Q(department__icontains=dept_f))

    country_f = _get(params, "country")
    if country_f:
        qs = qs.filter(country__icontains=country_f)

    state_f = _get(params, "state")
    if state_f:
        qs = qs.filter(state__icontains=state_f)

    edu_f = _get(params, "education_required")
    if edu_f:
        qs = qs.filter(education_required=edu_f)

    years_min_f = _get(params, "years_min")
    if years_min_f.isdigit():
        qs = qs.filter(years_required__gte=int(years_min_f))

    years_max_f = _get(params, "years_max")
    if years_max_f.isdigit():
        qs = qs.filter(years_required__lte=int(years_max_f))

    salary_min_from_f = _get(params, "salary_min_from")
    try:
        if salary_min_from_f:
            qs = qs.filter(salary_min__gte=float(salary_min_from_f))
    except ValueError:
        pass

    salary_max_to_f = _get(params, "salary_max_to")
    try:
        if salary_max_to_f:
            qs = qs.filter(salary_max__lte=float(salary_max_to_f))
    except ValueError:
        pass

    clear_f = _get(params, "clearance_required")
    if clear_f == "1":
        qs = qs.filter(clearance_required=True)
    elif clear_f == "0":
        qs = qs.filter(clearance_required=False)

    clearance_level_f = _get(params, "clearance_level")
    if clearance_level_f:
        qs = qs.filter(clearance_level__icontains=clearance_level_f)

    lang_f = _get(params, "language")
    if lang_f:
        try:
            qs = qs.filter(languages_required__contains=[lang_f])
        except Exception:
            qs = qs.filter(languages_required__icontains=lang_f)

    shift_f = _get(params, "shift_schedule")
    if shift_f:
        qs = qs.filter(shift_schedule__icontains=shift_f)

    schedule_f = _get(params, "schedule_type")
    if schedule_f:
        qs = qs.filter(schedule_type__icontains=schedule_f)

    weekend_f = _get(params, "weekend_required")
    if weekend_f == "1":
        qs = qs.filter(weekend_required=True)
    elif weekend_f == "0":
        qs = qs.filter(weekend_required=False)

    travel_min_f = _get(params, "travel_min")
    if travel_min_f.isdigit():
        qs = qs.filter(travel_pct_max__gte=int(travel_min_f))

    travel_max_f = _get(params, "travel_max")
    if travel_max_f.isdigit():
        qs = qs.filter(travel_pct_min__lte=int(travel_max_f))

    license_f = _get(params, "license")
    if license_f:
        try:
            qs = qs.filter(licenses_required__contains=[license_f])
        except Exception:
            qs = qs.filter(licenses_required__icontains=license_f)

    encouraged_f = _get(params, "encouraged")
    if encouraged_f:
        try:
            qs = qs.filter(encouraged_to_apply__contains=[encouraged_f])
        except Exception:
            qs = qs.filter(encouraged_to_apply__icontains=encouraged_f)

    cert_f = _get(params, "certification")
    if cert_f:
        try:
            qs = qs.filter(certifications__contains=[cert_f])
        except Exception:
            qs = qs.filter(certifications__icontains=cert_f)

    benefit_f = _get(params, "benefit")
    if benefit_f:
        try:
            qs = qs.filter(benefits_list__contains=[benefit_f])
        except Exception:
            qs = qs.filter(benefits_list__icontains=benefit_f)

    industry_f = _get(params, "company_industry")
    if industry_f:
        qs = qs.filter(company_industry__icontains=industry_f)

    company_stage_f = _get(params, "company_stage")
    if company_stage_f:
        qs = qs.filter(company_stage__icontains=company_stage_f)

    size_f = _get(params, "company_size")
    if size_f:
        qs = qs.filter(
            Q(company_size__icontains=size_f) | Q(company_employee_count_band__icontains=size_f)
        )

    funding_f = _get(params, "company_funding")
    if funding_f:
        qs = qs.filter(company_funding__icontains=funding_f)

    resume_score_f = _get(params, "resume_ready_min")
    try:
        if resume_score_f:
            qs = qs.filter(resume_ready_score__gte=float(resume_score_f))
    except ValueError:
        pass

    conf_min_f = _get(params, "classification_min_conf")
    try:
        if conf_min_f:
            qs = qs.filter(effective_classification_q(min_conf=float(conf_min_f)))
    except ValueError:
        pass

    conf_bucket = _get(params, "classification_bucket").lower()
    if conf_bucket:
        low_cut = get_ready_stage_min_confidence()
        any_classified_q = effective_classification_q(min_conf=0.01)
        high_q = effective_classification_q(min_conf=low_cut)
        if conf_bucket == "low":
            qs = qs.filter(any_classified_q).exclude(high_q)
        elif conf_bucket == "high":
            qs = qs.filter(high_q)
        elif conf_bucket == "missing":
            qs = qs.exclude(any_classified_q)

    founded_from = _get(params, "founded_from")
    if founded_from.isdigit():
        qs = qs.filter(company_founding_year__gte=int(founded_from))

    founded_to = _get(params, "founded_to")
    if founded_to.isdigit():
        qs = qs.filter(company_founding_year__lte=int(founded_to))

    sync_f = _get(params, "sync_status")
    if sync_f:
        qs = qs.filter(sync_status=sync_f)

    stage_f = _get(params, "stage")
    if stage_f:
        qs = apply_stage_filter(qs, stage_f)

    remote_f = _get(params, "is_remote")
    if remote_f == "1":
        qs = qs.filter(is_remote=True)
    elif remote_f == "0":
        qs = qs.filter(is_remote=False)

    active_f = _get(params, "is_active")
    if active_f == "1":
        qs = qs.filter(is_active=True)
    elif active_f == "0":
        qs = qs.filter(is_active=False)

    jd_f = _get(params, "has_jd")
    if jd_f == "1":
        qs = qs.filter(has_description=True)
    elif jd_f == "0":
        qs = qs.filter(has_description=False)

    resume_jd_f = _get(params, "resume_jd")
    if resume_jd_f == "ready":
        qs = qs.filter(
            has_description=True,
            is_active=True,
            word_count__gte=max(1, int(getattr(settings, "RESUME_JD_MIN_WORDS", 80))),
        ).filter(
            effective_classification_q(
                min_conf=float(getattr(settings, "RESUME_JD_MIN_CLASSIFICATION_CONFIDENCE", 0.35))
            )
        )
    elif resume_jd_f == "blocked":
        min_words = max(1, int(getattr(settings, "RESUME_JD_MIN_WORDS", 80)))
        min_conf = float(getattr(settings, "RESUME_JD_MIN_CLASSIFICATION_CONFIDENCE", 0.35))
        qs = qs.filter(
            Q(has_description=False)
            | Q(is_active=False)
            | Q(word_count__lt=min_words)
            | Q(category_confidence__lt=min_conf)
            | (
                Q(category_confidence__isnull=True)
                & (Q(classification_confidence__lt=min_conf) | Q(classification_confidence__isnull=True))
            )
        )

    fetched_from = _get(params, "fetched_from")
    if fetched_from:
        try:
            qs = qs.filter(fetched_at__gte=make_aware(datetime.strptime(fetched_from, "%Y-%m-%d")))
        except ValueError:
            pass

    fetched_to = _get(params, "fetched_to")
    if fetched_to:
        try:
            next_day = datetime.strptime(fetched_to, "%Y-%m-%d") + timedelta(days=1)
            qs = qs.filter(fetched_at__lt=make_aware(next_day))
        except ValueError:
            pass

    last_hours = _get(params, "last_hours")
    if last_hours.isdigit():
        hours = max(1, min(720, int(last_hours)))
        qs = qs.filter(fetched_at__gte=timezone.now() - timedelta(hours=hours))

    pending_age_bucket = _get(params, "pending_age_bucket")
    if pending_age_bucket:
        now = timezone.now()
        qs = qs.filter(sync_status=RawJob.SyncStatus.PENDING)
        if pending_age_bucket == "lt_1h":
            qs = qs.filter(fetched_at__gte=now - timedelta(hours=1))
        elif pending_age_bucket == "h_1_6":
            qs = qs.filter(fetched_at__lt=now - timedelta(hours=1), fetched_at__gte=now - timedelta(hours=6))
        elif pending_age_bucket == "h_6_24":
            qs = qs.filter(fetched_at__lt=now - timedelta(hours=6), fetched_at__gte=now - timedelta(hours=24))
        elif pending_age_bucket == "gt_24h":
            qs = qs.filter(fetched_at__lt=now - timedelta(hours=24))

    date_from = _get(params, "date_from")
    if date_from:
        qs = qs.filter(posted_date__gte=date_from)

    date_to = _get(params, "date_to")
    if date_to:
        qs = qs.filter(posted_date__lte=date_to)

    scope_f = _get(params, "scope_status")
    if scope_f:
        qs = qs.filter(scope_status=scope_f)

    filter_decision_f = _get(params, "filter_decision").upper()
    if filter_decision_f == "FILTERED_OUT":
        qs = qs.filter(filtered_out_q())
    elif filter_decision_f:
        qs = qs.filter(filter_decision=filter_decision_f)

    country_code_f = _get(params, "country_code")
    if country_code_f:
        country_code = country_code_f.upper()
        qs = qs.filter(Q(country_code__iexact=country_code) | json_array_contains_q("country_codes", country_code))

    marketing_role_f = _get(params, "marketing_role")
    if marketing_role_f:
        qs = qs.filter(
            Q(job_domain__iexact=marketing_role_f)
            | json_array_contains_q("job_domain_candidates", marketing_role_f)
        )

    role_category_f = _get(params, "role_category")
    if role_category_f:
        qs = qs.filter(role_category__iexact=role_category_f)

    return qs


def rawjob_filter_state(params: Mapping[str, str]) -> dict[str, str]:
    """UI state helper: returns selected_* values for every known filter."""
    state: dict[str, str] = {}
    for key in FILTER_STATE_KEYS:
        state[f"selected_{key}"] = _get(params, key)
    state["q"] = _get(params, "q")
    return state

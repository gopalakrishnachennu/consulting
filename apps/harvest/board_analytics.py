"""
Board Analytics Service
=======================
Single source of truth for per-platform dashboard metrics.

Produces ONE unified payload per platform using a SINGLE denominator
(total RawJob rows) so "blocked > jobs" is impossible.

Usage:
    from harvest.board_analytics import get_board_analytics
    data = get_board_analytics(window_days=30)
"""

from __future__ import annotations

from datetime import timedelta
from django.core.cache import cache
from django.db import models
from django.db.models import Avg, Case, CharField, Count, F, FloatField, Max, Q, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from .board_capabilities import get_capabilities, capability_gap
from .role_filter import STRONG
from .services.rawjob_query import (
    duplicate_rawjob_q,
    effective_classification_q,
    json_array_contains_q,
    production_rawjobs_queryset,
    ready_stage_q,
)
from .enrichments import CURRENT_DOMAIN_VERSION
from .runtime_config import get_ready_stage_min_confidence


# Boards that harvest via Jarvis (manual paste) — excluded from ATS ranking.
JARVIS_SLUGS = {"jarvis"}

# Minimum runs required before we trust the risk score.
MIN_RUNS_FOR_RISK = 5
CURRENT_ENRICHMENT_VERSION = "v3"
BOARD_ANALYTICS_CACHE_TTL = 300


RAWJOB_FIELD_GROUP_SPECS = [
    {
        "key": "freshness",
        "title": "Freshness & Versioning",
        "description": "Distinguish old historical rows from true parser/extractor gaps.",
        "columns": [
            {"key": "recent_30d", "label": "Recent 30d", "tip": "Rows fetched in the last 30 days.", "warn": 20, "good": 50},
            {"key": "current_enrichment_version", "label": "Enrich v3", "tip": "Rows enriched with the current enrichment version.", "warn": 40, "good": 75},
            {"key": "current_domain_version", "label": "Domain d2", "tip": "Rows classified with the current domain taxonomy version.", "warn": 40, "good": 75},
        ],
    },
    {
        "key": "scoped_harvest",
        "title": "Scoped Harvest Routing",
        "description": "Shows which discovered jobs deserve expensive processing versus cold storage.",
        "columns": [
            {"key": "country_code", "label": "Country Resolved", "tip": "Rows with ISO country_code populated.", "warn": 70, "good": 95},
            {"key": "target_country", "label": "Target Country", "tip": "Rows whose country_code is currently selected in Engine Config.", "warn": 20, "good": 50},
            {"key": "priority_target", "label": "Priority", "tip": "Rows marked is_priority=True for full processing.", "warn": 20, "good": 50},
            {"key": "review_unknown_country", "label": "Unknown Review", "tip": "Rows with unresolved country that need review or provider fallback.", "warn": 10, "good": 0, "inverse": True},
            {"key": "cold_non_target_country", "label": "Cold Non-Target", "tip": "Rows stored only because they are outside target countries.", "warn": 70, "good": 85, "inverse": True},
            {"key": "cold_no_location", "label": "No Location", "tip": "Rows without enough location signal to resolve country.", "warn": 10, "good": 0, "inverse": True},
        ],
    },
    {
        "key": "pipeline_stages",
        "title": "RawJob Pipeline Stages",
        "description": "Where each board's raw jobs are getting through the harvest pipeline.",
        "columns": [
            {"key": "fetched", "label": "Fetched", "tip": "All raw jobs collected for this board.", "warn": 100, "good": 100},
            {"key": "parsed", "label": "Parsed", "tip": "Has JD / description text parsed.", "warn": 70, "good": 90},
            {"key": "enriched", "label": "Enriched", "tip": "Has enrichment scores / extracted metadata.", "warn": 60, "good": 85},
            {"key": "classified", "label": "Classified", "tip": "Has at least minimal classification confidence.", "warn": 55, "good": 80},
            {"key": "ready", "label": "Ready", "tip": "Active + usable JD + configured classification confidence.", "warn": 40, "good": 65},
            {"key": "synced", "label": "Synced", "tip": "Already promoted to the vet pool.", "warn": 5, "good": 20},
            {"key": "failed_sync", "label": "Failed", "tip": "Sync failed.", "warn": 5, "good": 0, "inverse": True},
            {"key": "duplicate", "label": "Duplicate", "tip": "Marked duplicate / skipped.", "warn": 5, "good": 0, "inverse": True},
        ],
    },
    {
        "key": "content",
        "title": "RawJob Content Coverage",
        "description": "How much usable job content each board is storing in RawJob.",
        "columns": [
            {"key": "jd", "label": "JD", "tip": "Has description text stored.", "capability_key": "jd", "warn": 70, "good": 90},
            {"key": "requirements", "label": "Req", "tip": "Has requirements / qualifications text.", "capability_key": "requirements", "warn": 30, "good": 60},
            {"key": "responsibilities", "label": "Resp", "tip": "Has responsibilities / duties text.", "capability_key": "responsibilities", "warn": 30, "good": 60},
            {"key": "benefits", "label": "Benefits", "tip": "Has benefits section text.", "warn": 15, "good": 40},
            {"key": "salary_any", "label": "Salary", "tip": "Has either structured salary or raw salary text.", "capability_key": "salary", "warn": 10, "good": 30},
            {"key": "salary_structured", "label": "Sal Struct", "tip": "Has structured salary_min/max.", "capability_key": "salary", "warn": 10, "good": 25},
            {"key": "salary_raw", "label": "Sal Raw", "tip": "Has raw salary text.", "capability_key": "salary", "warn": 10, "good": 25},
            {"key": "posted_date", "label": "Posted", "tip": "Has posted date.", "warn": 60, "good": 90},
            {"key": "closing_date", "label": "Closing", "tip": "Has closing date.", "warn": 10, "good": 30},
        ],
    },
    {
        "key": "classification",
        "title": "Classification & Enrichment",
        "description": "Board coverage for taxonomy, title normalization, skills, and role routing fields.",
        "columns": [
            {"key": "normalized_title", "label": "Norm Title", "tip": "Normalized title stored.", "warn": 70, "good": 90},
            {"key": "department", "label": "Dept", "tip": "Raw department stored.", "capability_key": "department", "warn": 30, "good": 60},
            {"key": "department_normalized", "label": "Dept Norm", "tip": "Normalized department stored.", "warn": 30, "good": 60},
            {"key": "job_category", "label": "Category", "tip": "Category assigned.", "warn": 70, "good": 95},
            {"key": "job_domain", "label": "Domain", "tip": "Marketing role domain assigned.", "warn": 70, "good": 95},
            {"key": "job_domain_candidates", "label": "Candidates", "tip": "Candidate domain slugs stored.", "warn": 60, "good": 90},
            {"key": "skills", "label": "Skills", "tip": "Any extracted skills.", "warn": 40, "good": 75},
            {"key": "tech_stack", "label": "Tech", "tip": "Any extracted tech stack.", "warn": 30, "good": 60},
            {"key": "job_keywords", "label": "Keywords", "tip": "Any extracted job/title keywords.", "warn": 40, "good": 75},
        ],
    },
    {
        "key": "location_work",
        "title": "Location, Experience & Work Conditions",
        "description": "Coverage for structured location, experience, education, and work-condition fields.",
        "columns": [
            {"key": "location_raw", "label": "Loc Raw", "tip": "Original location text stored.", "warn": 70, "good": 95},
            {"key": "location_candidates", "label": "Loc Candidates", "tip": "All parsed locations stored for multi-location jobs.", "warn": 20, "good": 50},
            {"key": "country_codes", "label": "Countries", "tip": "All resolved country codes from parsed locations.", "warn": 20, "good": 50},
            {"key": "city", "label": "City", "tip": "City extracted.", "capability_key": "geo", "warn": 30, "good": 60},
            {"key": "state", "label": "State", "tip": "State/region extracted.", "capability_key": "geo", "warn": 30, "good": 60},
            {"key": "country", "label": "Country", "tip": "Country extracted.", "capability_key": "geo", "warn": 40, "good": 70},
            {"key": "postal_code", "label": "Postal", "tip": "Postal code extracted.", "capability_key": "geo", "warn": 10, "good": 30},
            {"key": "employment_type", "label": "EmpType", "tip": "Employment type stored.", "capability_key": "employment_type", "warn": 30, "good": 60},
            {"key": "experience_level", "label": "ExpLvl", "tip": "Experience level stored.", "capability_key": "experience_level", "warn": 15, "good": 35},
            {"key": "years_required", "label": "Years", "tip": "Years of experience extracted.", "warn": 20, "good": 45},
            {"key": "education_required", "label": "Education", "tip": "Education requirement stored.", "capability_key": "education", "warn": 20, "good": 45},
            {"key": "schedule_type", "label": "Schedule", "tip": "Schedule type stored.", "warn": 20, "good": 45},
            {"key": "shift_schedule", "label": "Shift", "tip": "Shift schedule stored.", "warn": 10, "good": 30},
            {"key": "travel_required", "label": "Travel", "tip": "Travel requirement stored.", "warn": 10, "good": 25},
        ],
    },
    {
        "key": "legal_company_vendor",
        "title": "Legal, Company & Vendor Fields",
        "description": "Coverage for legal/work-auth signals, company context, and vendor-native fields.",
        "columns": [
            {"key": "visa_sponsorship", "label": "Visa", "tip": "Visa sponsorship explicitly known (true/false).", "warn": 5, "good": 20},
            {"key": "work_authorization", "label": "WorkAuth", "tip": "Work authorization text stored.", "warn": 5, "good": 20},
            {"key": "clearance_signal", "label": "Clearance", "tip": "Security clearance signal stored.", "warn": 5, "good": 20},
            {"key": "languages_required", "label": "Lang", "tip": "Languages required list stored.", "warn": 5, "good": 20},
            {"key": "certifications", "label": "Certs", "tip": "Certifications list stored.", "warn": 10, "good": 25},
            {"key": "licenses_required", "label": "Licenses", "tip": "Licenses required list stored.", "warn": 10, "good": 25},
            {"key": "company_industry", "label": "Industry", "tip": "Company industry stored on RawJob.", "warn": 20, "good": 50},
            {"key": "company_size", "label": "Company Size", "tip": "Company size stored on RawJob.", "warn": 15, "good": 40},
            {"key": "company_stage", "label": "Stage", "tip": "Company stage stored on RawJob.", "warn": 10, "good": 30},
            {"key": "vendor_job_identification", "label": "Vendor ID", "tip": "Vendor-native job identification stored.", "warn": 20, "good": 50},
            {"key": "vendor_job_category", "label": "Vendor Cat", "tip": "Vendor-native category stored.", "warn": 20, "good": 50},
            {"key": "vendor_degree_level", "label": "Vendor Edu", "tip": "Vendor-native education/degree field stored.", "warn": 10, "good": 30},
            {"key": "vendor_job_schedule", "label": "Vendor Sched", "tip": "Vendor-native schedule stored.", "warn": 10, "good": 30},
            {"key": "vendor_job_shift", "label": "Vendor Shift", "tip": "Vendor-native shift stored.", "warn": 10, "good": 30},
            {"key": "vendor_location_block", "label": "Vendor Loc", "tip": "Vendor-native location block stored.", "warn": 20, "good": 50},
        ],
    },
]


RAWJOB_SCORE_SPECS = [
    {"key": "quality_score", "label": "Quality", "tip": "Average RawJob quality_score for rows where it exists."},
    {"key": "jd_quality_score", "label": "JD Quality", "tip": "Average JD quality score for rows where it exists."},
    {"key": "classification_confidence", "label": "Class Conf", "tip": "Average classification confidence where stored."},
    {"key": "category_confidence", "label": "Cat Conf", "tip": "Average category confidence where stored."},
    {"key": "resume_ready_score", "label": "Resume Ready", "tip": "Average resume-ready score where stored."},
]


def _pct(numerator, denominator, decimals=1):
    if not denominator:
        return None
    return round(numerator / denominator * 100, decimals)


def _coverage_metric(count, total):
    return {
        "count": int(count or 0),
        "pct": _pct(int(count or 0), total),
    }


def _score_metric(avg_value, known_count, total):
    avg_pct = None
    if avg_value is not None:
        avg_pct = round(float(avg_value) * 100, 1)
    return {
        "count": int(known_count or 0),
        "known_pct": _pct(int(known_count or 0), total),
        "avg_pct": avg_pct,
    }


def _tone_from_pct(pct, warn, good, inverse: bool = False):
    if pct is None:
        return "muted"
    if inverse:
        if pct <= good:
            return "good"
        if pct <= warn:
            return "warn"
        return "bad"
    if pct >= good:
        return "good"
    if pct >= warn:
        return "warn"
    return "bad"


def _tone_from_score(score_pct):
    if score_pct is None:
        return "muted"
    if score_pct >= 80:
        return "good"
    if score_pct >= 60:
        return "warn"
    return "bad"


def _rawjob_platform_slug_expr():
    fallback = Coalesce("job_platform__slug", "platform_label__platform__slug", Value("unknown"))
    return Case(
        When(Q(platform_slug__isnull=True) | Q(platform_slug=""), then=fallback),
        default=F("platform_slug"),
        output_field=CharField(),
    )


def _target_country_q(country_codes: list[str]) -> Q:
    q = Q()
    for code in country_codes:
        clean = (code or "").strip().upper()
        if clean:
            q |= Q(country_code__iexact=clean) | json_array_contains_q("country_codes", clean)
    return q or Q(pk__in=[])


def _build_board_analytics(window_days: int = 30) -> dict:
    """
    Returns a dict with:
      - platforms: list of per-platform metric rows (ATS boards only, no Jarvis)
      - jarvis: separate summary for Jarvis/manual ingest
      - unsupported: list of slugs marked as UNSUPPORTED
      - generated_at: ISO timestamp
      - window_days: the run-history window used
    """
    from harvest.models import CompanyFetchRun, HarvestEngineConfig, JobBoardPlatform, RawJob

    now = timezone.now()
    run_window = now - timedelta(days=window_days)
    fresh_30d_cutoff = now - timedelta(days=30)
    target_countries = HarvestEngineConfig.get().get_target_countries()
    target_country_q = _target_country_q(target_countries)
    raw_platform_slug = _rawjob_platform_slug_expr()
    run_platform_slug = Coalesce("label__platform__slug", Value("unknown"))

    # ── 1. Per-platform RawJob metrics (job-level, all-time) ──────────────────
    # Detect which optional fields exist in this DB (handles schema drift gracefully).
    _raw_fields = {f.name for f in RawJob._meta.get_fields()}

    ready_min_conf = get_ready_stage_min_confidence()
    _field_annotations = {
        "total":       Count("id"),
        "synced":      Count("id", filter=Q(sync_status="SYNCED")),
        "pending":     Count("id", filter=Q(sync_status="PENDING")),
        "failed_sync": Count("id", filter=Q(sync_status="FAILED")),
        "skipped":     Count("id", filter=Q(sync_status="SKIPPED")),
        "duplicate_count": Count("id", filter=duplicate_rawjob_q()),
        "inactive":    Count("id", filter=Q(is_active=False)),
        "missing_jd":  Count("id", filter=Q(has_description=False)),
        "jd_count":    Count("id", filter=Q(has_description=True)),
        "parsed_count": Count("id", filter=Q(has_description=True)),
        "enriched_count": Count("id", filter=Q(quality_score__isnull=False) | Q(jd_quality_score__isnull=False)),
        "classified_count": Count("id", filter=effective_classification_q(min_conf=0.01)),
        "ready_count": Count("id", filter=ready_stage_q(min_conf=ready_min_conf)),
        "recent_30d_count": Count("id", filter=Q(fetched_at__gte=fresh_30d_cutoff)),
        "current_enrichment_version_count": Count("id", filter=Q(enrichment_version=CURRENT_ENRICHMENT_VERSION)),
        "current_domain_version_count": Count("id", filter=Q(domain_version=CURRENT_DOMAIN_VERSION)),
        "country_code_count": Count("id", filter=~Q(country_code="")),
        "target_country_count": Count("id", filter=target_country_q),
        "priority_target_count": Count("id", filter=Q(is_priority=True)),
        "review_unknown_country_count": Count("id", filter=Q(scope_status="REVIEW_UNKNOWN_COUNTRY")),
        "cold_non_target_country_count": Count("id", filter=Q(scope_status="COLD_NON_TARGET_COUNTRY")),
        "cold_no_location_count": Count("id", filter=Q(scope_status="COLD_NO_LOCATION")),
        "has_salary":  Count("id", filter=Q(salary_min__isnull=False) | Q(salary_max__isnull=False)),
        "salary_any_count": Count("id", filter=Q(salary_min__isnull=False) | Q(salary_max__isnull=False) | ~Q(salary_raw="")),
        "salary_structured_count": Count("id", filter=Q(salary_min__isnull=False) | Q(salary_max__isnull=False)),
        "salary_raw_count": Count("id", filter=~Q(salary_raw="")),
        "benefits_count": Count("id", filter=~Q(benefits="")),
        "location_raw_count": Count("id", filter=~Q(location_raw="")),
        "location_candidates_count": Count("id", filter=~Q(location_candidates=[])),
        "country_codes_count": Count("id", filter=~Q(country_codes=[])),
        "city_count": Count("id", filter=~Q(city="")),
        "state_count": Count("id", filter=~Q(state="")),
        "country_count": Count("id", filter=~Q(country="")),
        "postal_code_count": Count("id", filter=~Q(postal_code="")),
        "posted_date_count": Count("id", filter=Q(posted_date__isnull=False)),
        "closing_date_count": Count("id", filter=Q(closing_date__isnull=False)),
        "normalized_title_count": Count("id", filter=~Q(normalized_title="")),
        "department_normalized_count": Count("id", filter=~Q(department_normalized="")),
        "job_category_count": Count("id", filter=~Q(job_category="")),
        "job_domain_count": Count("id", filter=~Q(job_domain="")),
        "job_domain_candidates_count": Count("id", filter=~Q(job_domain_candidates=[])),
        "skills_count": Count("id", filter=~Q(skills=[])),
        "tech_stack_count": Count("id", filter=~Q(tech_stack=[])),
        "job_keywords_count": Count("id", filter=~Q(job_keywords=[]) | ~Q(title_keywords=[])),
        "years_required_count": Count("id", filter=Q(years_required__isnull=False)),
        "employment_type_count": Count("id", filter=~Q(employment_type="UNKNOWN")),
        "experience_level_count": Count("id", filter=~Q(experience_level="UNKNOWN")),
        "travel_required_count": Count("id", filter=~Q(travel_required="")),
        "schedule_type_count": Count("id", filter=~Q(schedule_type="")),
        "shift_schedule_count": Count("id", filter=~Q(shift_schedule="")),
        "visa_sponsorship_count": Count("id", filter=Q(visa_sponsorship__isnull=False)),
        "work_authorization_count": Count("id", filter=~Q(work_authorization="")),
        "clearance_signal_count": Count("id", filter=Q(clearance_required=True) | ~Q(clearance_level="")),
        "languages_required_count": Count("id", filter=~Q(languages_required=[])),
        "certifications_count": Count("id", filter=~Q(certifications=[])),
        "licenses_required_count": Count("id", filter=~Q(licenses_required=[])),
        "company_industry_count": Count("id", filter=~Q(company_industry="")),
        "company_size_count": Count("id", filter=~Q(company_size="")),
        "company_stage_count": Count("id", filter=~Q(company_stage="")),
        "vendor_job_identification_count": Count("id", filter=~Q(vendor_job_identification="")),
        "vendor_job_category_count": Count("id", filter=~Q(vendor_job_category="")),
        "vendor_degree_level_count": Count("id", filter=~Q(vendor_degree_level="")),
        "vendor_job_schedule_count": Count("id", filter=~Q(vendor_job_schedule="")),
        "vendor_job_shift_count": Count("id", filter=~Q(vendor_job_shift="")),
        "vendor_location_block_count": Count("id", filter=~Q(vendor_location_block="")),
        "quality_score_known_count": Count("id", filter=Q(quality_score__isnull=False)),
        "jd_quality_score_known_count": Count("id", filter=Q(jd_quality_score__isnull=False)),
        "classification_confidence_known_count": Count("id", filter=Q(classification_confidence__isnull=False)),
        "category_confidence_known_count": Count("id", filter=Q(category_confidence__isnull=False)),
        "resume_ready_score_known_count": Count("id", filter=Q(resume_ready_score__isnull=False)),
        "avg_quality_score": Avg("quality_score"),
        "avg_jd_quality_score": Avg("jd_quality_score"),
        "avg_classification_confidence": Avg("classification_confidence"),
        "avg_category_confidence": Avg("category_confidence"),
        "avg_resume_ready_score": Avg("resume_ready_score"),
    }
    # Blocker reason counts (only if sync_skip_reason field exists)
    if "sync_skip_reason" in _raw_fields:
        _field_annotations["blocked_inactive"]    = Count("id", filter=Q(sync_skip_reason="INACTIVE_POSTING"))
        _field_annotations["blocked_jd_weak"]     = Count("id", filter=Q(sync_skip_reason="JD_TOO_WEAK"))
        _field_annotations["blocked_mismatch"]    = Count("id", filter=Q(sync_skip_reason="PLATFORM_MISMATCH"))
        _field_annotations["blocked_duplicate"]   = Count("id", filter=Q(sync_skip_reason__in=["DUPLICATE_RISK", "DUPLICATE_EXISTING"]))
        _field_annotations["blocked_no_company"]  = Count("id", filter=Q(sync_skip_reason="COMPANY_UNRESOLVED"))
    _field_annotations["blocked_low_conf"] = Count(
        "id",
        filter=(
            Q(sync_status="PENDING", has_description=True, is_active=True)
            & effective_classification_q(min_conf=0.01)
            & ~effective_classification_q(min_conf=ready_min_conf)
            & ~Q(filter_decision=STRONG)
        ),
    )
    if "requirements" in _raw_fields:
        _field_annotations["has_requirements"] = Count("id", filter=~Q(requirements=""))
    if "responsibilities" in _raw_fields:
        _field_annotations["has_responsibilities"] = Count("id", filter=~Q(responsibilities=""))
    if "department" in _raw_fields:
        _field_annotations["has_department"] = Count("id", filter=~Q(department=""))
    if "city" in _raw_fields:
        _field_annotations["has_geo"] = Count("id", filter=~Q(city="") | ~Q(country=""))
    if "education_required" in _raw_fields:
        _field_annotations["has_education"] = Count("id", filter=~Q(education_required=""))
    if "employment_type" in _raw_fields:
        _field_annotations["has_schedule"] = Count("id", filter=~Q(employment_type="UNKNOWN"))

    job_qs = (
        production_rawjobs_queryset()
        .annotate(metric_slug=raw_platform_slug)
        .exclude(metric_slug__in=JARVIS_SLUGS)
        .values("metric_slug")
        .annotate(**_field_annotations)
    )
    job_by_slug = {row["metric_slug"]: row for row in job_qs}

    # ── 2. Per-platform Run metrics (run-level, within window) ────────────────
    run_qs = (
        CompanyFetchRun.objects
        .filter(started_at__gte=run_window)
        .annotate(metric_slug=run_platform_slug)
        .exclude(metric_slug__in=JARVIS_SLUGS)
        .values("metric_slug")
        .annotate(
            runs=Count("id"),
            success_runs=Count("id", filter=Q(status=CompanyFetchRun.Status.SUCCESS)),
            empty_runs=Count("id", filter=Q(status=CompanyFetchRun.Status.EMPTY)),
            partial_runs=Count("id", filter=Q(status=CompanyFetchRun.Status.PARTIAL)),
            failed_runs=Count("id", filter=Q(status=CompanyFetchRun.Status.FAILED)),
            parse_error_runs=Count("id", filter=Q(error_type=CompanyFetchRun.ErrorType.PARSE_ERROR)),
            timeout_runs=Count("id", filter=Q(error_type=CompanyFetchRun.ErrorType.TIMEOUT)),
            rate_limited_runs=Count("id", filter=Q(error_type=CompanyFetchRun.ErrorType.RATE_LIMITED)),
            avg_jobs_found=Avg("jobs_found"),
            avg_duration_secs=Avg(
                Case(
                    When(
                        completed_at__isnull=False,
                        then=models.ExpressionWrapper(
                            F("completed_at") - F("started_at"),
                            output_field=models.DurationField(),
                        ),
                    ),
                    output_field=models.DurationField(),
                )
            ),
            last_run=Max("started_at"),
        )
    )
    run_by_slug = {row["metric_slug"]: row for row in run_qs}

    # ── 3. Platform registry (support_tier, is_enabled) ──────────────────────
    platform_meta = {
        p.slug: {"support_tier": p.support_tier, "name": p.name, "is_enabled": p.is_enabled}
        for p in JobBoardPlatform.objects.all()
    }

    # ── 4. Merge into per-platform rows ──────────────────────────────────────
    all_slugs = set(job_by_slug) | set(run_by_slug)
    ats_rows = []
    unsupported_rows = []
    jarvis_rows = []

    for slug in sorted(all_slugs):
        j = job_by_slug.get(slug, {})
        r = run_by_slug.get(slug, {})
        meta = platform_meta.get(slug, {})
        tier = meta.get("support_tier", "healthy")

        total = j.get("total", 0)
        runs = r.get("runs", 0)
        success_runs = r.get("success_runs", 0)
        empty_runs = r.get("empty_runs", 0)
        partial_runs = r.get("partial_runs", 0)
        failed_runs = r.get("failed_runs", 0)
        parse_error_runs = r.get("parse_error_runs", 0)

        # "blocked" = PENDING RawJobs (same denominator as total)
        pending = j.get("pending", 0)
        inactive = j.get("inactive", 0)
        missing_jd = j.get("missing_jd", 0)

        # Risk score: weighted combination of failure signals (0–100)
        # Requires MIN_RUNS_FOR_RISK runs to be trustworthy.
        risk_score = None
        risk_trusted = False
        if runs >= MIN_RUNS_FOR_RISK:
            empty_rate = empty_runs / runs if runs else 0
            fail_rate  = failed_runs / runs if runs else 0
            parse_rate = parse_error_runs / runs if runs else 0
            block_rate = pending / total if total else 0
            risk_score = round(
                (empty_rate * 25) +
                (fail_rate  * 35) +
                (parse_rate * 25) +
                (block_rate * 15),
                1,
            )
            risk_trusted = True

        avg_duration = r.get("avg_duration_secs")
        avg_duration_secs = (
            avg_duration.total_seconds() if hasattr(avg_duration, "total_seconds") else None
        )

        coverage = {
            "jd":               _pct(j.get("jd_count", 0), total),
            "requirements":     _pct(j.get("has_requirements", 0), total),
            "responsibilities": _pct(j.get("has_responsibilities", 0), total),
            "salary":           _pct(j.get("has_salary", 0), total),
            "department":       _pct(j.get("has_department", 0), total),
            "geo":              _pct(j.get("has_geo", 0), total),
            "education":        _pct(j.get("has_education", 0), total),
            "schedule":         _pct(j.get("has_schedule", 0), total),
        }

        caps = get_capabilities(slug)
        gaps = capability_gap(slug, coverage)

        # Build human-readable issue reasons for the dashboard badge
        issue_reasons: list[str] = []
        if runs >= MIN_RUNS_FOR_RISK:
            if risk_score and risk_score >= 35:
                if empty_runs / runs >= 0.4:
                    issue_reasons.append("high zero-yield")
                if failed_runs / runs >= 0.2:
                    issue_reasons.append("high fail rate")
                if parse_error_runs / runs >= 0.2:
                    issue_reasons.append("parse errors")
                if total and pending / total >= 0.3:
                    issue_reasons.append("blocked sync")
        if total and missing_jd / total >= 0.2:
            issue_reasons.append("missing JD")

        row = {
            "slug": slug,
            "name": meta.get("name", slug),
            "support_tier": tier,
            "is_enabled": meta.get("is_enabled", True),
            # job-level (all-time)
            "total_jobs": total,
            "synced": j.get("synced", 0),
            "pending": pending,
            "failed_sync": j.get("failed_sync", 0),
            "skipped": j.get("skipped", 0),
            "inactive": inactive,
            "missing_jd": missing_jd,
            "inactive_pct":  _pct(inactive, total),
            "missing_jd_pct": _pct(missing_jd, total),
            "pending_pct":   _pct(pending, total),
            # field coverage
            "coverage": coverage,
            # capability matrix + gap analysis
            "capabilities": {k: v for k, v in caps.items() if isinstance(v, bool)},
            "capability_gaps": gaps,
            "source_reliability": caps.get("source_reliability", "unknown"),
            # run-level (window_days)
            "runs": runs,
            "success_runs": success_runs,
            "empty_runs": empty_runs,
            "partial_runs": partial_runs,
            "failed_runs": failed_runs,
            "parse_error_runs": parse_error_runs,
            "timeout_runs": r.get("timeout_runs", 0),
            "rate_limited_runs": r.get("rate_limited_runs", 0),
            "zero_yield_pct": _pct(empty_runs, runs),
            "fail_rate_pct":  _pct(failed_runs, runs),
            "success_rate_pct": _pct(success_runs + partial_runs * 0.5, runs),
            "avg_jobs_per_run": round(float(r.get("avg_jobs_found") or 0), 1),
            "avg_duration_secs": round(avg_duration_secs, 1) if avg_duration_secs else None,
            "last_run": r.get("last_run").isoformat() if r.get("last_run") else None,
            "risk_score": risk_score,
            "risk_trusted": risk_trusted,   # False when runs < MIN_RUNS_FOR_RISK
            "issue_reasons": issue_reasons, # list of human-readable issue tags
            # Blocker breakdown — why jobs are failing sync (zero = not set / pre-migration)
            "blockers": {
                "inactive":   j.get("blocked_inactive", 0),
                "jd_weak":    j.get("blocked_jd_weak", 0),
                "mismatch":   j.get("blocked_mismatch", 0),
                "duplicate":  j.get("blocked_duplicate", 0),
                "no_company": j.get("blocked_no_company", 0),
                "low_conf":   j.get("blocked_low_conf", 0),
            },
        }

        row["rawjob_metrics"] = {
            "recent_30d": _coverage_metric(j.get("recent_30d_count", 0), total),
            "current_enrichment_version": _coverage_metric(j.get("current_enrichment_version_count", 0), total),
            "current_domain_version": _coverage_metric(j.get("current_domain_version_count", 0), total),
            "country_code": _coverage_metric(j.get("country_code_count", 0), total),
            "target_country": _coverage_metric(j.get("target_country_count", 0), total),
            "priority_target": _coverage_metric(j.get("priority_target_count", 0), total),
            "review_unknown_country": _coverage_metric(j.get("review_unknown_country_count", 0), total),
            "cold_non_target_country": _coverage_metric(j.get("cold_non_target_country_count", 0), total),
            "cold_no_location": _coverage_metric(j.get("cold_no_location_count", 0), total),
            "fetched": _coverage_metric(total, total),
            "parsed": _coverage_metric(j.get("parsed_count", 0), total),
            "enriched": _coverage_metric(j.get("enriched_count", 0), total),
            "classified": _coverage_metric(j.get("classified_count", 0), total),
            "ready": _coverage_metric(j.get("ready_count", 0), total),
            "synced": _coverage_metric(j.get("synced", 0), total),
            "failed_sync": _coverage_metric(j.get("failed_sync", 0), total),
            "duplicate": _coverage_metric(j.get("duplicate_count", 0), total),
            "jd": _coverage_metric(j.get("jd_count", 0), total),
            "requirements": _coverage_metric(j.get("has_requirements", 0), total),
            "responsibilities": _coverage_metric(j.get("has_responsibilities", 0), total),
            "benefits": _coverage_metric(j.get("benefits_count", 0), total),
            "salary_any": _coverage_metric(j.get("salary_any_count", 0), total),
            "salary_structured": _coverage_metric(j.get("salary_structured_count", 0), total),
            "salary_raw": _coverage_metric(j.get("salary_raw_count", 0), total),
            "posted_date": _coverage_metric(j.get("posted_date_count", 0), total),
            "closing_date": _coverage_metric(j.get("closing_date_count", 0), total),
            "normalized_title": _coverage_metric(j.get("normalized_title_count", 0), total),
            "department": _coverage_metric(j.get("has_department", 0), total),
            "department_normalized": _coverage_metric(j.get("department_normalized_count", 0), total),
            "job_category": _coverage_metric(j.get("job_category_count", 0), total),
            "job_domain": _coverage_metric(j.get("job_domain_count", 0), total),
            "job_domain_candidates": _coverage_metric(j.get("job_domain_candidates_count", 0), total),
            "skills": _coverage_metric(j.get("skills_count", 0), total),
            "tech_stack": _coverage_metric(j.get("tech_stack_count", 0), total),
            "job_keywords": _coverage_metric(j.get("job_keywords_count", 0), total),
            "location_raw": _coverage_metric(j.get("location_raw_count", 0), total),
            "location_candidates": _coverage_metric(j.get("location_candidates_count", 0), total),
            "country_codes": _coverage_metric(j.get("country_codes_count", 0), total),
            "city": _coverage_metric(j.get("city_count", 0), total),
            "state": _coverage_metric(j.get("state_count", 0), total),
            "country": _coverage_metric(j.get("country_count", 0), total),
            "postal_code": _coverage_metric(j.get("postal_code_count", 0), total),
            "employment_type": _coverage_metric(j.get("employment_type_count", 0), total),
            "experience_level": _coverage_metric(j.get("experience_level_count", 0), total),
            "years_required": _coverage_metric(j.get("years_required_count", 0), total),
            "education_required": _coverage_metric(j.get("has_education", 0), total),
            "schedule_type": _coverage_metric(j.get("schedule_type_count", 0), total),
            "shift_schedule": _coverage_metric(j.get("shift_schedule_count", 0), total),
            "travel_required": _coverage_metric(j.get("travel_required_count", 0), total),
            "visa_sponsorship": _coverage_metric(j.get("visa_sponsorship_count", 0), total),
            "work_authorization": _coverage_metric(j.get("work_authorization_count", 0), total),
            "clearance_signal": _coverage_metric(j.get("clearance_signal_count", 0), total),
            "languages_required": _coverage_metric(j.get("languages_required_count", 0), total),
            "certifications": _coverage_metric(j.get("certifications_count", 0), total),
            "licenses_required": _coverage_metric(j.get("licenses_required_count", 0), total),
            "company_industry": _coverage_metric(j.get("company_industry_count", 0), total),
            "company_size": _coverage_metric(j.get("company_size_count", 0), total),
            "company_stage": _coverage_metric(j.get("company_stage_count", 0), total),
            "vendor_job_identification": _coverage_metric(j.get("vendor_job_identification_count", 0), total),
            "vendor_job_category": _coverage_metric(j.get("vendor_job_category_count", 0), total),
            "vendor_degree_level": _coverage_metric(j.get("vendor_degree_level_count", 0), total),
            "vendor_job_schedule": _coverage_metric(j.get("vendor_job_schedule_count", 0), total),
            "vendor_job_shift": _coverage_metric(j.get("vendor_job_shift_count", 0), total),
            "vendor_location_block": _coverage_metric(j.get("vendor_location_block_count", 0), total),
        }
        row["score_metrics"] = {
            "quality_score": _score_metric(j.get("avg_quality_score"), j.get("quality_score_known_count", 0), total),
            "jd_quality_score": _score_metric(j.get("avg_jd_quality_score"), j.get("jd_quality_score_known_count", 0), total),
            "classification_confidence": _score_metric(j.get("avg_classification_confidence"), j.get("classification_confidence_known_count", 0), total),
            "category_confidence": _score_metric(j.get("avg_category_confidence"), j.get("category_confidence_known_count", 0), total),
            "resume_ready_score": _score_metric(j.get("avg_resume_ready_score"), j.get("resume_ready_score_known_count", 0), total),
        }

        if tier == JobBoardPlatform.SupportTier.UNSUPPORTED:
            unsupported_rows.append(row)
        else:
            ats_rows.append(row)

    # Sort: highest risk first (None = no runs → put last)
    ats_rows.sort(key=lambda x: (x["risk_score"] is None, -(x["risk_score"] or 0), -x["total_jobs"]))

    # ── 5. Jarvis summary ────────────────────────────────────────────────────
    jarvis_qs = production_rawjobs_queryset().filter(platform_slug="jarvis")
    jarvis_total = jarvis_qs.count()
    jarvis_summary = {
        "total_jobs": jarvis_total,
        "synced": jarvis_qs.filter(sync_status="SYNCED").count(),
        "pending": jarvis_qs.filter(sync_status="PENDING").count(),
        "missing_jd": jarvis_qs.filter(has_description=False).count(),
        "inactive": jarvis_qs.filter(is_active=False).count(),
        "note": "Manual/Jarvis ingest — not ranked alongside ATS boards.",
    }

    rawjob_field_groups = []
    for group in RAWJOB_FIELD_GROUP_SPECS:
        group_rows = []
        for p in ats_rows:
            cells = []
            for col in group["columns"]:
                metric = p["rawjob_metrics"][col["key"]]
                gap = None
                capability_key = col.get("capability_key")
                if capability_key:
                    gap = p["capability_gaps"].get(capability_key)
                cells.append({
                    "key": col["key"],
                    "count": metric["count"],
                    "pct": metric["pct"],
                    "gap": gap,
                    "tone": _tone_from_pct(metric["pct"], col["warn"], col["good"], inverse=bool(col.get("inverse"))),
                })
            group_rows.append({
                "slug": p["slug"],
                "name": p["name"],
                "support_tier": p["support_tier"],
                "source_reliability": p["source_reliability"],
                "total_jobs": p["total_jobs"],
                "cells": cells,
            })
        rawjob_field_groups.append({
            "key": group["key"],
            "title": group["title"],
            "description": group["description"],
            "columns": group["columns"],
            "rows": group_rows,
        })

    score_group = {
        "title": "RawJob Quality & Confidence",
        "description": "Average score values plus how many rows actually have those scores populated.",
        "columns": RAWJOB_SCORE_SPECS,
        "rows": [],
    }
    for p in ats_rows:
        cells = []
        for spec in RAWJOB_SCORE_SPECS:
            metric = p["score_metrics"][spec["key"]]
            cells.append({
                "key": spec["key"],
                "count": metric["count"],
                "known_pct": metric["known_pct"],
                "avg_pct": metric["avg_pct"],
                "tone": _tone_from_score(metric["avg_pct"]),
            })
        score_group["rows"].append({
            "slug": p["slug"],
            "name": p["name"],
            "support_tier": p["support_tier"],
            "source_reliability": p["source_reliability"],
            "total_jobs": p["total_jobs"],
            "cells": cells,
        })

    blocker_group = {
        "title": "RawJob Sync Blocker Matrix",
        "description": "Why RawJobs are getting stuck before sync. High percentages here are the clearest fix targets.",
        "columns": [
            {"key": "inactive", "label": "Inactive", "tip": "Blocked because posting is inactive."},
            {"key": "jd_weak", "label": "JD Weak", "tip": "Blocked because JD is too weak / unusable."},
            {"key": "mismatch", "label": "Mismatch", "tip": "Blocked by platform mismatch or gate mismatch."},
            {"key": "duplicate", "label": "Duplicate", "tip": "Blocked because of duplicate risk / existing duplicate."},
            {"key": "no_company", "label": "No Company", "tip": "Blocked because company could not be resolved."},
            {"key": "low_conf", "label": "Low Conf", "tip": "Blocked because classification is present but below ready threshold."},
        ],
        "rows": [],
    }
    for p in ats_rows:
        cells = []
        for spec in blocker_group["columns"]:
            count = int(p["blockers"].get(spec["key"], 0))
            pct = _pct(count, p["total_jobs"])
            cells.append({
                "key": spec["key"],
                "count": count,
                "pct": pct,
                "tone": _tone_from_pct(pct, 5, 15, inverse=True),
            })
        blocker_group["rows"].append({
            "slug": p["slug"],
            "name": p["name"],
            "support_tier": p["support_tier"],
            "source_reliability": p["source_reliability"],
            "total_jobs": p["total_jobs"],
            "cells": cells,
        })

    return {
        "platforms": ats_rows,
        "rawjob_field_groups": rawjob_field_groups,
        "rawjob_score_group": score_group,
        "rawjob_blocker_group": blocker_group,
        "unsupported": unsupported_rows,
        "jarvis": jarvis_summary,
        "generated_at": now.isoformat(),
        "window_days": window_days,
        "versions": {
            "domain_version": CURRENT_DOMAIN_VERSION,
            "enrichment_version": CURRENT_ENRICHMENT_VERSION,
            "cache_ttl_seconds": BOARD_ANALYTICS_CACHE_TTL,
        },
        "totals": {
            "ats_boards": len(ats_rows),
            "total_rawjobs": sum(r["total_jobs"] for r in ats_rows),
            "total_synced":  sum(r["synced"]     for r in ats_rows),
            "total_pending": sum(r["pending"]     for r in ats_rows),
        },
        "scope_summary": _build_scope_summary(target_countries),
    }


def _build_scope_summary(target_countries: list[str]) -> dict:
    """Top-level Scoped Harvest Routing card. Single aggregate query, fast.

    Returns counts + percentages for the 4 scope statuses, plus the top 8
    countries ranked by row count so the user sees both the routing split
    and where their unresolved/non-target volume is concentrated.
    """
    from .models import RawJob

    base = production_rawjobs_queryset()
    total = base.count()
    if total == 0:
        return {
            "total": 0,
            "rows": [],
            "top_countries": [],
            "target_countries": list(target_countries or []),
        }

    by_status = dict(
        base.values("scope_status")
        .annotate(c=Count("id"))
        .values_list("scope_status", "c")
    )

    def _row(label: str, key: str, count: int, tone: str) -> dict:
        return {
            "label": label,
            "key": key,
            "count": count,
            "pct": round(100.0 * count / total, 1) if total else 0.0,
            "tone": tone,
        }

    rows = [
        _row("Priority Target",   "PRIORITY_TARGET",
             by_status.get("PRIORITY_TARGET", 0), "good"),
        _row("Review (Unknown)",  "REVIEW_UNKNOWN_COUNTRY",
             by_status.get("REVIEW_UNKNOWN_COUNTRY", 0), "warn"),
        _row("Cold Non-Target",   "COLD_NON_TARGET_COUNTRY",
             by_status.get("COLD_NON_TARGET_COUNTRY", 0), "muted"),
        _row("No Location",       "COLD_NO_LOCATION",
             by_status.get("COLD_NO_LOCATION", 0), "muted"),
    ]

    top_countries = list(
        base.exclude(country_code="")
        .values("country_code")
        .annotate(c=Count("id"))
        .order_by("-c")[:8]
    )
    for tc in top_countries:
        tc["count"] = tc.pop("c")
        tc["pct"] = round(100.0 * tc["count"] / total, 1) if total else 0.0
        tc["is_target"] = tc["country_code"] in (target_countries or [])

    return {
        "total": total,
        "rows": rows,
        "top_countries": top_countries,
        "target_countries": list(target_countries or []),
    }


def get_board_analytics(window_days: int = 30, *, force_refresh: bool = False) -> dict:
    cache_key = f"harvest:board-analytics:v4:{window_days}"
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    data = _build_board_analytics(window_days=window_days)
    cache.set(cache_key, data, BOARD_ANALYTICS_CACHE_TTL)
    return data

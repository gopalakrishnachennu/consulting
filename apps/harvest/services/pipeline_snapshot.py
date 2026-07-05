from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, Max, Min, Q
from django.utils import timezone

from harvest.models import CompanyFetchRun, RawJob
from harvest.runtime_config import get_jd_backfill_lock_stale_minutes, get_ready_stage_min_confidence
from harvest.role_filter import STRONG
from harvest.services.rawjob_query import build_funnel_counts, filtered_out_q, production_rawjobs_queryset


def raw_jobs_missing_description_count() -> int:
    """Count jobs with empty/trivial description that still have a URL."""
    return RawJob.objects.missing_jd(
        stale_minutes=get_jd_backfill_lock_stale_minutes()
    ).filter(is_test_run=False).count()


def raw_jobs_missing_jd_expired_count() -> int:
    """Missing-JD rows that are effectively expired/inactive."""
    today = timezone.now().date()
    now = timezone.now()
    stale_days = max(30, int(getattr(settings, "HARVEST_JD_STALE_DAYS", 120)))
    stale_cutoff = today - timedelta(days=stale_days)

    return (
        RawJob.objects.missing_jd(stale_minutes=get_jd_backfill_lock_stale_minutes())
        .filter(is_test_run=False)
        .filter(
            Q(expires_at__lt=now)
            | Q(closing_date__lt=today)
            | Q(is_active=False)
            | Q(raw_payload__active=False)
            | Q(posted_date__lt=stale_cutoff)
        )
        .count()
    )


def load_rawjobs_dashboard_stats(*, force_refresh: bool = False) -> dict:
    """Unified KPI payload for Raw Jobs cards and polling endpoints."""
    stats_key = "rawjobs_dashboard_stats"
    expired_key = "rawjobs_expired_missing_jd"
    stats_ttl_sec = 20
    expired_ttl_sec = 120

    stats = None if force_refresh else cache.get(stats_key)
    if stats is not None:
        return stats

    last_24h_cutoff = timezone.now() - timedelta(hours=24)
    base = production_rawjobs_queryset()
    agg = base.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        remote=Count("id", filter=Q(is_remote=True)),
        synced=Count("id", filter=Q(sync_status="SYNCED")),
        pending=Count("id", filter=Q(sync_status="PENDING")),
        failed=Count("id", filter=Q(sync_status="FAILED")),
        new_today=Count("id", filter=Q(fetched_at__gte=last_24h_cutoff)),
        missing_jd=Count(
            "id",
            filter=Q(has_description=False, is_cold=False, jd_fetch_skipped=False)
            & ~Q(filter_decision__in=["COLD", "NO_MATCH"]),
        ),
    )
    agg["test_rows"] = RawJob.objects.filter(is_test_run=True).count()

    expired_missing = None if force_refresh else cache.get(expired_key)
    if expired_missing is None:
        expired_missing = raw_jobs_missing_jd_expired_count()
        cache.set(expired_key, expired_missing, expired_ttl_sec)

    agg["expired_missing"] = expired_missing
    cache.set(stats_key, agg, stats_ttl_sec)
    return agg


def raw_jobs_workflow_insights(*, stale_pending_hours: int = 6) -> dict:
    """Queue/funnel/quality/platform-health snapshot for the workflow board."""
    stale_hours = max(1, int(stale_pending_hours))
    cache_key = f"rawjobs_workflow_insights_{stale_hours}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    now = timezone.now()
    stale_cutoff = now - timedelta(hours=stale_hours)
    recent_cutoff = now - timedelta(hours=24)

    base = production_rawjobs_queryset()
    funnel = build_funnel_counts(base)

    pending_qs = base.filter(sync_status=RawJob.SyncStatus.PENDING)
    pending_total = pending_qs.count()
    pending_stale_qs = pending_qs.filter(fetched_at__lt=stale_cutoff)
    pending_stale = pending_stale_qs.count()

    pending_aging = {
        "lt_1h": pending_qs.filter(fetched_at__gte=now - timedelta(hours=1)).count(),
        "h_1_6": pending_qs.filter(
            fetched_at__lt=now - timedelta(hours=1), fetched_at__gte=now - timedelta(hours=6)
        ).count(),
        "h_6_24": pending_qs.filter(
            fetched_at__lt=now - timedelta(hours=6), fetched_at__gte=now - timedelta(hours=24)
        ).count(),
        "gt_24h": pending_qs.filter(fetched_at__lt=now - timedelta(hours=24)).count(),
    }

    completed_24h = base.filter(
        sync_status__in=[RawJob.SyncStatus.SYNCED, RawJob.SyncStatus.SKIPPED], updated_at__gte=recent_cutoff
    ).count()
    failed_24h = base.filter(sync_status=RawJob.SyncStatus.FAILED, updated_at__gte=recent_cutoff).count()
    drain_per_hour = round(completed_24h / 24.0, 2) if completed_24h > 0 else 0.0
    eta_hours = round(pending_total / drain_per_hour, 1) if drain_per_hour > 0 else None

    ready_min_conf = get_ready_stage_min_confidence()
    quality_debt = {
        "missing_jd": base.filter(
            has_description=False,
            is_cold=False,
            jd_fetch_skipped=False,
        ).exclude(filter_decision__in=["COLD", "NO_MATCH"]).count(),
        "filtered_out": base.filter(filtered_out_q()).count(),
        "html_heavy": base.filter(has_html_content=True).count(),
        "low_confidence": base.filter(
            Q(category_confidence__lt=ready_min_conf)
            | (
                Q(category_confidence__isnull=True)
                & (Q(classification_confidence__lt=ready_min_conf) | Q(classification_confidence__isnull=True))
            )
        ).exclude(filter_decision=STRONG).count(),
        "missing_salary": base.filter(salary_min__isnull=True, salary_max__isnull=True).count(),
        "missing_location": base.filter(Q(location_raw="") & Q(city="") & Q(state="") & Q(country="")).count(),
        "missing_experience": base.filter(
            Q(experience_level=RawJob.ExperienceLevel.UNKNOWN)
            & Q(years_required__isnull=True)
            & Q(years_required_max__isnull=True)
        ).count(),
    }

    duplicates = {
        "total": base.filter(sync_status=RawJob.SyncStatus.SKIPPED).count(),
        "recent_24h": base.filter(sync_status=RawJob.SyncStatus.SKIPPED, updated_at__gte=recent_cutoff).count(),
    }

    blocked_companies = list(
        pending_stale_qs.values("company_name")
        .annotate(count=Count("id"), last_seen=Max("fetched_at"))
        .order_by("-count")[:10]
    )
    blocked_platforms = list(
        pending_stale_qs.values("platform_slug")
        .annotate(count=Count("id"), last_seen=Max("fetched_at"))
        .order_by("-count")[:10]
    )

    stuck_queue = list(
        pending_stale_qs.values("platform_label_id", "company_name", "platform_slug")
        .annotate(count=Count("id"), oldest=Min("fetched_at"))
        .order_by("-count")[:100]
    )

    recent_runs = CompanyFetchRun.objects.filter(started_at__gte=recent_cutoff)
    platform_health = []
    for row in (
        recent_runs.values("label__platform__slug")
        .annotate(
            runs=Count("id"),
            success=Count("id", filter=Q(status=CompanyFetchRun.Status.SUCCESS)),
            partial=Count("id", filter=Q(status=CompanyFetchRun.Status.PARTIAL)),
            failed=Count("id", filter=Q(status=CompanyFetchRun.Status.FAILED)),
            avg_jobs_found=Avg("jobs_found"),
            avg_jobs_new=Avg("jobs_new"),
            avg_jobs_failed=Avg("jobs_failed"),
            last_run=Max("started_at"),
        )
        .order_by("-runs")
    ):
        slug = (row.get("label__platform__slug") or "unknown").strip() or "unknown"
        runs = int(row.get("runs") or 0)
        success = int(row.get("success") or 0)
        partial = int(row.get("partial") or 0)
        failed = int(row.get("failed") or 0)
        success_rate = round(((success + (partial * 0.5)) / runs) * 100, 1) if runs else 0.0
        platform_health.append(
            {
                "platform_slug": slug,
                "runs": runs,
                "success": success,
                "partial": partial,
                "failed": failed,
                "success_rate": success_rate,
                "avg_jobs_found": round(float(row.get("avg_jobs_found") or 0.0), 1),
                "avg_jobs_new": round(float(row.get("avg_jobs_new") or 0.0), 1),
                "avg_jobs_failed": round(float(row.get("avg_jobs_failed") or 0.0), 1),
                "last_run": row.get("last_run").isoformat() if row.get("last_run") else "",
            }
        )

    payload = {
        "funnel": funnel,
        "queue": {
            "pending_total": pending_total,
            "pending_stale": pending_stale,
            "stale_pending_hours": stale_hours,
            "aging": pending_aging,
            "drain_per_hour": drain_per_hour,
            "eta_hours": eta_hours,
            "completed_24h": completed_24h,
            "failed_24h": failed_24h,
        },
        "quality_debt": quality_debt,
        "duplicates": duplicates,
        "top_blocked": {
            "companies": blocked_companies,
            "platforms": blocked_platforms,
        },
        "stuck_queue": stuck_queue,
        "platform_health": platform_health,
        "generated_at": now.isoformat(),
    }

    cache.set(cache_key, payload, 20)
    return payload

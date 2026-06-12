import logging
import time
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.db import connection, models, transaction
from django.db.models import Count, F, IntegerField, Q, Value
from django.db.models.functions import Coalesce, Length, Mod, Trim
from django.utils import timezone

from core.task_progress import update_task_progress
from .ops_audit import tick_ops_run_progress
from .runtime_config import (
    DEFAULT_JD_BACKFILL_LOCK_STALE_MINUTES,
    get_jd_backfill_lock_stale_minutes,
    require_harvest_engine_config,
)
from .services.enrichment_input import build_enrichment_input

# ─── Harvest compliance constants ────────────────────────────────────────────
# Delay between processing each company within a platform run.
# Applies on top of the per-request delay inside each harvester.
INTER_COMPANY_DELAY_API = 1.5        # seconds — API platforms (GH, Lever, Ashby, Workday)
INTER_COMPANY_DELAY_SCRAPE = 5.0     # seconds — HTML scrape platforms
HTML_SCRAPE_PLATFORMS = {"html_scrape", "icims", "taleo", "jobvite", "ultipro",
                         "applicantpro", "applytojob", "theapplicantmanager",
                         "zoho", "recruitee", "breezy", "teamtailor"}

# ─── Harvest batch run_kind (stored on FetchBatch.audit_payload + logs) ─────


def _resolve_harvest_run_kind(
    *,
    run_kind: str | None,
    test_mode: bool,
    fetch_all: bool,
    platform_slug: str | None,
) -> str:
    """Normalize UI/source of batch: quick_sync | full_crawl_* | platform_smoke."""
    if run_kind:
        return run_kind
    if test_mode:
        return "platform_smoke"
    if fetch_all:
        return "full_crawl_platform" if platform_slug else "full_crawl_all"
    return "quick_sync"
MAX_CONSECUTIVE_FAILURES = 3

logger = logging.getLogger(__name__)

# JD backfill parallel workers: locks older than this are treated as stale (worker crash).
BACKFILL_LOCK_STALE_MINUTES = DEFAULT_JD_BACKFILL_LOCK_STALE_MINUTES
HARVEST_SAFE_TASK_RATE_LIMIT = 3
HARVEST_SAFE_API_STAGGER_MS = 1000
HARVEST_SAFE_SCRAPER_STAGGER_MS = 5000
HARVEST_SAFE_VALIDATE_CONCURRENCY = 3
HARVEST_SAFE_VALIDATE_MAX_JOBS = 800
HARVEST_SAFE_SYNC_MAX_JOBS = 1000
HARVEST_SAFE_DUPLICATE_LIMIT = 1000
BACKFILL_MAX_PARALLEL = 1
OPS_SINGLETON_STALE_MINUTES = 30
OPS_SINGLETON_CACHE_TTL_SECONDS = 45 * 60


def _ops_singleton_cache_key(operation: str) -> str:
    return f"harvest:ops-singleton:{operation}"


def _acquire_ops_singleton(
    operation: str,
    task_id: str,
    *,
    queue: dict | None = None,
    stale_minutes: int = OPS_SINGLETON_STALE_MINUTES,
    cache_ttl_seconds: int = OPS_SINGLETON_CACHE_TTL_SECONDS,
) -> str | None:
    """Best-effort cache lock plus DB heartbeat guard for one active op run."""
    from .models import HarvestOpsRun
    from .ops_audit import active_ops_run_exists, begin_ops_run, finish_ops_run, mark_stale_running_ops

    task_id = task_id or ""
    queue_payload = dict(queue or {})
    stale_marked = mark_stale_running_ops(
        operation,
        stale_after_minutes=stale_minutes,
        reason="singleton_acquire",
    )
    if active_ops_run_exists(
        operation,
        exclude_task_id=task_id,
        stale_after_minutes=stale_minutes,
    ):
        skipped = begin_ops_run(
            operation,
            task_id,
            queue={
                **queue_payload,
                "skipped_duplicate_instance": True,
                "guard": "db_heartbeat",
                "stale_runs_marked": stale_marked,
            },
        )
        finish_ops_run(skipped, HarvestOpsRun.Status.SKIPPED, {"reason": "duplicate_active_operation"})
        return None

    key = _ops_singleton_cache_key(operation)
    if cache.add(key, task_id, cache_ttl_seconds):
        return key

    # Cache can survive a worker crash. If DB says no fresh heartbeat exists,
    # clear the stale cache key once and retry.
    if not active_ops_run_exists(
        operation,
        exclude_task_id=task_id,
        stale_after_minutes=stale_minutes,
    ):
        cache.delete(key)
        if cache.add(key, task_id, cache_ttl_seconds):
            return key

    skipped = begin_ops_run(
        operation,
        task_id,
        queue={
            **queue_payload,
            "skipped_duplicate_instance": True,
            "guard": "cache",
            "stale_runs_marked": stale_marked,
        },
    )
    finish_ops_run(skipped, HarvestOpsRun.Status.SKIPPED, {"reason": "duplicate_active_operation"})
    return None


def _release_ops_singleton(lock_key: str | None, task_id: str) -> None:
    if not lock_key:
        return
    try:
        current = cache.get(lock_key)
        if current in (None, task_id):
            cache.delete(lock_key)
    except Exception:
        logger.debug("Failed to release ops singleton lock %s", lock_key, exc_info=True)


def _invalidate_rawjobs_dashboard_cache() -> None:
    """Ensure Raw Jobs KPI cards refresh quickly after writes."""
    try:
        cache.delete("rawjobs_dashboard_stats")
        cache.delete("rawjobs_expired_missing_jd")
    except Exception:
        pass


def _company_snapshot_fields(company) -> dict:
    """Denormalized company fields copied onto RawJob for fast filtering."""
    if not company:
        return {
            "company_industry": "",
            "company_stage": "",
            "company_funding": "",
            "company_size": "",
            "company_employee_count_band": "",
            "company_founding_year": None,
        }
    return {
        "company_industry": (getattr(company, "industry", "") or "")[:255],
        "company_stage": (getattr(company, "funding_stage", "") or "")[:64],
        "company_funding": (getattr(company, "funding_amount", "") or "")[:128],
        "company_size": (
            (getattr(company, "size_band", "") or "")
            or (getattr(company, "headcount_range", "") or "")
        )[:64],
        "company_employee_count_band": (
            (getattr(company, "employee_count_band", "") or "")
            or (getattr(company, "headcount_range", "") or "")
        )[:64],
        "company_founding_year": getattr(company, "founding_year", None),
    }
def _backfill_inter_job_delay_sec() -> float:
    """Pause between JD fetches in a chunk; Jarvis per-host/global limits handle burst control."""
    from django.conf import settings

    return float(getattr(settings, "HARVEST_BACKFILL_INTER_JOB_DELAY_SEC", 0.05))


def _backfill_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return "\n".join(_backfill_str(v) for v in val if v)
    if isinstance(val, dict):
        return str(val.get("text") or val.get("content") or val.get("name") or "")
    return str(val)


def _supports_select_for_update_skip_locked() -> bool:
    return getattr(connection.features, "supports_select_for_update_skip_locked", False)


def _backfill_eligible_queryset(platform_slug: str | None, include_cold: bool = False):
    """Rows that still need a JD and are not actively claimed (unless lock is stale).

    Scoped harvest gate: only PRIORITY (target-country) jobs get JD backfill by
    default. Cold + unknown-country jobs stay as cheap discovery rows and become
    eligible later when the country resolver upgrades them.

    When ``include_cold=True`` (controlled by HarvestEngineConfig.backfill_jd_include_cold)
    COLD and REVIEW_* jobs are also included — useful for full-coverage audits.
    """
    from .models import RawJob

    stale_mins = get_jd_backfill_lock_stale_minutes()
    if include_cold:
        q = RawJob.objects.missing_jd(stale_minutes=stale_mins)
    else:
        q = RawJob.objects.missing_jd(stale_minutes=stale_mins).filter(
            is_priority=True,
        )
    if platform_slug:
        q = q.filter(platform_slug=platform_slug)
    return q


def _claim_backfill_job_batch(
    claim_size: int,
    platform_slug: str | None,
    *,
    shard_index: int = 0,
    shard_count: int = 1,
    include_cold: bool = False,
) -> list:
    """
    Claim up to *claim_size* rows for JD backfill.

    - **PostgreSQL**: ``SELECT … FOR UPDATE SKIP LOCKED`` — parallel chunks may
      all use ``shard_count=1`` and compete for the next rows.
    - **SQLite / no SKIP LOCKED**: use ``shard_index`` + ``shard_count`` with
      ``MOD(pk, shard_count) = shard_index`` so parallel chunks never claim the
      same primary key (safe without row-level skip locked).
    """
    from .models import RawJob

    eligible = _backfill_eligible_queryset(platform_slug, include_cold=include_cold)
    sc = max(1, int(shard_count))
    si = int(shard_index) % sc
    if sc > 1:
        eligible = (
            eligible.annotate(
                _bk_shard=Mod(F("pk"), Value(sc, output_field=IntegerField())),
            )
            .filter(_bk_shard=si)
        )

    with transaction.atomic():
        if _supports_select_for_update_skip_locked():
            locked = list(
                eligible.select_for_update(skip_locked=True, of=("self",))
                .order_by("pk")[:claim_size]
            )
        else:
            locked = list(eligible.select_for_update().order_by("pk")[:claim_size])
        if not locked:
            return []
        ids = [j.pk for j in locked]
        now = timezone.now()
        RawJob.objects.filter(pk__in=ids).update(jd_backfill_locked_at=now)
    return list(RawJob.objects.filter(pk__in=ids).order_by("pk"))


@shared_task(bind=True, name="harvest.release_stale_jd_backfill_locks", max_retries=0)
def release_stale_jd_backfill_locks_task(self):
    """Clear stale JD lock timestamps so crashed workers do not hide backlog rows."""
    from .models import HarvestOpsRun, RawJob
    from .ops_audit import begin_ops_run, finish_ops_run

    stale_minutes = get_jd_backfill_lock_stale_minutes()
    cutoff = timezone.now() - timedelta(minutes=stale_minutes)
    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.BACKFILL_JD,
        getattr(self.request, "id", "") or "",
        queue={
            "maintenance": "release_stale_jd_backfill_locks",
            "stale_minutes": stale_minutes,
        },
    )
    stale_qs = RawJob.objects.filter(
        has_description=False,
        jd_backfill_locked_at__isnull=False,
        jd_backfill_locked_at__lt=cutoff,
    )
    stale_count = stale_qs.count()
    cleared = stale_qs.update(jd_backfill_locked_at=None)
    if cleared:
        logger.warning(
            "Released %s stale JD backfill locks older than %s minutes",
            cleared,
            stale_minutes,
        )
    completion = {
        "stale_minutes": stale_minutes,
        "stale_count": stale_count,
        "cleared": cleared,
    }
    finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, completion)
    return completion


@shared_task(bind=True, name="harvest.run_jd_gate", max_retries=0, soft_time_limit=1800, time_limit=2100)
def run_jd_gate_task(
    self,
    *,
    batch_size: int = 100,
    scope: str | None = None,
    dry_run: bool = False,
    trigger_backfill: bool = True,
):
    """
    Tier-2 JD content gate — runs on AMBIGUOUS jobs after the title gate.

    Picks up jobs with title_gate_decision=PENDING (AMBIGUOUS) or legacy POSSIBLE
    filter_decision, fetches a JD snippet, and calls the LLM binary YES/NO gate.

    Results:
      CONFIRMED → queued for full JD backfill (same queue as HARD_YES).
      REJECTED  → marked is_cold=True, jd_fetch_skipped=True. No further processing.
      UNCERTAIN → jd_gate_decision=UNCERTAIN. Human review queue.

    In audit_mode (HarvestEngineConfig.jd_gate_audit_mode=True):
      Decisions are recorded but no jobs are suppressed. Safe to run for tuning.

    Scheduled: every 30 minutes (configured in celery.py beat schedule).
    Also triggered automatically after each company fetch batch completes
    when HarvestEngineConfig.jd_gate_enabled=True.
    """
    from .models import HarvestEngineConfig
    from .content_gate import run_content_gate

    try:
        cfg = HarvestEngineConfig.get()
        if not cfg.jd_gate_enabled:
            logger.info("run_jd_gate_task: jd_gate_enabled=False — skipping")
            return {"skipped": True, "reason": "jd_gate_enabled=False"}
    except Exception as exc:
        logger.warning("run_jd_gate_task: could not load HarvestEngineConfig: %s", exc)
        return {"skipped": True, "reason": str(exc)}

    logger.info(
        "run_jd_gate_task: starting batch_size=%d scope=%s dry_run=%s",
        batch_size, scope or cfg.jd_gate_scope, dry_run,
    )

    try:
        result = run_content_gate(
            batch_size=batch_size,
            dry_run=dry_run,
            scope=scope,
            trigger_backfill_on_confirm=trigger_backfill,
        )
    except SoftTimeLimitExceeded:
        logger.warning("run_jd_gate_task: soft time limit exceeded — partial run")
        return {"status": "PARTIAL", "reason": "soft_time_limit"}
    except Exception as exc:
        logger.error("run_jd_gate_task: unhandled error: %s", exc, exc_info=True)
        raise

    summary = {
        "status":      "OK",
        "total":       result.total_processed,
        "confirmed":   result.confirmed,
        "rejected":    result.rejected,
        "uncertain":   result.uncertain,
        "errors":      result.errors,
        "audit_mode":  result.audit_mode,
        "duration_s":  round(result.duration_seconds, 1),
        "model":       result.model,
    }
    logger.info("run_jd_gate_task: complete — %s", summary)
    return summary


@shared_task(bind=True, name="harvest.reevaluate_cold_scope_jobs", max_retries=0)
def reevaluate_cold_scope_jobs_task(
    self,
    batch_size: int = 1000,
    max_jobs: int = 0,
    reason: str = "",
    queued_before: str = "",
):
    """Re-run scope evaluation for cold/review jobs after target-country changes."""
    from django.utils.dateparse import parse_datetime

    from .location_resolver import evaluate_rawjob_scope
    from .models import HarvestOpsRun, RawJob
    from .ops_audit import begin_ops_run, finish_ops_run

    cfg = require_harvest_engine_config("reevaluate_cold_scope_jobs")
    batch_size = min(5000, max(100, int(batch_size or 1000)))
    max_jobs = max(0, int(max_jobs or 0))
    cutoff_dt = parse_datetime(queued_before) if queued_before else timezone.now()
    if cutoff_dt is None:
        cutoff_dt = timezone.now()
    if timezone.is_naive(cutoff_dt):
        cutoff_dt = timezone.make_aware(cutoff_dt, timezone.get_current_timezone())
    queued_before = cutoff_dt.isoformat()

    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.EVALUATE_SCOPE,
        getattr(self.request, "id", "") or "",
        queue={
            "maintenance": "reevaluate_cold_scope_jobs",
            "batch_size": batch_size,
            "max_jobs": max_jobs,
            "reason": reason,
            "queued_before": queued_before,
        },
    )

    statuses = [
        RawJob.ScopeStatus.UNSCOPED,
        RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY,
        RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY,
        RawJob.ScopeStatus.COLD_NO_LOCATION,
    ]
    qs = RawJob.objects.filter(scope_status__in=statuses).filter(
        Q(last_scope_evaluated_at__isnull=True) |
        Q(last_scope_evaluated_at__lt=cutoff_dt)
    )
    remaining_before = qs.count()
    claim_size = min(batch_size, max_jobs) if max_jobs else batch_size
    jobs = list(qs.order_by("pk")[:claim_size])
    if not jobs:
        completion = {
            "processed": 0,
            "remaining_before": remaining_before,
            "promoted_priority": 0,
            "review": 0,
            "cold": 0,
            "queued_next": False,
        }
        finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, completion)
        return completion

    update_task_progress(
        self,
        current=0,
        total=len(jobs),
        message=f"Re-evaluating scope for {len(jobs):,} cold/review jobs...",
    )

    fields = [
        "country_code", "country_confidence", "country_source", "country_codes",
        "location_candidates", "scope_status", "scope_reason", "is_priority",
        "last_scope_evaluated_at", "country", "state", "city", "location_raw",
        "updated_at",
    ]
    now = timezone.now()
    promoted = review = cold = errors = 0
    to_update: list[RawJob] = []
    for idx, job in enumerate(jobs, start=1):
        try:
            updates = evaluate_rawjob_scope(job, cfg=cfg, use_provider=None, save=False)
            for field, value in updates.items():
                setattr(job, field, value)
            job.updated_at = now
            to_update.append(job)
            if job.scope_status == RawJob.ScopeStatus.PRIORITY_TARGET:
                promoted += 1
            elif job.scope_status == RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY:
                review += 1
            else:
                cold += 1
        except Exception as exc:
            errors += 1
            logger.warning("Scope re-evaluation failed for RawJob %s: %s", job.pk, exc)
        if idx % 200 == 0:
            update_task_progress(
                self,
                current=idx,
                total=len(jobs),
                message=f"Re-scoped {idx:,}/{len(jobs):,} jobs...",
            )

    if to_update:
        RawJob.objects.bulk_update(to_update, fields, batch_size=500)

    processed = len(jobs)
    queued_next = bool(max_jobs == 0 and processed >= batch_size)
    if queued_next:
        reevaluate_cold_scope_jobs_task.apply_async(
            kwargs={
                "batch_size": batch_size,
                "max_jobs": 0,
                "reason": reason or "continuation",
                "queued_before": queued_before,
            },
            countdown=3,
            queue="harvest",
        )

    completion = {
        "processed": processed,
        "remaining_before": remaining_before,
        "promoted_priority": promoted,
        "review": review,
        "cold": cold,
        "errors": errors,
        "queued_next": queued_next,
    }
    status = HarvestOpsRun.Status.PARTIAL if errors else HarvestOpsRun.Status.SUCCESS
    finish_ops_run(ops_run, status, completion)
    _invalidate_rawjobs_dashboard_cache()
    return completion


@shared_task(bind=True, name="harvest.backfill_platform_labels_from_jobs")
def backfill_platform_labels_from_jobs_task(self):
    """
    Scan all job original_link URLs, detect ATS platform from URL patterns,
    and create/update CompanyPlatformLabel records — no HTTP requests needed.

    Runs after every bulk job import so new companies get labeled immediately.
    """
    from jobs.models import Job
    from companies.models import Company
    from .models import JobBoardPlatform, CompanyPlatformLabel
    from .detectors import extract_tenant, get_url_patterns
    from .detectors.url_pattern import pattern_matches_url

    update_task_progress(self, current=0, total=0, message="Loading job URLs…")

    platforms = {p.slug: p for p in JobBoardPlatform.objects.filter(is_enabled=True)}
    patterns_by_slug = get_url_patterns()
    company_best: dict = {}

    all_jobs = list(
        Job.objects.exclude(original_link="")
        .filter(company_obj__isnull=False)
        .values("company_obj_id", "original_link")
    )
    total_jobs = len(all_jobs)

    update_task_progress(self, current=0, total=total_jobs, message=f"Scanning {total_jobs} job URLs…")

    for idx, job in enumerate(all_jobs, start=1):
        cid = job["company_obj_id"]
        if cid in company_best:
            continue
        raw_url = job["original_link"]
        for slug, patterns in patterns_by_slug.items():
            for pattern in patterns:
                if pattern_matches_url(pattern, raw_url):
                    company_best[cid] = {
                        "slug": slug,
                        "tenant_id": extract_tenant(slug, raw_url),
                    }
                    break
            if cid in company_best:
                break

        if idx % 200 == 0:
            update_task_progress(
                self,
                current=idx,
                total=total_jobs,
                message=f"Scanned {idx}/{total_jobs} URLs · {len(company_best)} platforms found…",
            )

    matches = len(company_best)
    update_task_progress(self, current=total_jobs, total=total_jobs,
                         message=f"URL scan done — labeling {matches} companies…")

    created = updated = 0
    now = timezone.now()
    items = list(company_best.items())

    for i, (company_id, info) in enumerate(items, start=1):
        platform = platforms.get(info["slug"])
        if not platform:
            continue
        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            continue

        _, was_created = CompanyPlatformLabel.objects.update_or_create(
            company=company,
            defaults={
                "platform": platform,
                "confidence": "HIGH",
                "detection_method": "URL_PATTERN",
                "tenant_id": info["tenant_id"],
                "detected_at": now,
                "last_checked_at": now,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

        if i % 50 == 0:
            update_task_progress(
                self,
                current=i,
                total=matches,
                message=f"Labeled {i}/{matches} companies ({created} new, {updated} updated)…",
            )

    logger.info(f"backfill_platform_labels_from_jobs: {created} created, {updated} updated")
    return {"created": created, "updated": updated}


@shared_task(bind=True, max_retries=2, name="harvest.detect_company_platforms")
def detect_company_platforms_task(
    self,
    batch_size: int | None = None,
    force_recheck: bool = False,
    triggered_user_id: int | None = None,
    platform_slug: str | None = None,
):
    """
    Run 3-step platform detection for companies without labels (or stale ones).
    Step 1: URL Pattern → Step 2: HTTP HEAD → Step 3: HTML Parse
    Phase 5: audit goes to PipelineEvent instead of HarvestRun.

    batch_size — defaults to HarvestEngineConfig.detect_batch_size (configurable from GUI)
    platform_slug — optional: limit detection to companies already labeled with this platform
    """
    from django.contrib.auth import get_user_model
    from jobs.models import PipelineEvent

    from companies.models import Company
    from .models import HarvestEngineConfig, HarvestOpsRun, JobBoardPlatform, CompanyPlatformLabel
    from .detectors import run_detection_pipeline, extract_tenant
    from .ops_audit import begin_ops_run, finish_ops_run

    # Use config value when caller doesn't specify
    if batch_size is None:
        batch_size = HarvestEngineConfig.get().detect_batch_size

    stale_threshold = timezone.now() - timedelta(days=7)

    if force_recheck:
        base_qs = Company.objects.all()
        if platform_slug:
            base_qs = base_qs.filter(platform_label__platform__slug=platform_slug)
        company_ids = list(base_qs.values_list("id", flat=True)[:batch_size])
    else:
        label_filter = {}
        if platform_slug:
            label_filter["platform__slug"] = platform_slug
        stale_ids = list(
            CompanyPlatformLabel.objects.filter(
                last_checked_at__lt=stale_threshold,
                detection_method__in=["UNDETECTED", "HTML_PARSE"],
                **label_filter,
            ).values_list("company_id", flat=True)
        )
        unlabeled_qs = Company.objects.exclude(platform_label__isnull=False)
        if platform_slug:
            # For unlabeled companies filtering by platform doesn't apply
            # (they have no label yet) — just include all unlabeled
            pass
        unlabeled_ids = list(unlabeled_qs.values_list("id", flat=True))
        company_ids = list(set(stale_ids + unlabeled_ids))[:batch_size]

    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.DETECT_PLATFORMS,
        getattr(self.request, "id", "") or "",
        user_id=triggered_user_id,
        progress_total=len(company_ids),
        queue={
            "batch_size": batch_size,
            "force_recheck": force_recheck,
            "platform_slug": platform_slug or "",
            "companies_planned": len(company_ids),
        },
    )

    if not company_ids:
        logger.info("No companies need platform detection.")
        finish_ops_run(
            ops_run,
            HarvestOpsRun.Status.SUCCESS,
            {"detected": 0, "total": 0, "note": "no_companies_targeted"},
        )
        return {"detected": 0, "total": 0}

    companies = Company.objects.filter(id__in=company_ids).order_by("id")
    company_list = list(companies)
    total_n = len(company_list)
    detected = 0
    errors: list[str] = []

    update_task_progress(self, current=0, total=total_n, message="Starting platform detection…")
    tick_ops_run_progress(ops_run, 0, total_n, "Starting platform detection…", force=True)

    try:
        for idx, company in enumerate(company_list, start=1):
            try:
                slug, confidence, method = run_detection_pipeline(company)

                platform = None
                tenant_id = ""
                if slug:
                    platform = JobBoardPlatform.objects.filter(slug=slug, is_enabled=True).first()
                    url = company.career_site_url or company.website or ""
                    tenant_id = extract_tenant(slug, url)

                CompanyPlatformLabel.objects.update_or_create(
                    company=company,
                    defaults={
                        "platform": platform,
                        "confidence": confidence,
                        "detection_method": method,
                        "detected_at": timezone.now() if slug else None,
                        "last_checked_at": timezone.now(),
                        "tenant_id": tenant_id,
                    },
                )
                if slug:
                    detected += 1
            except Exception as e:
                msg = f"Company {company.id}: {e}"
                logger.error("Detection failed: %s", msg)
                errors.append(msg[:300])

            time.sleep(2.0)
            _det_msg = f"{idx}/{total_n} · {(company.name or str(company.pk))[:60]}"
            update_task_progress(
                self, current=idx, total=total_n,
                message=_det_msg,
            )
            tick_ops_run_progress(ops_run, idx, total_n, _det_msg)

        status = "SUCCESS" if not errors else ("PARTIAL" if detected else "FAILED")
        PipelineEvent.record(
            task_name="harvest.detect_company_platforms",
            celery_id=self.request.id or "",
            status=PipelineEvent.Status.SUCCESS if status == "SUCCESS" else PipelineEvent.Status.FAILED,
            meta={"detected": detected, "total": len(company_ids), "errors": errors[:10]},
        )
        ops_fin_status = (
            HarvestOpsRun.Status.SUCCESS
            if status == "SUCCESS"
            else (HarvestOpsRun.Status.PARTIAL if detected else HarvestOpsRun.Status.FAILED)
        )
        finish_ops_run(
            ops_run,
            ops_fin_status,
            {
                "detected": detected,
                "total": len(company_ids),
                "errors_sample": errors[:10],
                "ops_audit_note": "see PipelineEvent for legacy row",
            },
        )
        logger.info("Detection done: %s/%s detected.", detected, len(company_ids))
        return {"detected": detected, "total": len(company_ids)}

    except Exception as e:
        logger.exception("detect_company_platforms_task failed: %s", e)
        finish_ops_run(
            ops_run,
            HarvestOpsRun.Status.FAILED,
            {"detected": detected, "total": len(company_ids), "error": str(e)[:500]},
        )
        PipelineEvent.record(
            task_name="harvest.detect_company_platforms",
            celery_id=self.request.id or "",
            status=PipelineEvent.Status.FAILED,
            error=str(e)[:2000],
            meta={"detected": detected, "total": len(company_ids)},
        )
        raise


@shared_task(bind=True, max_retries=2, name="harvest.harvest_jobs")
def harvest_jobs_task(
    self,
    platform_slug: str | None = None,
    since_hours: int = 24,
    max_companies: int = 50,
    triggered_by: str = "SCHEDULED",
    triggered_user_id: int | None = None,
):
    """Harvest jobs from all enabled platforms → write directly to RawJob (Phase 5)."""
    import hashlib as _hashlib
    from django.contrib.auth import get_user_model
    from jobs.models import PipelineEvent

    from .models import JobBoardPlatform, CompanyPlatformLabel, RawJob
    from .harvesters import get_harvester
    from .normalizer import normalize_job_data
    from .rate_limiter import throttle as _throttle
    from .enrichments import clean_job_content, clean_job_text, extract_enrichments
    from .location_resolver import extract_location_candidates

    tb = triggered_by if triggered_by in ("SCHEDULED", "MANUAL") else "SCHEDULED"

    qs = JobBoardPlatform.objects.filter(is_enabled=True)
    if platform_slug:
        qs = qs.filter(slug=platform_slug)

    total_new = total_dup = total_fail = 0

    for platform in qs:
        labels_qs = CompanyPlatformLabel.objects.filter(
            platform=platform,
            detection_method__in=["URL_PATTERN", "HTTP_HEAD", "HTML_PARSE", "MANUAL"],
        ).select_related("company")[:max_companies]

        labels_list = list(labels_qs)
        if not labels_list:
            continue

        harvester = get_harvester(platform.slug)
        if harvester is None:
            continue
        is_scraper = platform.slug in HTML_SCRAPE_PLATFORMS
        inter_delay = INTER_COMPANY_DELAY_SCRAPE if is_scraper else INTER_COMPANY_DELAY_API

        jobs_new = jobs_dup = jobs_fail = 0
        errors: list[str] = []
        consecutive_failures = 0

        total_l = len(labels_list)
        update_task_progress(self, current=0, total=total_l, message=f"Harvest {platform.name}: starting…")

        for i, label in enumerate(labels_list, start=1):
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning("[HARVEST] Circuit breaker on %s after %d failures", platform.name, consecutive_failures)
                break

            company = label.company
            tenant_id = label.tenant_id or ""
            _throttle(platform.slug)
            try:
                raw_jobs = harvester.fetch_jobs(company, tenant_id, since_hours=since_hours)
                if not raw_jobs:
                    # An empty board is a NORMAL outcome (small company, nothing new in
                    # the since_hours window) — it must NOT trip the circuit breaker.
                    # Counting empties as failures made 3 quiet companies silently kill
                    # an entire platform's harvest (root cause of the May–June stall).
                    # Only real exceptions (the except branch below) advance the breaker.
                    logger.info("[HARVEST] %s / %s: 0 jobs returned", platform.slug, company.name)
                else:
                    consecutive_failures = 0

                for raw in raw_jobs:
                    try:
                        normalized = normalize_job_data(raw, platform, company, harvest_run=None)
                        original_url = normalized.get("original_url", "")
                        url_hash = normalized.get("url_hash", "")
                        if not original_url or not url_hash:
                            continue
                        desc_meta = clean_job_content(normalized.get("description_text", ""), max_len=50000)
                        description = desc_meta["clean_text"]
                        requirements = clean_job_text(normalized.get("requirements_text", ""), max_len=20000)
                        benefits = clean_job_text(normalized.get("benefits_text", ""), max_len=10000)
                        enriched = extract_enrichments(build_enrichment_input(
                            normalized,
                            overrides={
                                "description": description,
                                "description_clean": description[:50000],
                                "description_raw_html": (desc_meta.get("raw_html") or "")[:120000],
                                "has_html_content": bool(desc_meta.get("has_html_content")),
                                "cleaning_version": (desc_meta.get("cleaning_version") or "v2")[:20],
                                "requirements": requirements,
                                "benefits": benefits,
                                "location_raw": normalized.get("location", ""),
                                "employment_type": normalized.get("job_type", "UNKNOWN"),
                                "experience_level": "UNKNOWN",
                            },
                            company_name=normalized.get("company_name", company.name),
                            posted_date=normalized.get("posted_date"),
                        ))

                        # Map HarvestedJob-shaped dict → RawJob fields
                        rj_defaults = {
                            "company": company,
                            "job_platform": platform,
                            "platform_slug": platform.slug,
                            "external_id": normalized.get("external_id", "")[:512],
                            "original_url": original_url[:1024],
                            "title": normalized.get("title", "")[:512],
                            "company_name": normalized.get("company_name", company.name)[:256],
                            "location_raw": normalized.get("location", "")[:512],
                            "is_remote": normalized.get("is_remote", False) or False,
                            "employment_type": normalized.get("job_type", "UNKNOWN"),
                            "department": normalized.get("department", "")[:256],
                            "salary_min": normalized.get("salary_min"),
                            "salary_max": normalized.get("salary_max"),
                            "salary_currency": normalized.get("salary_currency", "USD")[:8],
                            "salary_raw": normalized.get("salary_raw", "")[:256],
                            "description": description,
                            "requirements": requirements,
                            "benefits": benefits,
                            "posted_date": normalized.get("posted_date"),
                            "raw_payload": normalized.get("raw_payload", {}),
                            "sync_status": "PENDING",
                            "is_active": True,
                            **_company_snapshot_fields(company),
                            **enriched,
                        }
                        # Use the advisory-lock dedupe service (not a raw update_or_create)
                        # so the same job reached via a DIFFERENT URL is caught by
                        # content-hash/external-id identity instead of slipping in twice.
                        from .services.rawjob_upsert import upsert_raw_job_with_dedupe
                        _res = upsert_raw_job_with_dedupe(
                            company=company,
                            defaults=rj_defaults,
                            url_hash=url_hash,
                            original_url=original_url,
                            external_id=(normalized.get("external_id") or "")[:512],
                            platform_label=label,
                            job_platform=platform,
                            platform_slug=platform.slug,
                        )
                        raw_obj, created = _res.raw_job, _res.created
                        if raw_obj is None:
                            jobs_dup += 1
                            continue
                        try:
                            from .models import RawJobPayloadSnapshot
                            from .payload_archive import capture_rawjob_source_payloads
                            capture_rawjob_source_payloads(
                                raw_obj,
                                normalized,
                                default_payload_kind=RawJobPayloadSnapshot.PayloadKind.API_RESPONSE,
                                default_source_url=original_url,
                                default_platform_slug=platform.slug,
                                default_source_metadata={"ingest": "harvest_jobs", "external_id": normalized.get("external_id", "")},
                            )
                        except Exception:
                            logger.exception("Failed to archive source payload for RawJob %s", url_hash)
                        if created:
                            jobs_new += 1
                        else:
                            jobs_dup += 1
                    except Exception as e:
                        jobs_fail += 1
                        errors.append(str(e)[:200])

            except Exception as e:
                jobs_fail += 1
                consecutive_failures += 1
                errors.append(f"Company {company.id} ({company.name}): {str(e)[:150]}")

            time.sleep(inter_delay)
            update_task_progress(self, current=i, total=total_l, message=f"{platform.name}: {i}/{total_l}")

        total_new += jobs_new; total_dup += jobs_dup; total_fail += jobs_fail

        # Audit via PipelineEvent instead of HarvestRun
        status = "SUCCESS" if not errors else ("PARTIAL" if jobs_new > 0 else "FAILED")
        PipelineEvent.record(
            task_name="harvest.harvest_jobs",
            celery_id=self.request.id or "",
            status=PipelineEvent.Status.SUCCESS if status == "SUCCESS" else PipelineEvent.Status.FAILED,
            meta={"platform": platform.slug, "new": jobs_new, "dup": jobs_dup, "fail": jobs_fail,
                  "errors": errors[:10], "trigger": tb},
        )
        platform.last_harvested_at = timezone.now()
        platform.save(update_fields=["last_harvested_at"])
        logger.info("Harvest %s: +%d new, %d dup, %d fail", platform.name, jobs_new, jobs_dup, jobs_fail)

    return {"new": total_new, "dup": total_dup, "fail": total_fail}


@shared_task(bind=True, name="harvest.check_portal_health")
def check_portal_health_task(self, label_pk: int):
    """
    HTTP-check a single career portal URL and update portal_alive + portal_last_verified.
    Called individually per label — queue many at once via verify_all_portals_task.
    """
    import requests
    from .models import CompanyPlatformLabel

    try:
        label = CompanyPlatformLabel.objects.select_related("platform").get(pk=label_pk)
    except CompanyPlatformLabel.DoesNotExist:
        return

    from .career_url import build_career_url
    url = build_career_url(
        label.platform.slug if label.platform else "",
        label.tenant_id or "",
    )
    if not url:
        return

    try:
        cfg = require_harvest_engine_config("check_portal_health")
        failure_threshold = int(getattr(cfg, "portal_health_failure_threshold", 2) or 2)
    except Exception:
        failure_threshold = 2
    failure_threshold = min(10, max(1, failure_threshold))

    alive = False
    try:
        resp = requests.head(
            url,
            timeout=12,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; GoCareers-PortalBot/1.0; "
                    "+https://chennu.co)"
                )
            },
        )
        # Treat 2xx and 3xx (after redirect) as alive; 4xx/5xx as down
        if resp.status_code >= 400:
            # Some ATS block HEAD — retry with GET (just first bytes)
            resp = requests.get(
                url,
                timeout=15,
                stream=True,
                allow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; GoCareers-PortalBot/1.0)"
                    )
                },
            )
            resp.close()
        alive = resp.status_code < 400
    except Exception:
        alive = False

    if alive:
        label.portal_alive = True
        label.portal_consecutive_failures = 0
    else:
        label.portal_consecutive_failures = int(label.portal_consecutive_failures or 0) + 1
        if label.portal_consecutive_failures >= failure_threshold:
            label.portal_alive = False
        else:
            logger.warning(
                "Portal health pending failure %s/%s for label=%s url=%s",
                label.portal_consecutive_failures,
                failure_threshold,
                label_pk,
                url,
            )
    label.portal_last_verified = timezone.now()
    label.save(update_fields=[
        "portal_alive",
        "portal_last_verified",
        "portal_consecutive_failures",
    ])


@shared_task(bind=True, name="harvest.verify_all_portals")
def verify_all_portals_task(self):
    """
    Queue HTTP health checks for all CompanyPlatformLabels that have a career URL.
    Each check runs asynchronously via check_portal_health_task.
    """
    from .models import CompanyPlatformLabel

    update_task_progress(self, current=0, total=0, message="Queuing portal health checks…")

    label_pks = list(
        CompanyPlatformLabel.objects.filter(
            platform__isnull=False,
        ).exclude(tenant_id="").exclude(tenant_id__isnull=True)
        .values_list("pk", flat=True)
    )

    total = len(label_pks)
    update_task_progress(self, current=0, total=total, message=f"Queuing {total} checks…")

    for i, pk in enumerate(label_pks, start=1):
        check_portal_health_task.apply_async(
            args=[pk],
            countdown=i * 0.3,   # stagger by 0.3s each to avoid hammering
        )
        if i % 50 == 0:
            update_task_progress(
                self, current=i, total=total,
                message=f"Queued {i}/{total} checks…",
            )

    update_task_progress(self, current=total, total=total,
                         message=f"✅ All {total} portal checks queued!")
    return {"queued": total}


@shared_task(
    bind=True,
    name="harvest.fetch_raw_jobs_for_company",
    max_retries=2,
    default_retry_delay=60,
    # soft_time_limit / time_limit / rate_limit are all read from HarvestEngineConfig
    # at task startup — see the body of the function.  These decorator-level values
    # are the safe fallbacks used only if the DB read fails.
    soft_time_limit=480,
    time_limit=600,
    rate_limit="6/m",
)
def fetch_raw_jobs_for_company_task(
    self,
    label_pk: int,
    batch_id: int = None,
    triggered_by: str = "MANUAL",
    max_jobs: int | None = None,
    since_hours: int | None = None,
    fetch_all: bool = False,
    filter_snapshot_id: str | None = None,
):
    """
    Fetch ALL jobs for a single CompanyPlatformLabel and upsert into RawJob.
    Creates a CompanyFetchRun audit record. Updates FetchBatch counters if batch_id given.
    """
    import requests
    from datetime import date

    from .models import CompanyPlatformLabel, CompanyFetchRun, FetchBatch, RawJob
    from .harvesters import get_harvester
    from .normalizer import compute_url_hash, compute_content_hash
    from .location_resolver import extract_location_candidates

    # ── Read live config from DB — required for harvest correctness ───────────
    _cfg = require_harvest_engine_config("fetch_raw_jobs_for_company_task")
    # Apply the current rate limit to THIS worker's slot for this task type.
    # This ensures the DB value is always honoured even after a live GUI change.
    self.rate_limit = f"{min(int(_cfg.task_rate_limit or 1), HARVEST_SAFE_TASK_RATE_LIMIT)}/m"
    _soft_limit = _cfg.task_soft_time_limit_secs
    _hard_limit = _soft_limit + 120

    # ── Load label ────────────────────────────────────────────────────────────
    try:
        label = CompanyPlatformLabel.objects.select_related("platform", "company").get(pk=label_pk)
    except CompanyPlatformLabel.DoesNotExist:
        logger.warning("fetch_raw_jobs_for_company_task: label %s not found", label_pk)
        return

    batch = None
    if batch_id:
        batch = FetchBatch.objects.filter(pk=batch_id).first()

    filter_enabled = bool(getattr(_cfg, "selective_filter_enabled", False))
    filter_audit_mode = bool(getattr(_cfg, "filter_audit_mode", True))
    # Full Fetch is the permanent audit/admin path. It may classify for visibility,
    # but it must never suppress detail fetches or pool recovery data.
    # Exception: when filter_full_crawl=True, the operator explicitly wants the filter
    # to enforce (drop HARD_NO jobs) even during full crawls. This enables selective
    # harvesting from day one without a separate incremental run first.
    _filter_full_crawl = bool(getattr(_cfg, "filter_full_crawl", False))
    effective_filter_audit_mode = filter_audit_mode or (bool(fetch_all) and not _filter_full_crawl)
    filter_snapshot = None
    filter_categories: list[dict] = []
    filter_hard_negatives: list[str] = []
    if filter_enabled:
        try:
            from .models import HarvestFilterSnapshot

            if filter_snapshot_id:
                filter_snapshot = HarvestFilterSnapshot.objects.filter(snapshot_id=filter_snapshot_id).first()
            if filter_snapshot is None:
                filter_snapshot = HarvestFilterSnapshot.create_snapshot(batch=batch, notes="created by company task")
            filter_snapshot_id = str(filter_snapshot.snapshot_id)
            filter_categories = filter_snapshot.get_categories()
            filter_hard_negatives = filter_snapshot.get_hard_negatives()
        except Exception:
            logger.exception("Selective filter snapshot load failed; continuing in audit-safe full mode.")
            filter_enabled = False
            filter_snapshot_id = None

    # ── Stop-gate: bail immediately if operator cancelled the batch ───────────
    # stop_requested is set by StopBatchView. Tasks with countdown that were
    # already queued when Stop was clicked will see this flag and exit without
    # doing any work. This drains the queue fast (pure DB read per task).
    if batch and batch.stop_requested:
        logger.info(
            "fetch_raw_jobs_for_company_task: batch #%s stop_requested — skipping label %s",
            batch_id, label_pk,
        )
        return {"skipped": True, "reason": "stop_requested", "label_pk": label_pk}

    # ── Create run record ─────────────────────────────────────────────────────
    is_test_run = bool(max_jobs)
    run = CompanyFetchRun.objects.create(
        label=label,
        batch=batch,
        status=CompanyFetchRun.Status.RUNNING,
        task_id=self.request.id or "",
        started_at=timezone.now(),
        triggered_by=triggered_by,
        is_test_run=is_test_run,
    )
    try:
        self.update_state(
            state="PROGRESS",
            meta={"percent": 5, "message": "Starting company fetch…"},
        )
    except Exception:
        pass

    # ── Guard: no tenant or no platform ──────────────────────────────────────
    if not label.platform or not label.tenant_id:
        run.status = CompanyFetchRun.Status.SKIPPED
        run.error_type = CompanyFetchRun.ErrorType.NO_TENANT
        run.issue_code = CompanyFetchRun.IssueCode.NO_ACTIVE_TENANT
        run.error_message = "No platform or tenant_id configured."
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "issue_code", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return

    harvester = get_harvester(label.platform.slug)
    if harvester is None:
        run.status = CompanyFetchRun.Status.SKIPPED
        run.error_type = CompanyFetchRun.ErrorType.PLATFORM_ERROR
        run.error_message = f"No harvester for platform slug: {label.platform.slug}"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return

    # ── Fetch ─────────────────────────────────────────────────────────────────
    # Scraper platforms (HTML-based) can't filter by date — always fetch all.
    # API platforms support since_hours for incremental fetches (default: 25h window).
    # test_mode passes max_jobs — skip full pagination to stay fast + respectful.
    # adp uses HTMLScrapeHarvester (not a JSON API) — include in scraper set
    # for proper rate-limiting and fetch_all behavior
    SCRAPER_SLUGS = {"jobvite", "icims", "taleo", "applicantpro", "applytojob",
                     "theapplicantmanager", "zoho", "breezy", "teamtailor", "adp"}
    platform_slug_val = label.platform.slug if label.platform else ""
    is_scraper_platform = platform_slug_val in SCRAPER_SLUGS

    # fetch_all logic:
    #   fetch_all=True  → always paginate through ALL pages, ignore since_hours filter
    #                     (used by FETCH ALL button for initial/full crawl)
    #   fetch_all=False → use since_hours window (fast daily incremental)
    #   scrapers        → always fetch_all (no date filter exists in HTML)
    #   test mode       → fetch_all so we get real data, but capped by max_jobs
    use_fetch_all = fetch_all or is_scraper_platform or (max_jobs is not None)
    effective_since_hours = since_hours if since_hours is not None else 25

    # Phase 3: honor PlatformConfig.inter_request_delay_ms before each fetch.
    from .rate_limiter import throttle as _throttle
    _throttle(label.platform.slug)
    try:
        self.update_state(
            state="PROGRESS",
            meta={"percent": 12, "message": "Connecting to company board…"},
        )
    except Exception:
        pass

    try:
        if is_scraper_platform:
            # HTML scrapers have no date filter — always fetch everything
            raw_jobs = harvester.fetch_jobs(
                label.company,
                label.tenant_id,
                fetch_all=True,
            )
        elif use_fetch_all:
            # Full crawl: get ALL jobs from this company, all pages, ignore time filter
            raw_jobs = harvester.fetch_jobs(
                label.company,
                label.tenant_id,
                fetch_all=True,
            )
        else:
            # Incremental: only jobs updated in the last N hours (fast daily run)
            raw_jobs = harvester.fetch_jobs(
                label.company,
                label.tenant_id,
                since_hours=effective_since_hours,
                fetch_all=False,
            )
        # Capture API-reported total (even when we only fetched a subset)
        run.jobs_total_available = getattr(harvester, "last_total_available", 0) or len(raw_jobs)
        run.jobs_detail_fetched = getattr(harvester, "last_detail_fetched", 0)
        run.jobs_found = len(raw_jobs)
        # If harvester hit a terminal HTTP error (e.g. Greenhouse 404 = invalid board),
        # mark as FAILED/TENANT_INVALID now rather than waiting for zero-job check below.
        _fetch_http_status = getattr(harvester, "last_fetch_http_status", None)
        if _fetch_http_status == 404 and not raw_jobs:
            run.status = CompanyFetchRun.Status.FAILED
            run.error_type = CompanyFetchRun.ErrorType.HTTP_ERROR
            run.issue_code = CompanyFetchRun.IssueCode.TENANT_INVALID
            run.error_message = f"HTTP 404 — board/tenant not found: {label.tenant_id}"
            run.completed_at = timezone.now()
            run.save(update_fields=[
                "status", "error_type", "issue_code", "error_message",
                "jobs_total_available", "jobs_found", "completed_at",
            ])
            if batch:
                FetchBatch.objects.filter(pk=batch.pk).update(
                    failed_companies=models.F("failed_companies") + 1
                )
            return
        run.save(update_fields=["jobs_total_available", "jobs_detail_fetched", "jobs_found"])
        try:
            self.update_state(
                state="PROGRESS",
                meta={
                    "percent": 30 if raw_jobs else 95,
                    "message": f"Discovered {len(raw_jobs)} jobs. Processing…",
                    "jobs_found": len(raw_jobs),
                },
            )
        except Exception:
            pass
    except requests.exceptions.Timeout as exc:
        run.status = CompanyFetchRun.Status.FAILED
        run.error_type = CompanyFetchRun.ErrorType.TIMEOUT
        run.issue_code = CompanyFetchRun.IssueCode.FETCH_TIMEOUT
        run.error_message = str(exc)[:500]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "issue_code", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return
    except requests.exceptions.HTTPError as exc:
        run.status = CompanyFetchRun.Status.FAILED
        # 429 = rate limited — distinguish from generic HTTP errors
        _resp = getattr(exc, "response", None)
        _status_code = getattr(_resp, "status_code", 0)
        if _status_code == 429:
            run.error_type = CompanyFetchRun.ErrorType.RATE_LIMITED
            run.issue_code = CompanyFetchRun.IssueCode.RATE_LIMITED
        else:
            run.error_type = CompanyFetchRun.ErrorType.HTTP_ERROR
        run.error_message = str(exc)[:500]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "issue_code", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return
    except SoftTimeLimitExceeded:
        # Task hit the 8-minute soft limit — mark as PARTIAL so the run is visible
        # in the monitor and doesn't retry (it was already too slow once).
        run.status = CompanyFetchRun.Status.PARTIAL
        run.error_type = CompanyFetchRun.ErrorType.TIMEOUT
        run.issue_code = CompanyFetchRun.IssueCode.PARTIAL_RESULTS
        run.error_message = "Soft time limit exceeded (8 min) — task killed gracefully."
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "issue_code", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        logger.warning("fetch_raw_jobs_for_company_task: soft time limit hit for label %s", label_pk)
        return
    except Exception as exc:
        run.status = CompanyFetchRun.Status.FAILED
        run.error_type = CompanyFetchRun.ErrorType.PARSE_ERROR
        run.error_message = str(exc)[:500]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        logger.exception("fetch_raw_jobs_for_company_task failed for label %s: %s", label_pk, exc)
        return

    # ── Upsert jobs ───────────────────────────────────────────────────────────
    jobs_new = jobs_updated = jobs_duplicate = jobs_failed = jobs_pre_filtered = 0
    filter_strong = filter_possible = filter_unknown = filter_cold = filter_no_match = 0
    upsert_errors: list[str] = []
    new_raw_job_pks: list[int] = []  # PKs of freshly-created RawJobs for auto-pipeline
    unknown_jd_count = 0

    # In test mode, cap to max_jobs so we don't write hundreds of rows
    cap_applied = False
    if max_jobs and len(raw_jobs) > max_jobs:
        cap_applied = True
        raw_jobs = raw_jobs[:max_jobs]

    total_jobs = len(raw_jobs)
    from .enrichments import clean_job_content, clean_job_text, extract_enrichments
    from .role_filter import COLD, NO_MATCH, POSSIBLE, STRONG, UNKNOWN, ClassifyResult, classify_title
    for idx, job_dict in enumerate(raw_jobs, start=1):
        try:
            original_url = (job_dict.get("original_url") or "").strip()
            if not original_url:
                jobs_failed += 1
                continue

            url_hash = compute_url_hash(original_url)
            if not url_hash:
                jobs_failed += 1
                continue
            external_id = (job_dict.get("external_id") or "").strip()[:512]

            # Parse posted_date
            posted_date = None
            posted_raw = job_dict.get("posted_date_raw", "")
            if posted_raw:
                try:
                    # Handle ISO format: 2024-01-15T00:00:00Z or 2024-01-15
                    posted_date = date.fromisoformat(
                        posted_raw[:10].replace("Z", "")
                    )
                except Exception:
                    pass

            # Parse closing_date
            closing_date = None
            closing_raw = job_dict.get("closing_date", "")
            if closing_raw:
                try:
                    closing_date = date.fromisoformat(closing_raw[:10])
                except Exception:
                    pass

            filter_result = ClassifyResult(
                decision=POSSIBLE,
                category=None,
                matched_phrase=None,
                matched_negative=None,
                reason="selective filter disabled",
                snapshot_id=None,
            )
            if filter_enabled and filter_snapshot_id:
                if getattr(label.platform, "title_in_list", False):
                    filter_result = classify_title(
                        title=job_dict.get("title") or "",
                        department=job_dict.get("department") or "",
                        categories=filter_categories,
                        hard_negatives=filter_hard_negatives,
                        custom_phrases=label.custom_include_phrases or [],
                        snapshot_id=filter_snapshot_id,
                    )
                else:
                    filter_result = ClassifyResult(
                        decision=POSSIBLE,
                        category=None,
                        matched_phrase=None,
                        matched_negative=None,
                        reason="platform title_in_list is false - preserving existing fetch behavior",
                        snapshot_id=filter_snapshot_id,
                    )

            if filter_enabled and filter_result.decision == UNKNOWN:
                budget = int(getattr(label.platform, "unknown_jd_budget_per_run", 0) or 0)
                if unknown_jd_count >= budget:
                    filter_result = ClassifyResult(
                        decision=COLD,
                        category=None,
                        matched_phrase=None,
                        matched_negative=filter_result.matched_negative,
                        reason=f"UNKNOWN budget ({budget}) exceeded for this company",
                        snapshot_id=filter_snapshot_id,
                    )
                else:
                    unknown_jd_count += 1

            should_skip_jd = (
                filter_enabled
                and not effective_filter_audit_mode
                and filter_result.decision in {COLD, NO_MATCH}
            )
            if should_skip_jd:
                desc_meta = {
                    "clean_text": "",
                    "raw_html": "",
                    "has_html_content": False,
                    "cleaning_version": "v2",
                }
                description = ""
                requirements = ""
                responsibilities = ""
                benefits = ""
                enriched = {}
            else:
                desc_meta = clean_job_content(job_dict.get("description") or "", max_len=50000)
                description = desc_meta["clean_text"]
                requirements = clean_job_text(job_dict.get("requirements") or "", max_len=20000)
                responsibilities = clean_job_text(job_dict.get("responsibilities") or "", max_len=20000)
                benefits = clean_job_text(job_dict.get("benefits") or "", max_len=10000)
                enriched = extract_enrichments(build_enrichment_input(
                    job_dict,
                    overrides={
                        "description": description,
                        "description_clean": description[:50000],
                        "description_raw_html": (desc_meta.get("raw_html") or "")[:120000],
                        "has_html_content": bool(desc_meta.get("has_html_content")),
                        "cleaning_version": (desc_meta.get("cleaning_version") or "v2")[:20],
                        "requirements": requirements,
                        "responsibilities": responsibilities,
                        "benefits": benefits,
                    },
                    company_name=job_dict.get("company_name") or label.company.name,
                    posted_date=posted_date,
                ))

                if (
                    filter_enabled
                    and filter_snapshot_id
                    and not getattr(label.platform, "title_in_list", False)
                ):
                    post_fetch_result = classify_title(
                        title=job_dict.get("title") or "",
                        department=job_dict.get("department") or "",
                        categories=filter_categories,
                        hard_negatives=filter_hard_negatives,
                        custom_phrases=label.custom_include_phrases or [],
                        snapshot_id=filter_snapshot_id,
                    )
                    filter_result = ClassifyResult(
                        decision=post_fetch_result.decision,
                        category=post_fetch_result.category,
                        matched_phrase=post_fetch_result.matched_phrase,
                        matched_negative=post_fetch_result.matched_negative,
                        reason=f"post-fetch classification: {post_fetch_result.reason}",
                        snapshot_id=post_fetch_result.snapshot_id,
                        confidence=post_fetch_result.confidence,  # MUST forward: pre-storage gate uses this to distinguish HARD_NO (conf<0.2) from AMBIGUOUS (conf≥0.2)
                    )
                    should_skip_jd = False

            filter_blocks_pool = (
                filter_enabled
                and not effective_filter_audit_mode
                and filter_result.decision in {COLD, NO_MATCH}
            )

            # ── Pre-storage title gate (selective fetch) ──────────────────────
            # Drop definitive HARD_NO titles BEFORE any DB write, defaults build,
            # or location extraction — keeps RawJob table clean from day one.
            #
            # Only fires when ALL of:
            #   • selective_filter_enabled=True (filter is on)
            #   • filter_audit_mode=False (enforcement mode, not observation)
            #   • pre_storage_filter_enabled=True (operator opt-in flag)
            #   • filter_blocks_pool=True (COLD or NO_MATCH in enforcement mode)
            #
            # HARD_NO = NO_MATCH  OR  COLD with confidence < 0.2
            # Borderline COLD (conf ≥ 0.2) → AMBIGUOUS → still stored for JD gate.
            # Blank titles → unknown intent → stored (fail-safe, treated as AMBIGUOUS).
            # Any exception in this block → fall through and store (never silently drop).
            # fetch_all / audit_mode paths never reach here (filter_blocks_pool=False).
            if filter_blocks_pool and getattr(_cfg, "pre_storage_filter_enabled", False):
                _pre_title = (job_dict.get("title") or "").strip()
                _is_hard_no = (
                    filter_result.decision == NO_MATCH
                    or (
                        filter_result.decision == COLD
                        and getattr(filter_result, "confidence", 1.0) < 0.2
                    )
                )
                if _pre_title and _is_hard_no:
                    jobs_pre_filtered += 1
                    # Keep filter counters accurate for run summary + zero-tech logic
                    if filter_result.decision == NO_MATCH:
                        filter_no_match += 1
                    else:
                        filter_cold += 1
                    if jobs_pre_filtered <= 5 or jobs_pre_filtered % 100 == 0:
                        logger.debug(
                            "pre-storage drop [%d]: %r decision=%s conf=%.2f label=%s",
                            jobs_pre_filtered,
                            _pre_title[:80],
                            filter_result.decision,
                            getattr(filter_result, "confidence", 0.0),
                            label_pk,
                        )
                    continue  # ← skip upsert, payload archive, new_raw_job_pks entirely

            supplied_location_candidates = job_dict.get("location_candidates")
            if not isinstance(supplied_location_candidates, list):
                supplied_location_candidates = []
            supplied_country_codes = job_dict.get("country_codes")
            if not isinstance(supplied_country_codes, list):
                supplied_country_codes = []
            location_candidates = (
                supplied_location_candidates
                or extract_location_candidates(
                    location_raw=job_dict.get("location_raw") or "",
                    city=job_dict.get("city") or "",
                    state=job_dict.get("state") or "",
                    country=job_dict.get("country") or "",
                    vendor_location_block=job_dict.get("vendor_location_block") or "",
                    raw_payload=job_dict.get("raw_payload") or {},
                )
            )

            defaults = {
                "company": label.company,
                "platform_label": label,
                "fetch_batch": batch,
                "job_platform": label.platform,
                "external_id": external_id,
                "original_url": original_url[:1024],
                "apply_url": (job_dict.get("apply_url") or "")[:1024],
                "title": (job_dict.get("title") or "")[:512],
                "company_name": (job_dict.get("company_name") or label.company.name)[:256],
                "department": (job_dict.get("department") or "")[:256],
                "team": (job_dict.get("team") or "")[:256],
                "location_raw": (job_dict.get("location_raw") or "")[:512],
                "city": (job_dict.get("city") or "")[:128],
                "state": (job_dict.get("state") or "")[:128],
                "country": (job_dict.get("country") or "")[:128],
                "location_candidates": location_candidates,
                "country_codes": supplied_country_codes,
                "location_type": job_dict.get("location_type", "UNKNOWN"),
                "is_remote": bool(job_dict.get("is_remote", False)),
                "employment_type": job_dict.get("employment_type", "UNKNOWN"),
                "experience_level": job_dict.get("experience_level", "UNKNOWN"),
                "salary_min": job_dict.get("salary_min"),
                "salary_max": job_dict.get("salary_max"),
                "salary_currency": (job_dict.get("salary_currency") or "USD")[:8],
                "salary_period": (job_dict.get("salary_period") or "")[:16],
                "salary_raw": (job_dict.get("salary_raw") or "")[:256],
                "description": description,
                "description_clean": (enriched.get("description_clean") or description)[:50000],
                "description_raw_html": (desc_meta.get("raw_html") or "")[:120000],
                "has_html_content": bool(desc_meta.get("has_html_content")),
                "cleaning_version": (desc_meta.get("cleaning_version") or "v2")[:20],
                "requirements": requirements,
                "responsibilities": responsibilities,
                "benefits": benefits,
                "posted_date": posted_date,
                "closing_date": closing_date,
                "platform_slug": (label.platform.slug if label.platform else "")[:64],
                "vendor_job_identification": (job_dict.get("vendor_job_identification") or "")[:128],
                "vendor_job_category": (job_dict.get("vendor_job_category") or "")[:128],
                "vendor_degree_level": (job_dict.get("vendor_degree_level") or "")[:128],
                "vendor_job_schedule": (job_dict.get("vendor_job_schedule") or "")[:128],
                "vendor_job_shift": (job_dict.get("vendor_job_shift") or "")[:128],
                "vendor_location_block": (job_dict.get("vendor_location_block") or "")[:512],
                "raw_payload": job_dict.get("raw_payload") or {},
                "list_payload_json": job_dict,
                "role_category": filter_result.category,
                "filter_decision": filter_result.decision if filter_enabled else None,
                "filter_reason": filter_result.reason if filter_enabled else None,
                "filter_snapshot_id": filter_result.snapshot_id if filter_enabled else None,
                "is_cold": bool(filter_blocks_pool),
                "jd_fetch_skipped": bool(should_skip_jd),
                "is_test_run": bool(is_test_run),
                "is_active": True,
                "content_hash": compute_content_hash(
                    label.company.pk,
                    job_dict.get("title") or "",
                    job_dict.get("location_raw") or "",
                ),
                **_company_snapshot_fields(label.company),
                **enriched,
            }

            def _archive_job_payload(raw_obj):
                try:
                    from .models import RawJobPayloadSnapshot
                    from .payload_archive import capture_rawjob_source_payloads
                    capture_rawjob_source_payloads(
                        raw_obj,
                        job_dict,
                        default_raw_html=desc_meta.get("raw_html") or "",
                        default_payload_kind=RawJobPayloadSnapshot.PayloadKind.API_RESPONSE,
                        default_source_url=original_url,
                        default_platform_slug=(label.platform.slug if label.platform else ""),
                        default_source_metadata={
                            "ingest": "fetch_company_jobs",
                            "external_id": external_id,
                            "label_id": label.pk,
                        },
                    )
                except Exception:
                    logger.exception("Failed to archive source payload for RawJob %s", url_hash)

            # ── Atomic identity decision ───────────────────────────────────────
            # Lock and evaluate all duplicate identities together so external_id,
            # content_hash, query-variant, and legacy-url hashes cannot disagree.
            from .services.rawjob_upsert import upsert_raw_job_with_dedupe

            upsert = upsert_raw_job_with_dedupe(
                company=label.company,
                defaults=defaults,
                url_hash=url_hash,
                original_url=original_url,
                external_id=external_id,
                platform_label=label,
                job_platform=label.platform,
                platform_slug=(label.platform.slug if label.platform else ""),
            )
            if upsert.action == "duplicate" or upsert.raw_job is None:
                jobs_duplicate += 1
                continue
            obj = upsert.raw_job
            created = upsert.created
            _archive_job_payload(obj)
            if filter_enabled:
                if filter_result.decision == STRONG:
                    filter_strong += 1
                elif filter_result.decision == POSSIBLE:
                    filter_possible += 1
                elif filter_result.decision == UNKNOWN:
                    filter_unknown += 1
                elif filter_result.decision == COLD:
                    filter_cold += 1
                elif filter_result.decision == NO_MATCH:
                    filter_no_match += 1
            if filter_enabled and filter_result.decision in {COLD, NO_MATCH}:
                try:
                    import random
                    from .models import HarvestSkippedTitle

                    sample_rate = max(0, min(100, int(getattr(_cfg, "cold_no_match_sample_rate_pct", 5) or 0)))
                    HarvestSkippedTitle.objects.create(
                        raw_job=obj,
                        company_name=label.company.name[:256],
                        platform_slug=(label.platform.slug if label.platform else "")[:64],
                        job_title=(job_dict.get("title") or "")[:512],
                        job_external_id=external_id[:256],
                        department=(job_dict.get("department") or "")[:256],
                        filter_decision=filter_result.decision,
                        filter_reason=filter_result.reason[:512],
                        matched_negative=(filter_result.matched_negative or "")[:256],
                        snapshot_id=filter_result.snapshot_id,
                        batch_id=batch.pk if batch else None,
                        is_sampled=bool(random.randint(1, 100) <= sample_rate) or filter_result.reason.startswith("UNKNOWN budget"),
                    )
                except Exception:
                    logger.exception("Failed to write HarvestSkippedTitle for RawJob %s", obj.pk)
            if created:
                jobs_new += 1
                if not filter_blocks_pool:
                    new_raw_job_pks.append(obj.pk)
            else:
                jobs_updated += 1

        except Exception as exc:
            jobs_failed += 1
            err_str = f"{type(exc).__name__}: {exc}"
            logger.error("RawJob upsert failed for label %s: %s", label_pk, err_str)
            if len(upsert_errors) < 5:
                upsert_errors.append(err_str[:300])

        if idx == 1 or idx % 5 == 0 or idx == total_jobs:
            run.jobs_new = jobs_new
            run.jobs_updated = jobs_updated
            run.jobs_duplicate = jobs_duplicate
            run.jobs_failed = jobs_failed
            run.save(update_fields=["jobs_new", "jobs_updated", "jobs_duplicate", "jobs_failed"])
            try:
                pct = 35 + int((idx / max(total_jobs, 1)) * 60)
                self.update_state(
                    state="PROGRESS",
                    meta={
                        "percent": min(95, max(35, pct)),
                        "message": f"Processing jobs… {idx}/{total_jobs}",
                        "jobs_found": total_jobs,
                        "jobs_new": jobs_new,
                        "jobs_updated": jobs_updated,
                        "jobs_duplicate": jobs_duplicate,
                        "jobs_failed": jobs_failed,
                        "jobs_pre_filtered": jobs_pre_filtered,
                    },
                )
            except Exception:
                pass

    # ── Compute field_presence from harvested jobs ────────────────────────────
    _fp: dict[str, int] = {
        "jd": 0, "requirements": 0, "responsibilities": 0,
        "department": 0, "geo": 0, "salary": 0,
        "employment_type": 0, "education": 0, "experience_level": 0,
        "category": 0, "schedule": 0,
    }
    for jd in raw_jobs:
        if jd.get("description") or jd.get("has_description"):
            _fp["jd"] += 1
        if jd.get("requirements"):
            _fp["requirements"] += 1
        if jd.get("responsibilities"):
            _fp["responsibilities"] += 1
        if jd.get("department"):
            _fp["department"] += 1
        if jd.get("city") or jd.get("country"):
            _fp["geo"] += 1
        if jd.get("salary_min") or jd.get("salary_max"):
            _fp["salary"] += 1
        if jd.get("employment_type") and jd.get("employment_type") not in ("UNKNOWN", ""):
            _fp["employment_type"] += 1
        if jd.get("education_required") and jd.get("education_required") not in ("UNKNOWN", ""):
            _fp["education"] += 1
        if jd.get("experience_level") and jd.get("experience_level") not in ("UNKNOWN", ""):
            _fp["experience_level"] += 1
        if jd.get("job_category") and jd.get("job_category") not in ("", "UNKNOWN"):
            _fp["category"] += 1
        if (jd.get("schedule_type") and jd.get("schedule_type") not in ("", "UNKNOWN")) or jd.get("vendor_job_schedule"):
            _fp["schedule"] += 1

    if filter_enabled and not effective_filter_audit_mode:
        try:
            tech_matched = filter_strong + filter_possible + filter_unknown
            if tech_matched == 0 and len(raw_jobs):
                CompanyPlatformLabel.objects.filter(pk=label.pk).update(
                    consecutive_zero_tech_fetches=F("consecutive_zero_tech_fetches") + 1
                )
                label.refresh_from_db(fields=["consecutive_zero_tech_fetches"])
                if label.consecutive_zero_tech_fetches >= int(getattr(_cfg, "zero_tech_threshold", 5) or 5):
                    CompanyPlatformLabel.objects.filter(pk=label.pk).update(
                        zero_tech_last_flagged_at=timezone.now()
                    )
            else:
                CompanyPlatformLabel.objects.filter(pk=label.pk).update(consecutive_zero_tech_fetches=0)
        except Exception:
            logger.exception("Failed to update selective harvest zero-tech counters for label %s", label.pk)

    # ── Update run record ─────────────────────────────────────────────────────
    _total_found = len(raw_jobs)
    if jobs_failed > 0:
        run.status = (
            CompanyFetchRun.Status.PARTIAL
            if (jobs_new + jobs_updated) > 0
            else CompanyFetchRun.Status.FAILED
        )
        if not run.issue_code:
            run.issue_code = CompanyFetchRun.IssueCode.PARSE_FAILED
    elif _total_found == 0:
        # Fetch succeeded (no errors) but returned zero jobs — silent empty, not a clean success.
        run.status = CompanyFetchRun.Status.EMPTY
        run.issue_code = CompanyFetchRun.IssueCode.NO_JOBS_RETURNED
    else:
        run.status = CompanyFetchRun.Status.SUCCESS

    run.jobs_found = _total_found
    run.jobs_new = jobs_new
    run.jobs_updated = jobs_updated
    run.jobs_duplicate = jobs_duplicate
    run.jobs_failed = jobs_failed
    run.field_presence = _fp
    run.jobs_cap_applied = cap_applied
    run.completed_at = timezone.now()
    if upsert_errors and not run.error_message:
        run.error_message = "Upsert errors: " + " | ".join(upsert_errors)
        run.error_type = CompanyFetchRun.ErrorType.PARSE_ERROR
    run.save(update_fields=[
        "status", "jobs_found", "jobs_total_available", "jobs_detail_fetched", "jobs_new", "jobs_updated",
        "jobs_duplicate", "jobs_failed", "completed_at", "error_message", "error_type",
        "issue_code", "field_presence", "jobs_cap_applied",
    ])

    # ── Update batch counters + auto-complete ────────────────────────────────
    _batch_just_finished = False
    if batch:
        if run.status in (
            CompanyFetchRun.Status.SUCCESS,
            CompanyFetchRun.Status.EMPTY,    # zero-yield still counts as completed, not failed
            CompanyFetchRun.Status.PARTIAL,
        ):
            FetchBatch.objects.filter(pk=batch.pk).update(
                completed_companies=models.F("completed_companies") + 1,
                total_jobs_found=models.F("total_jobs_found") + _total_found,
                total_jobs_new=models.F("total_jobs_new") + jobs_new,
            )
        else:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1,
            )

        # Auto-complete the batch when every child task has reported back.
        # Use a conditional UPDATE (WHERE status=RUNNING) so exactly ONE worker
        # wins the race — only that worker triggers the post-batch sync.
        refreshed = FetchBatch.objects.filter(pk=batch.pk).values(
            "total_companies", "completed_companies", "failed_companies"
        ).first()
        if refreshed:
            done = refreshed["completed_companies"] + refreshed["failed_companies"]
            total_co = refreshed["total_companies"]
            if total_co > 0 and done >= total_co:
                final_status = (
                    FetchBatch.Status.COMPLETED
                    if refreshed["failed_companies"] == 0
                    else FetchBatch.Status.PARTIAL
                )
                try:
                    status_rows = (
                        CompanyFetchRun.objects.filter(batch_id=batch.pk)
                        .values("status")
                        .annotate(c=Count("id"))
                    )
                    by_status = {row["status"]: row["c"] for row in status_rows}
                    plat_rows = list(
                        CompanyFetchRun.objects.filter(batch_id=batch.pk)
                        .values("label__platform__slug")
                        .annotate(
                            n=Count("id"),
                            ok=Count(
                                "id",
                                filter=Q(
                                    status__in=[
                                        CompanyFetchRun.Status.SUCCESS,
                                        CompanyFetchRun.Status.PARTIAL,
                                    ]
                                ),
                            ),
                            bad=Count(
                                "id",
                                filter=Q(
                                    status__in=[
                                        CompanyFetchRun.Status.FAILED,
                                        CompanyFetchRun.Status.SKIPPED,
                                    ]
                                ),
                            ),
                        )
                        .order_by("-n")[:30]
                    )
                    with transaction.atomic():
                        locked = FetchBatch.objects.select_for_update().get(pk=batch.pk)
                        if locked.status == FetchBatch.Status.RUNNING:
                            completion_audit = {
                                "phase": "completed",
                                "batch_status": final_status,
                                "total_companies": locked.total_companies,
                                "completed_companies": locked.completed_companies,
                                "failed_companies": locked.failed_companies,
                                "total_jobs_found": locked.total_jobs_found,
                                "total_jobs_new": locked.total_jobs_new,
                                "by_status": by_status,
                                "by_platform": plat_rows,
                                "logged_at": timezone.now().isoformat(),
                                "last_company_task_id": getattr(self.request, "id", "") or "",
                                "notes": (
                                    "failed_companies includes SKIPPED early exits (no tenant / no harvester). "
                                    "See batch detail page for per-company rows."
                                ),
                            }
                            merged = dict(locked.audit_payload or {})
                            merged["completion"] = completion_audit
                            locked.audit_payload = merged
                            locked.status = final_status
                            locked.completed_at = timezone.now()
                            locked.save(update_fields=["audit_payload", "status", "completed_at"])
                            _batch_just_finished = True
                            logger.info(
                                "[HARVEST_AUDIT done] run_kind=%s batch_id=%s batch_status=%s "
                                "total_companies=%s completed_counter=%s failed_counter=%s "
                                "total_jobs_found=%s total_jobs_new=%s by_status=%s by_platform=%s",
                                ((merged.get("queue") or {}).get("run_kind", "")),
                                batch.pk,
                                final_status,
                                locked.total_companies,
                                locked.completed_companies,
                                locked.failed_companies,
                                locked.total_jobs_found,
                                locked.total_jobs_new,
                                by_status,
                                plat_rows,
                            )
                except Exception as exc:
                    logger.warning("HARVEST_AUDIT completion persist/log failed: %s", exc)

    logger.info(
        "fetch_raw_jobs: label=%s new=%d updated=%d failed=%d pre_filtered=%d "
        "filter(strong=%d possible=%d cold=%d no_match=%d unknown=%d)",
        label_pk, jobs_new, jobs_updated, jobs_failed, jobs_pre_filtered,
        filter_strong, filter_possible, filter_cold, filter_no_match, filter_unknown,
    )

    # ── Inline pipeline: enrich only (safe — pure Python, zero HTTP) ────────────
    #
    # Enrich runs inline because it's pure Python regex/keyword extraction — ~1 ms/job,
    # no HTTP, no Playwright, no CPU spikes.
    #
    # JD backfill (Jarvis/Playwright/HTTP) does NOT run inline — it would spike CPU and
    # cause 502s on the app server.  It fires as a separate background task below,
    # scoped to THIS platform so it's focused and doesn't scan the whole DB.
    #
    # Gated by HarvestEngineConfig flags so either step can be disabled from the GUI.
    if new_raw_job_pks:
        try:
            from .models import RawJob
            from .enrichments import extract_enrichments

            _pipe_cfg = require_harvest_engine_config("fetch_raw_jobs_for_company_inline_pipeline")

            # Re-load only the jobs we just created
            new_jobs = list(
                RawJob.objects.filter(pk__in=new_raw_job_pks).select_related("company")
            )

            # ── Scope every new RawJob before any gated work ────────────────
            # This must run even when auto-enrichment is disabled; otherwise
            # new jobs remain UNSCOPED/is_priority=False and downstream gated
            # tasks will skip them.
            SCOPE_FIELDS = [
                "country_code", "country_confidence", "country_source",
                "country_codes", "location_candidates",
                "scope_status", "scope_reason", "is_priority",
                "last_scope_evaluated_at",
                "country", "state", "city", "location_raw",
            ]
            from .location_resolver import evaluate_rawjob_scope

            bulk_scope: list[RawJob] = []
            for job in new_jobs:
                scope_updates = evaluate_rawjob_scope(
                    job, cfg=_pipe_cfg, use_provider=None, save=False,
                )
                for field, value in scope_updates.items():
                    setattr(job, field, value)
                bulk_scope.append(job)
            if bulk_scope:
                RawJob.objects.bulk_update(bulk_scope, SCOPE_FIELDS)

            # ── Inline enrich (pure Python, ~1 ms/job, no HTTP) ─────────────
            enriched = 0
            if _pipe_cfg.auto_enrich and new_jobs:
                ENRICH_FIELDS = [
                    "skills", "tech_stack", "job_category",
                    "normalized_title", "title_keywords",
                    "years_required", "years_required_max", "education_required",
                    "visa_sponsorship", "work_authorization", "clearance_required", "clearance_level",
                    "salary_equity", "signing_bonus", "relocation_assistance",
                    "travel_required", "travel_pct_min", "travel_pct_max",
                    "schedule_type", "shift_schedule", "shift_details", "hours_hint", "weekend_required",
                    "certifications", "licenses_required", "benefits_list",
                    "languages_required", "encouraged_to_apply",
                    "job_keywords", "department_normalized",
                    "word_count", "quality_score", "jd_quality_score",
                    "classification_confidence", "classification_provenance",
                    "field_confidence", "field_provenance",
                    "resume_ready_score", "description_clean", "description_raw_html",
                    "has_html_content", "cleaning_version",
                    # Section extraction — populated by extract_enrichments if not set by harvester
                    "requirements", "responsibilities",
                    # Domain taxonomy
                    "job_domain", "domain_version",
                ]
                bulk_enrich: list[RawJob] = []
                for job in new_jobs:
                    # Enrich only PRIORITY jobs. Cold/unknown stay as cheap discovery rows.
                    if not job.is_priority:
                        continue
                    if job.skills or job.job_category:
                        continue  # already enriched
                    enriched_data = extract_enrichments(build_enrichment_input(job))
                    for f in ENRICH_FIELDS:
                        if f in enriched_data:
                            setattr(job, f, enriched_data[f])
                    bulk_enrich.append(job)
                    enriched += 1
                if bulk_enrich:
                    RawJob.objects.bulk_update(bulk_enrich, ENRICH_FIELDS)

            logger.info(
                "Inline enrich done: label=%s new_jobs=%d enriched=%d",
                label_pk, len(new_jobs), enriched,
            )

            # ── Background JD backfill — scoped to this platform, fires once ─
            # This queues a SINGLE background task for the platform just harvested.
            # The backfill task controls its own parallelism and rate limits so it
            # never spikes CPU.  Only queued if there are new jobs without descriptions.
            platform_s = (label.platform.slug if label and label.platform else "") or ""
            if _pipe_cfg.auto_backfill_jd:
                needs_jd_count = sum(1 for j in new_jobs if j.is_priority and not (j.description or "").strip())
                if needs_jd_count > 0:
                    backfill_descriptions_task.apply_async(
                        kwargs={
                            "batch_size": min(needs_jd_count + 10, 100),  # focused batch
                            "parallel_workers": 1,  # ONE worker — never spike CPU
                            "platform_slug": platform_s or None,
                        },
                        countdown=15,  # 15 s after harvest — DB writes settle first
                        queue="harvest",  # dedicated queue, doesn't compete with app
                    )
                    logger.info(
                        "JD backfill queued for %d new %s jobs (bg task, 1 worker)",
                        needs_jd_count, platform_s or "all",
                    )

        except SoftTimeLimitExceeded:
            logger.warning("Inline pipeline aborted at soft time limit for label %s", label_pk)
        except Exception as exc:
            logger.warning("Inline pipeline failed for label %s: %s", label_pk, exc)

    # ── Post-batch: auto-sync to pool once the whole batch is done ───────────
    # Sync is the only step that still runs as a separate background task.
    # It touches the main Job model, involves dedup checks, and only needs to run
    # once after the whole batch — not per-company.  A short countdown (60 s)
    # after the batch closes is more than enough for all inline enrichment to settle.
    if _batch_just_finished:
        try:
            _sync_cfg = require_harvest_engine_config("fetch_raw_jobs_for_company_post_batch")
            # First run link-health validation to flip soft-404 rows inactive
            # before promotion into Vet Queue.
            validate_raw_job_urls_task.apply_async(
                kwargs={
                    "batch_size": 100,
                    "concurrency": HARVEST_SAFE_VALIDATE_CONCURRENCY,
                    "max_jobs": HARVEST_SAFE_VALIDATE_MAX_JOBS,
                    "pending_only": True,
                    "recent_hours": 96,
                },
                countdown=15,
            )
            logger.info("Auto URL validation queued before sync (batch #%s)", batch.pk)
            if _sync_cfg.auto_sync_to_pool:
                sync_harvested_to_pool_task.apply_async(
                    kwargs={"max_jobs": HARVEST_SAFE_SYNC_MAX_JOBS},
                    countdown=90,  # wait for URL validation pass first
                )
                logger.info("Auto-sync queued 90 s after batch #%s completion", batch.pk)
            duplicate_limit = min(
                HARVEST_SAFE_DUPLICATE_LIMIT,
                max(200, int(getattr(batch, "total_jobs_new", 0) or 0)),
            )
            run_duplicate_detection_task.apply_async(
                kwargs={"limit": duplicate_limit},
                countdown=120,
                queue="harvest",
            )
            logger.info(
                "Duplicate-pair detection queued 120 s after batch #%s (limit=%s)",
                batch.pk,
                duplicate_limit,
            )
        except Exception as exc:
            logger.warning("Auto-sync queue failed: %s", exc)

    _invalidate_rawjobs_dashboard_cache()

    return {
        "label_pk": label_pk,
        "run_id": run.pk,
        "jobs_found": len(raw_jobs),
        "jobs_new": jobs_new,
        "jobs_updated": jobs_updated,
        "jobs_duplicate": jobs_duplicate,
        "jobs_failed": jobs_failed,
        "filter": {
            "strong": filter_strong,
            "possible": filter_possible,
            "unknown": filter_unknown,
            "cold": filter_cold,
            "no_match": filter_no_match,
            "audit_mode": filter_audit_mode,
            "effective_audit_mode": effective_filter_audit_mode,
            "fetch_all_bypass": bool(fetch_all),
            "snapshot_id": filter_snapshot_id,
        },
    }


@shared_task(bind=True, name="harvest.fetch_raw_jobs_batch", max_retries=0)
def fetch_raw_jobs_batch_task(
    self,
    platform_slug: str = None,
    label_pks: list = None,
    batch_name: str = None,
    triggered_user_id: int = None,
    test_mode: bool = False,
    test_max_jobs: int = 10,
    companies_per_platform: int = 1,
    skip_platforms: list = None,
    min_hours_since_fetch: int = 6,
    fetch_all: bool = False,
    run_kind: str | None = None,
):
    """
    Create a FetchBatch and dispatch fetch_raw_jobs_for_company_task for every matching label.

    run_kind — explicit audit label for logs/UI: quick_sync | full_crawl_all |
        full_crawl_platform | platform_smoke (inferred from fetch_all/test_mode when omitted).

    test_mode=True — picks up to `companies_per_platform` companies per platform,
    passes max_jobs=test_max_jobs (no full pagination). Useful for smoke-testing.
    skip_platforms — list of platform slugs to exclude (e.g. ["greenhouse","lever"]).
    min_hours_since_fetch — skip labels that were successfully fetched within this many
    hours. Pass None to read from HarvestEngineConfig (default). Pass 0 to force re-fetch.
    """
    from django.contrib.auth import get_user_model
    from .models import CompanyPlatformLabel, CompanyFetchRun, FetchBatch

    # ── Read live engine config — required for harvest correctness ───────────
    _ecfg = require_harvest_engine_config("fetch_raw_jobs_batch_task")
    # Caller-supplied min_hours_since_fetch overrides DB only when explicitly passed.
    # Default argument sentinel is 6; if it still equals 6, prefer the DB value.
    if min_hours_since_fetch == 6:
        min_hours_since_fetch = _ecfg.min_hours_since_fetch
    _api_stagger = max(_ecfg.api_stagger_ms, HARVEST_SAFE_API_STAGGER_MS) / 1000.0
    _scraper_stagger = max(_ecfg.scraper_stagger_ms, HARVEST_SAFE_SCRAPER_STAGGER_MS) / 1000.0

    User = get_user_model()
    triggered_user = None
    if triggered_user_id:
        triggered_user = User.objects.filter(pk=triggered_user_id).first()

    # Build batch name
    if not batch_name:
        ts = timezone.now().strftime("%Y-%m-%d %H:%M")
        skipped = ", ".join(skip_platforms or [])
        if test_mode:
            skip_str = f" | skip: {skipped}" if skipped else ""
            batch_name = f"PLATFORM CHECK — {companies_per_platform} co/platform, {test_max_jobs} jobs{skip_str} — {ts}"
        elif platform_slug:
            batch_name = f"{platform_slug.title()} batch — {ts}"
        else:
            batch_name = f"Full batch — {ts}"

    batch = FetchBatch.objects.create(
        created_by=triggered_user,
        name=batch_name,
        status=FetchBatch.Status.RUNNING,
        platform_filter=platform_slug or "",
        task_id=self.request.id or "",
        started_at=timezone.now(),
        is_full_crawl=bool(fetch_all) and not test_mode,
    )
    filter_snapshot_id = None
    if _ecfg.selective_filter_enabled:
        try:
            from .models import HarvestFilterSnapshot

            snapshot = HarvestFilterSnapshot.create_snapshot(batch=batch)
            filter_snapshot_id = str(snapshot.snapshot_id)
        except Exception:
            logger.exception("Failed to create selective filter snapshot; batch will run without selective filtering.")

    # ── Build label queryset ──────────────────────────────────────────────────
    # Include portal_alive=True (confirmed up) AND portal_alive=None (never checked).
    # Exclude portal_alive=False (confirmed down — no point hammering dead portals).
    # Only include companies whose platform is enabled — respect is_enabled flag.
    qs = CompanyPlatformLabel.objects.filter(
        portal_alive__in=[True, None],
        platform__isnull=False,
        platform__is_enabled=True,
    ).exclude(tenant_id="").select_related("platform", "company").order_by("company__name")

    if _ecfg.selective_filter_enabled and not fetch_all and not test_mode:
        qs = qs.exclude(skip_in_selective_harvest=True)

    if platform_slug:
        qs = qs.filter(platform__slug=platform_slug)

    if label_pks:
        qs = qs.filter(pk__in=label_pks)

    if skip_platforms:
        qs = qs.exclude(platform__slug__in=skip_platforms)

    # ── Build skip-if-fresh set ───────────────────────────────────────────────
    # Labels with a successful/partial run completed within min_hours_since_fetch
    # are skipped — no point re-fetching the same jobs minutes/hours later.
    fresh_label_pks: set[int] = set()
    if min_hours_since_fetch > 0 and not test_mode:
        fresh_cutoff = timezone.now() - timedelta(hours=min_hours_since_fetch)
        fresh_label_pks = set(
            CompanyFetchRun.objects.filter(
                status__in=[CompanyFetchRun.Status.SUCCESS, CompanyFetchRun.Status.PARTIAL],
                completed_at__gte=fresh_cutoff,
            ).values_list("label_id", flat=True)
        )
        if fresh_label_pks:
            logger.info(
                "fetch_raw_jobs_batch: skipping %d labels fetched within last %dh",
                len(fresh_label_pks), min_hours_since_fetch,
            )

    all_pks: list[int] = []
    skipped_fresh = 0
    if test_mode:
        # Pick up to `companies_per_platform` companies per platform slug
        per_plat = max(1, companies_per_platform)
        seen_platforms: dict[str, int] = {}  # slug -> count
        label_list = []
        for label in qs.iterator():
            slug = label.platform.slug if label.platform else ""
            if not slug:
                continue
            count = seen_platforms.get(slug, 0)
            if count < per_plat:
                seen_platforms[slug] = count + 1
                label_list.append(label.pk)
        logger.info(
            "fetch_raw_jobs_batch TEST MODE: %d platforms, %d companies selected (%d per platform)",
            len(seen_platforms), len(label_list), per_plat,
        )
    else:
        all_pks = list(qs.values_list("pk", flat=True))
        label_list = [pk for pk in all_pks if pk not in fresh_label_pks]
        skipped_fresh = len(all_pks) - len(label_list)
        if skipped_fresh:
            logger.info(
                "fetch_raw_jobs_batch: %d/%d labels skipped (fresh <%dh), %d queued",
                skipped_fresh, len(all_pks), min_hours_since_fetch, len(label_list),
            )

    total = len(label_list)

    rk = _resolve_harvest_run_kind(
        run_kind=run_kind,
        test_mode=test_mode,
        fetch_all=fetch_all,
        platform_slug=platform_slug,
    )
    queue_audit = {
        "phase": "queued",
        "run_kind": rk,
        "fetch_all": bool(fetch_all),
        "platform_filter": platform_slug or "",
        "test_mode": bool(test_mode),
        "eligible_labels": len(all_pks) if not test_mode else None,
        "skipped_fresh": int(skipped_fresh) if not test_mode else None,
        "skipped_fresh_explanation": (
            None
            if test_mode
            else "NOT queued: had SUCCESS/PARTIAL CompanyFetchRun within min_hours_since_successful_fetch."
        ),
        "queued_companies": total,
        "min_hours_since_successful_fetch": min_hours_since_fetch,
        "selective_filter_enabled": bool(_ecfg.selective_filter_enabled),
        "filter_audit_mode": bool(_ecfg.filter_audit_mode),
        "filter_snapshot_id": filter_snapshot_id,
        "incremental_since_hours_for_api_boards": 25,
        "what_quick_fetch_does_not_do": (
            "Does not bypass fresh skip; does not crawl disabled/dead labels; "
            "HTML scraper platforms still fetch full board (no date filter)."
            if rk == "quick_sync"
            else None
        ),
        "orchestrator_task_id": self.request.id or "",
        "logged_at": timezone.now().isoformat(),
    }
    batch.total_companies = total
    batch.is_full_crawl = bool(fetch_all) and not test_mode
    batch.audit_payload = {"queue": queue_audit}
    batch.save(update_fields=["total_companies", "is_full_crawl", "audit_payload"])

    logger.info(
        "[HARVEST_AUDIT queue] run_kind=%s batch_id=%s fetch_all=%s eligible=%s skipped_fresh=%s queued=%s orch_task=%s",
        rk,
        batch.pk,
        fetch_all,
        queue_audit["eligible_labels"],
        queue_audit["skipped_fresh"],
        total,
        self.request.id or "",
    )

    update_task_progress(self, current=0, total=total, message=f"Dispatching {total} company fetches…")

    # Stagger by platform type: API platforms get a tighter stagger (0.1s),
    # HTML scrapers get a wider one (1.0s) to avoid hammering slow targets.
    # adp uses HTMLScrapeHarvester → needs the slower 1.5s scraper stagger
    SCRAPER_SLUGS = {"jobvite", "icims", "taleo", "ultipro", "applicantpro",
                     "applytojob", "theapplicantmanager", "zoho", "breezy", "teamtailor", "adp"}

    # Fetch label→platform slug mapping once to decide stagger
    label_platform_map: dict[int, str] = {}
    if label_list:
        for row in CompanyPlatformLabel.objects.filter(pk__in=label_list).values("pk", "platform__slug"):
            label_platform_map[row["pk"]] = row["platform__slug"] or ""

    api_offset = 0
    scraper_offset = 0
    for label_pk in label_list:
        slug = label_platform_map.get(label_pk, "")
        is_scraper = slug in SCRAPER_SLUGS
        if is_scraper:
            countdown = scraper_offset
            scraper_offset += _scraper_stagger   # from HarvestEngineConfig (default 1.5s)
        else:
            countdown = api_offset
            api_offset += _api_stagger           # from HarvestEngineConfig (default 0.1s)

        kwargs = {"max_jobs": test_max_jobs} if test_mode else {}
        if fetch_all and not test_mode:
            kwargs["fetch_all"] = True   # pass full-crawl flag to child tasks
        if filter_snapshot_id:
            kwargs["filter_snapshot_id"] = filter_snapshot_id
        fetch_raw_jobs_for_company_task.apply_async(
            args=[label_pk, batch.pk, "BATCH"],
            kwargs=kwargs,
            countdown=countdown,
        )

    if label_list and label_list[0] % 50 == 0:
        pass  # progress update already at end
    update_task_progress(self, current=total, total=total,
                         message=f"All {total} fetches queued for batch #{batch.pk}")

    logger.info("fetch_raw_jobs_batch: queued %d companies (batch #%d, test=%s)", total, batch.pk, test_mode)
    return {"batch_id": batch.pk, "total_companies": total, "test_mode": test_mode}


@shared_task(bind=True, name="harvest.resume_fetch_batch", max_retries=0)
def resume_fetch_batch_task(self, batch_id: int):
    """
    Resume a PARTIAL/RUNNING FetchBatch that was interrupted mid-run.

    Finds every company in the batch that does NOT have a finished
    CompanyFetchRun (SUCCESS/PARTIAL/FAILED/EMPTY) and re-dispatches
    only those — acting as a checkpoint/resume instead of starting over.

    Safe to call multiple times — already-completed companies are skipped.
    """
    from .models import CompanyFetchRun, CompanyPlatformLabel, FetchBatch

    try:
        batch = FetchBatch.objects.get(pk=batch_id)
    except FetchBatch.DoesNotExist:
        logger.error("resume_fetch_batch: batch #%s not found", batch_id)
        return {"error": "batch not found"}

    # IDs of labels that already have a finished run in this batch
    finished_label_ids = set(
        CompanyFetchRun.objects.filter(
            batch=batch,
            status__in=[
                CompanyFetchRun.Status.SUCCESS,
                CompanyFetchRun.Status.PARTIAL,
                CompanyFetchRun.Status.FAILED,
                CompanyFetchRun.Status.EMPTY,
            ],
        ).values_list("label_id", flat=True)
    )

    # All labels that were supposed to be in this batch (same criteria as original)
    qs = CompanyPlatformLabel.objects.filter(
        portal_alive__in=[True, None],
        platform__isnull=False,
        platform__is_enabled=True,
    ).exclude(tenant_id="").select_related("platform", "company")

    if batch.platform_filter:
        qs = qs.filter(platform__slug=batch.platform_filter)

    # Labels still needing a fetch = all eligible MINUS already finished
    pending_pks = [
        pk for pk in qs.values_list("pk", flat=True)
        if pk not in finished_label_ids
    ]

    if not pending_pks:
        batch.status = FetchBatch.Status.COMPLETED
        batch.completed_at = timezone.now()
        batch.save(update_fields=["status", "completed_at"])
        logger.info("resume_fetch_batch: batch #%s already complete", batch_id)
        return {"batch_id": batch_id, "resumed": 0, "message": "Already complete"}

    # Put batch back to RUNNING and update total to reflect reality
    batch.status = FetchBatch.Status.RUNNING
    batch.total_companies = batch.completed_companies + batch.failed_companies + len(pending_pks)
    batch.completed_at = None
    batch.save(update_fields=["status", "total_companies", "completed_at"])

    _ecfg = require_harvest_engine_config("resume_fetch_batch_task")
    _api_stagger = _ecfg.api_stagger_ms / 1000.0
    _scraper_stagger = _ecfg.scraper_stagger_ms / 1000.0

    for i, label_pk in enumerate(pending_pks):
        label = CompanyPlatformLabel.objects.filter(pk=label_pk).select_related("platform", "company").first()
        if not label:
            continue
        is_scraper = getattr(label.platform, "is_scraper", False) if label.platform else False
        stagger = _scraper_stagger if is_scraper else _api_stagger
        countdown = int(i * stagger)
        fetch_raw_jobs_for_company_task.apply_async(
            kwargs={
                "label_pk": label_pk,
                "batch_id": batch_id,
                "fetch_all": True,
            },
            countdown=countdown,
            queue="batches",
        )

    logger.info(
        "resume_fetch_batch: batch #%s resumed — %d/%d companies re-queued (%d already done)",
        batch_id, len(pending_pks),
        batch.total_companies, len(finished_label_ids),
    )
    update_task_progress(
        self, current=len(pending_pks), total=len(pending_pks),
        message=f"Resumed batch #{batch_id}: {len(pending_pks)} companies re-queued, {len(finished_label_ids)} already done",
    )
    return {
        "batch_id": batch_id,
        "resumed": len(pending_pks),
        "already_done": len(finished_label_ids),
    }


@shared_task(bind=True, name="harvest.retry_failed_raw_jobs")
def retry_failed_raw_jobs_task(self):
    """Re-queue fetch_raw_jobs_for_company_task for all FAILED runs.

    Look-back window is configurable via HarvestEngineConfig.retry_failed_days
    (default 7 days). Increase to recover from longer outages.
    """
    from .models import CompanyFetchRun, HarvestEngineConfig, HarvestOpsRun
    from .ops_audit import begin_ops_run, finish_ops_run

    days = HarvestEngineConfig.get().retry_failed_days
    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.RETRY_FAILED,
        getattr(self.request, "id", "") or "",
        queue={"retry_failed_days": days},
    )
    try:
        cutoff = timezone.now() - timedelta(days=days)
        failed_runs = CompanyFetchRun.objects.filter(
            status=CompanyFetchRun.Status.FAILED,
            started_at__gte=cutoff,
        ).select_related("label")

        queued = 0
        for run in failed_runs:
            fetch_raw_jobs_for_company_task.delay(
                run.label_id,
                run.batch_id,
                "SCHEDULED",
            )
            queued += 1

        logger.info("retry_failed_raw_jobs: re-queued %d tasks", queued)
        finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS,
                       {"queued": queued, "lookback_days": days})
        return {"queued": queued}
    except Exception as e:
        finish_ops_run(ops_run, HarvestOpsRun.Status.FAILED, {"error": str(e)[:300]})
        raise


@shared_task(
    bind=True,
    name="harvest.validate_raw_job_urls",
    soft_time_limit=7200,   # 2 h — enough for 126k URLs at 20 threads
    time_limit=7500,
    max_retries=0,
)
def validate_raw_job_urls_task(
    self,
    platform_slug: str | None = None,
    batch_size: int = 200,
    concurrency: int = 20,
    max_jobs: int | None = None,
    pending_only: bool = False,
    recent_hours: int | None = None,
):
    """
    Validate raw job URLs with multi-signal liveness detection and mark inactive
    only on *definitive* closed signals.

    Runs after every FETCH ALL batch (or on a schedule) to surface broken links
    before a human ever sees them. Results are visible in the Jobs Browser
    (SYNC column stays PENDING; is_active=False jobs are hidden from candidates).

    Uses a thread pool for concurrency — HEAD requests are I/O bound so
    parallelism is safe and fast.

    platform_slug — limit to one platform (e.g. "workday")
    batch_size    — DB fetch chunk size (memory control)
    concurrency   — parallel HTTP threads
    max_jobs      — cap total checked (for quick spot-checks)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .models import HarvestOpsRun, RawJob
    from .ops_audit import begin_ops_run, finish_ops_run
    from .url_health import check_job_posting_live, is_definitive_inactive

    task_id = getattr(self.request, "id", "") or ""
    singleton_queue = {
        "platform_slug": platform_slug or "",
        "batch_size": batch_size,
        "concurrency": concurrency,
        "max_jobs": max_jobs,
        "pending_only": pending_only,
        "recent_hours": recent_hours,
    }
    singleton_lock = _acquire_ops_singleton(
        HarvestOpsRun.Operation.VALIDATE_URLS,
        task_id,
        queue=singleton_queue,
        stale_minutes=45,
        cache_ttl_seconds=3 * 60 * 60,
    )
    if singleton_lock is None:
        return {"message": "Skipped — another validate-live-links run is active.", "checked": 0}

    qs = RawJob.objects.filter(is_active=True).exclude(original_url="")
    if platform_slug:
        qs = qs.filter(platform_slug=platform_slug)
    if pending_only:
        qs = qs.filter(sync_status=RawJob.SyncStatus.PENDING)
    if recent_hours and recent_hours > 0:
        qs = qs.filter(fetched_at__gte=timezone.now() - timedelta(hours=int(recent_hours)))
    if max_jobs:
        qs = qs[:max_jobs]

    total = qs.count()
    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.VALIDATE_URLS,
        task_id,
        progress_total=total,
        queue={
            "platform_slug": platform_slug or "",
            "batch_size": batch_size,
            "concurrency": concurrency,
            "max_jobs": max_jobs,
            "pending_only": pending_only,
            "recent_hours": recent_hours,
            "urls_planned": total,
        },
    )

    checked = alive = dead = inconclusive = errors = 0
    reason_counts: dict[str, int] = {}

    try:
        update_task_progress(self, current=0, total=total, message=f"Checking {total:,} URLs…")
        tick_ops_run_progress(ops_run, 0, total, f"Checking {total:,} URLs…", force=True)

        def check_url(job_id: int, url: str, slug: str) -> tuple[int, object]:
            """Returns (job_id, LinkHealthResult)."""
            try:
                result = check_job_posting_live(url, platform_slug=slug or "")
                return job_id, result
            except Exception:
                return job_id, check_job_posting_live("", platform_slug=slug or "")  # missing_url (non-fatal)

        offset = 0
        while True:
            chunk = list(qs.values("id", "original_url", "platform_slug")[offset: offset + batch_size])
            if not chunk:
                break
            if max_jobs and offset >= max_jobs:
                break

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(check_url, row["id"], row["original_url"], row.get("platform_slug") or platform_slug or ""): row["id"]
                    for row in chunk
                }
                dead_ids = []
                alive_ids = []
                inactive_reasons: dict[int, str] = {}
                for future in as_completed(futures):
                    job_id, result = future.result()
                    checked += 1
                    reason = (getattr(result, "reason", "") or "unknown").strip()[:120]
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    if bool(result.is_live):
                        alive += 1
                        alive_ids.append(job_id)
                    else:
                        if is_definitive_inactive(result):
                            dead += 1
                            dead_ids.append(job_id)
                            inactive_reasons[job_id] = reason
                        else:
                            inconclusive += 1

                # Mark definitively closed jobs inactive in one batch update.
                if dead_ids:
                    now = timezone.now()
                    RawJob.objects.filter(pk__in=dead_ids).update(is_active=False)
                    # Store reason metadata for auditability on detail page.
                    for raw in RawJob.objects.filter(pk__in=dead_ids).only("id", "raw_payload"):
                        payload = dict(raw.raw_payload or {})
                        payload["link_health"] = {
                            "is_live": False,
                            "reason": inactive_reasons.get(raw.id, "inactive"),
                            "checked_at": now.isoformat(),
                            "decisive": True,
                        }
                        raw.raw_payload = payload
                        raw.save(update_fields=["raw_payload", "updated_at"])

                    # Propagate to linked Job records — no second HTTP call needed.
                    try:
                        from jobs.models import Job
                        from jobs.notify import notify_job_posting_link_unhealthy
                        linked_jobs = list(
                            Job.objects.filter(
                                source_raw_job_id__in=dead_ids,
                                status__in=[Job.Status.OPEN, Job.Status.POOL],
                                is_archived=False,
                            ).only("id", "status", "possibly_filled", "original_link")
                        )
                        for job in linked_jobs:
                            was_pf = job.possibly_filled
                            job.original_link_is_live = False
                            job.original_link_last_checked_at = now
                            job.possibly_filled = job.status == Job.Status.OPEN
                            job.save(update_fields=[
                                "original_link_is_live", "original_link_last_checked_at", "possibly_filled"
                            ])
                            if job.possibly_filled and not was_pf:
                                try:
                                    notify_job_posting_link_unhealthy(job)
                                except Exception:
                                    pass
                    except Exception:
                        logger.warning("validate_raw_job_urls: failed to propagate dead status to Jobs", exc_info=True)

                # Stamp last_checked_at on linked Jobs whose URL is still live.
                if alive_ids:
                    try:
                        from jobs.models import Job
                        Job.objects.filter(
                            source_raw_job_id__in=alive_ids,
                            status__in=[Job.Status.OPEN, Job.Status.POOL],
                            is_archived=False,
                        ).update(original_link_is_live=True, original_link_last_checked_at=timezone.now())
                    except Exception:
                        logger.warning("validate_raw_job_urls: failed to stamp live status on Jobs", exc_info=True)

            offset += batch_size
            _val_msg = f"Checked {checked:,}/{total:,} — {alive:,} live, {dead:,} inactive, {inconclusive:,} inconclusive"
            update_task_progress(
                self, current=checked, total=total,
                message=_val_msg,
            )
            tick_ops_run_progress(ops_run, checked, total, _val_msg)

        logger.info(
            "validate_raw_job_urls: checked=%d live=%d inactive=%d inconclusive=%d errors=%d reasons=%s",
            checked, alive, dead, inconclusive, errors, reason_counts,
        )
        out = {
            "checked": checked,
            "live": alive,
            "inactive": dead,
            "inconclusive": inconclusive,
            "reason_counts": reason_counts,
        }
        finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, out)
        _release_ops_singleton(singleton_lock, task_id)
        return out

    except SoftTimeLimitExceeded:
        # 2-hour window exceeded — record what we finished, not a FAILED
        logger.warning(
            "validate_raw_job_urls_task soft time limit — checked=%d/%d live=%d inactive=%d",
            checked, total, alive, dead,
        )
        finish_ops_run(
            ops_run,
            HarvestOpsRun.Status.PARTIAL,
            {
                "note": "soft_time_limit",
                "checked": checked,
                "total": total,
                "live": alive,
                "inactive": dead,
                "inconclusive": inconclusive,
                "reason_counts": reason_counts,
            },
        )
        _release_ops_singleton(singleton_lock, task_id)
        return {"checked": checked, "live": alive, "inactive": dead, "soft_time_limit": True}

    except Exception as e:
        logger.exception("validate_raw_job_urls_task failed: %s", e)
        finish_ops_run(
            ops_run,
            HarvestOpsRun.Status.FAILED,
            {
                "error": str(e)[:500],
                "checked": checked,
                "live": alive,
                "inactive": dead,
                "inconclusive": inconclusive,
                "reason_counts": reason_counts,
            },
        )
        _release_ops_singleton(singleton_lock, task_id)
        raise


@shared_task(bind=True, name="harvest.cleanup_harvested_jobs")
def cleanup_harvested_jobs_task(self):
    """Phase 5: clean expired RawJob rows (HarvestedJob/HarvestRun removed).

    All thresholds are read from HarvestEngineConfig so they're tunable from the
    GUI without a code deploy:
      cleanup_inactive_age_days     — Phase 3 age threshold (default 7 days)
      cleanup_pending_safe_minutes  — Phase 2 race guard (default 10 min)
    """
    from .models import HarvestEngineConfig, HarvestOpsRun, RawJob
    from .ops_audit import begin_ops_run, finish_ops_run

    cfg = HarvestEngineConfig.get()
    inactive_age_days = cfg.cleanup_inactive_age_days
    pending_safe_minutes = cfg.cleanup_pending_safe_minutes

    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.CLEANUP,
        getattr(self.request, "id", "") or "",
        queue={
            "inactive_age_days": inactive_age_days,
            "pending_safe_minutes": pending_safe_minutes,
        },
    )
    try:
        now = timezone.now()

        # Phase 1: mark expired PENDING/SKIPPED rows as inactive
        deactivated = RawJob.objects.filter(
            expires_at__lt=now,
            sync_status__in=["PENDING", "SKIPPED"],
            is_active=True,
        ).update(is_active=False)

        # Phase 2: purge is_active=False + PENDING rows (never synced, confirmed
        # dead). A safe buffer guards against a race with in-flight sync tasks —
        # only delete rows that have been inactive for at least pending_safe_minutes.
        pending_safe_cutoff = now - timedelta(minutes=pending_safe_minutes)
        purged_pending = RawJob.objects.filter(
            is_active=False,
            sync_status=RawJob.SyncStatus.PENDING,
            fetched_at__lt=pending_safe_cutoff,
        ).delete()[0]

        # Phase 3: purge remaining inactive rows older than configured threshold
        # (SKIPPED/FAILED rows where the job posting is gone)
        old_cutoff = now - timedelta(days=inactive_age_days)
        purged_old = RawJob.objects.filter(
            is_active=False,
            fetched_at__lt=old_cutoff,
        ).delete()[0]

        purged = purged_pending + purged_old
        logger.info(
            "Cleanup: %d deactivated, %d pending-dead purged, %d old purged.",
            deactivated, purged_pending, purged_old,
        )
        out = {"deactivated": deactivated, "purged_pending": purged_pending,
               "purged_old": purged_old, "purged": purged}
        finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, out)
        return out
    except Exception as e:
        logger.exception("cleanup_harvested_jobs_task failed: %s", e)
        finish_ops_run(ops_run, HarvestOpsRun.Status.FAILED, {"error": str(e)[:500]})
        raise



def _reconcile_synced_job_locations(cap: int = 500) -> dict:
    """Propagate post-sync country corrections from RawJob → linked pool Job.

    Only touches Job.country, only when the RawJob has a non-empty country that
    differs from the Job's, capped per run. Gated by the 'job_location_sync'
    feature flag (missing = ON — it is a bounded data correction; disable the
    flag in the UI if a manual country edit must be preserved).
    """
    try:
        from core.models import FeatureFlag
        ff = FeatureFlag.objects.filter(key="job_location_sync").first()
        if ff is not None and not ff.is_enabled:
            return {"enabled": False, "updated": 0}
    except Exception:
        pass

    from django.db.models import F
    from jobs.models import Job

    qs = (
        Job.objects.filter(source_raw_job__isnull=False)
        .exclude(source_raw_job__country="")
        .exclude(source_raw_job__country__isnull=True)
        .exclude(country=F("source_raw_job__country"))
        .select_related("source_raw_job")[:cap]
    )
    updated = 0
    for job in qs:
        new_country = (job.source_raw_job.country or "")[:100]
        if new_country and job.country != new_country:
            job.country = new_country
            job.save(update_fields=["country"])
            updated += 1
    if updated:
        logger.info("location reconcile: propagated country to %d pool jobs", updated)
    return {"enabled": True, "updated": updated, "cap": cap}


@shared_task(bind=True, name="harvest.sync_harvested_to_pool")
def sync_harvested_to_pool_task(
    self,
    max_jobs: int = 100,
    qualified_only: bool = False,
    chunk_size: int = 500,
):
    """
    Promote RawJobs to the Job pool.

    When ``qualified_only=True`` this task scans the full pending backlog and only
    picks rows that already look vet-eligible, so a manual "Sync Qualified" run
    moves meaningful jobs quickly across all pages instead of sampling just the
    newest pending rows.
    """
    from .models import HarvestOpsRun, RawJob
    from .ops_audit import begin_ops_run, finish_ops_run
    from jobs.models import Job, PipelineEvent
    from jobs.dedup import find_existing_job_by_url
    from jobs.quality import compute_quality_score
    from jobs.gating import apply_gate_result_to_job, evaluate_raw_job_gate
    from django.contrib.auth import get_user_model
    from django.utils import timezone as _tz

    User = get_user_model()
    system_user = User.objects.filter(is_superuser=True).first()
    if not system_user:
        logger.error("No superuser found for sync task.")
        ops_run = begin_ops_run(
            HarvestOpsRun.Operation.SYNC_POOL,
            getattr(self.request, "id", "") or "",
            queue={"precheck_failed": True, "reason": "no_superuser"},
        )
        finish_ops_run(ops_run, HarvestOpsRun.Status.FAILED, {"error": "No superuser found"})
        return {"synced": 0}

    max_jobs = int(max_jobs or 0)
    if max_jobs < 0:
        max_jobs = 0

    # Load VetGateConfig singleton — all sync gate settings come from here.
    from .models import VetGateConfig as _VetGateCfg
    gate_cfg = _VetGateCfg.get()

    chunk_size = max(50, min(int(chunk_size or gate_cfg.default_chunk_size), 2000))

    # Scoped harvest gate: only PRIORITY (target-country) or REVIEW_UNKNOWN_COUNTRY
    # jobs sync to the Vet Queue. Belt-and-suspenders: both is_priority and
    # scope_status are checked so a stale flag on either field can't open the gate.
    # Cold + UNSCOPED jobs stay in RawJob until the country resolver upgrades them
    # (or until target_countries config expands).
    scope_statuses = [RawJob.ScopeStatus.PRIORITY_TARGET]
    if gate_cfg.allow_unknown_country:
        scope_statuses.append(RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)

    base_qs = (
        RawJob.objects.filter(
            sync_status="PENDING",
            is_active=True,
            company__isnull=False,
            is_priority=True,
            scope_status__in=scope_statuses,
        )
        .exclude(original_url="")
        .exclude(Q(is_cold=True) | Q(filter_decision="NO_MATCH") | Q(jd_fetch_skipped=True))
    )

    if not gate_cfg.allow_possible_filter:
        base_qs = base_qs.filter(filter_decision="STRONG")

    if gate_cfg.blocked_domains and isinstance(gate_cfg.blocked_domains, list):
        base_qs = base_qs.exclude(job_domain__in=gate_cfg.blocked_domains)

    if qualified_only:
        min_words = max(1, int(gate_cfg.min_word_count or 80))
        min_chars = max(1, int(gate_cfg.min_char_count or 400))
        # Only pre-filter by substantive JD text. Do NOT pre-filter by classification
        # confidence here; the real gate (evaluate_raw_job_gate) decides pass/fail/lane.
        # Pre-filtering by confidence was silently excluding large volumes and made
        # "Sync Qualified to Vet Queue" look capped even when max_jobs=0 (all).
        if gate_cfg.require_description:
            base_qs = base_qs.filter(
                has_description=True,
                word_count__gte=min_words,
            ).annotate(
                _jd_len=Length(Coalesce(F("description_clean"), F("description"), Value("")))
            ).filter(_jd_len__gte=min_chars)
        else:
            base_qs = base_qs.filter(
                word_count__gte=min_words,
            ).annotate(
                _jd_len=Length(Coalesce(F("description_clean"), F("description"), Value("")))
            ).filter(_jd_len__gte=min_chars)

    total_candidates = base_qs.count()
    total_target = min(total_candidates, max_jobs) if max_jobs else total_candidates
    synced = skipped = failed = processed = 0
    skipped_reasons: dict[str, int] = {}
    # Always report progress at start so Ops Center shows the task even if 0 candidates.
    update_task_progress(
        self,
        current=0,
        total=max(total_target, 1),
        message=(
            f"Sync qualified RawJobs to Vet Queue — {total_candidates:,} candidates…"
            if qualified_only else
            f"Sync RawJobs to Vet Queue — {total_candidates:,} candidates…"
        ),
    )

    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.SYNC_POOL,
        getattr(self.request, "id", "") or "",
        queue={
            "qualified_only": qualified_only,
            "max_jobs": max_jobs,
            "chunk_size": chunk_size,
            "total_candidates": total_candidates,
            "total_target": total_target,
        },
    )

    if total_target == 0:
        logger.info("Sync task: 0 qualifying candidates. qualified_only=%s", qualified_only)
        out = {
            "qualified_only": bool(qualified_only),
            "processed": 0,
            "candidates": total_candidates,
            "synced": 0,
            "skipped": 0,
            "failed": 0,
            "skipped_reasons": {},
        }
        # Location reconcile must run even when there is nothing new to promote —
        # post-sync country corrections are independent of new candidates.
        try:
            out["location_reconcile"] = _reconcile_synced_job_locations()
        except Exception as _re:
            logger.exception("location reconcile failed (non-fatal): %s", _re)
            out["location_reconcile"] = {"error": str(_re)[:200]}
        finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, out)
        return out

    try:
        last_pk = None
        while processed < total_target:
            take = min(chunk_size, total_target - processed)
            page_qs = base_qs
            if last_pk is not None:
                page_qs = page_qs.filter(pk__lt=last_pk)
            batch = list(
                page_qs.order_by("-pk")
                .select_related("company", "job_platform", "platform_label", "platform_label__platform")[:take]
            )
            if not batch:
                break
            last_pk = batch[-1].pk
            for rj in batch:
                processed += 1
                try:
                    gate = evaluate_raw_job_gate(rj, cfg=gate_cfg)
                except Exception as _gate_exc:
                    logger.error("evaluate_raw_job_gate crashed for RawJob %s: %s", rj.pk, _gate_exc)
                    failed += 1
                    continue

                existing = (
                    (Job.objects.filter(url_hash=rj.url_hash, is_archived=False).first() if rj.url_hash else None)
                    or find_existing_job_by_url(rj.original_url)
                    # Guard: original_link is URLField(max_length=500); a query with a longer
                    # value raises DataError in PostgreSQL and crashes the whole task since this
                    # line is outside the per-job try/except.
                    or (Job.objects.filter(original_link=rj.original_url).first() if rj.original_url and len(rj.original_url) <= 500 else None)
                )
                if existing:
                    payload = dict(rj.raw_payload or {})
                    payload["vet_gate"] = {
                        "status": "duplicate",
                        "reason_code": "DUPLICATE_EXISTING",
                        "existing_job_id": existing.pk,
                        "checked_at": _tz.now().isoformat(),
                    }
                    rj.sync_status = "SKIPPED"
                    rj.sync_skip_reason = "DUPLICATE_EXISTING"
                    rj.raw_payload = payload
                    try:
                        rj.save(update_fields=["sync_status", "sync_skip_reason", "raw_payload", "updated_at"])
                    except Exception as _save_exc:
                        logger.warning("Could not save duplicate-skip status for RawJob %s: %s", rj.pk, _save_exc)
                    PipelineEvent.record(
                        job=existing,
                        url_hash=rj.url_hash or "",
                        from_stage=getattr(existing, "stage", "") or "",
                        to_stage=getattr(existing, "stage", "") or "",
                        task_name="harvest.sync_harvested_to_pool",
                        celery_id=getattr(self.request, "id", "") or "",
                        status=PipelineEvent.Status.SKIPPED,
                        meta={
                            "raw_job_id": rj.pk,
                            "qualified_only": bool(qualified_only),
                            "reason_code": "DUPLICATE_EXISTING",
                        },
                    )
                    skipped += 1
                    if total_target:
                        update_task_progress(
                            self,
                            current=processed,
                            total=total_target,
                            message=(
                                f"Qualified sync {processed:,}/{total_target:,}"
                                if qualified_only
                                else f"Sync {processed:,}/{total_target:,}"
                            ),
                        )
                    continue
    
                if not gate.passed:
                    if qualified_only:
                        # For qualified-only runs, keep non-passing rows pending so they can
                        # be re-enriched/revalidated later instead of being force-failed here.
                        reason_key = (gate.reason_code or "UNKNOWN").strip() or "UNKNOWN"
                        skipped_reasons[reason_key] = skipped_reasons.get(reason_key, 0) + 1
                        skipped += 1
                        if total_target:
                            update_task_progress(
                                self,
                                current=processed,
                                total=total_target,
                                message=f"Qualified sync {processed:,}/{total_target:,}",
                            )
                        continue
                    payload = dict(rj.raw_payload or {})
                    payload["vet_gate"] = {
                        "status": "blocked",
                        "reason_code": gate.reason_code,
                        "reasons": gate.reasons,
                        "checks": gate.checks,
                        "scores": {
                            "data_quality": gate.data_quality_score,
                            "trust": gate.trust_score,
                            "candidate_fit": gate.candidate_fit_score,
                            "vet_priority": gate.vet_priority_score,
                        },
                        "checked_at": _tz.now().isoformat(),
                    }
                    rj.sync_status = "FAILED"
                    rj.sync_skip_reason = (gate.reason_code or "")[:32]
                    rj.raw_payload = payload
                    try:
                        rj.save(update_fields=["sync_status", "sync_skip_reason", "raw_payload", "updated_at"])
                    except Exception as _save_exc:
                        logger.warning("Could not save gate-fail status for RawJob %s: %s", rj.pk, _save_exc)
                    failed += 1
                    if total_target:
                        update_task_progress(
                            self,
                            current=processed,
                            total=total_target,
                            message=f"Sync {processed:,}/{total_target:,}",
                        )
                    continue
    
                try:
                    platform_slug = rj.platform_slug or (rj.job_platform.slug if rj.job_platform else "")
                    job_location = " | ".join(rj.location_candidates or []) or rj.location_raw or ""
                    job_country = rj.country or ((rj.country_codes or [""])[0] if rj.country_codes else "")
                    with transaction.atomic():
                        job = Job.objects.create(
                            title=(rj.title or "")[:200],  # Job.title max_length=200; RawJob.title up to 512
                            company=(rj.company_name or (rj.company.name if rj.company else ""))[:200],
                            company_obj=rj.company,
                            location=job_location[:200],   # Job.location max_length=200
                            description=rj.description or rj.title or "",  # Job.description is NOT NULL
                            original_link=(rj.original_url or "")[:500],  # Job.original_link max_length=500; RawJob up to 1024
                            salary_range=(rj.salary_raw or "")[:100],     # Job.salary_range max_length=100
                            job_type=(rj.employment_type if rj.employment_type and rj.employment_type != "UNKNOWN" else "FULL_TIME")[:20],  # max_length=20; guard None
                            status="POOL",
                            stage=Job.Stage.VETTED,
                            stage_changed_at=_tz.now(),
                            url_hash=rj.url_hash or "",
                            job_source=(f"HARVESTED_{platform_slug.upper()}" if platform_slug else "HARVESTED")[:100],  # max_length=100
                            posted_by=system_user,
                            source_raw_job=rj,
                            queue_entered_at=_tz.now(),
                            # Propagate classification from RawJob if available
                            country=job_country[:100],                    # Job.country max_length=100
                            department=(rj.department_normalized or "")[:20],  # Job.department max_length=20
                        )
                        apply_gate_result_to_job(job, gate)
                        job.quality_score = compute_quality_score(job)
                        job.validation_score = int(round(gate.vet_priority_score * 100))
                        job.validation_result = {
                            "score": job.validation_score,
                            "lane": gate.lane,
                            "gate_status": gate.status,
                            "reason_code": gate.reason_code,
                            "reasons": gate.reasons,
                            "checks": gate.checks,
                            "multi_score": {
                                "data_quality": gate.data_quality_score,
                                "trust": gate.trust_score,
                                "candidate_fit": gate.candidate_fit_score,
                                "vet_priority": gate.vet_priority_score,
                            },
                        }
                        job.validation_run_at = _tz.now()
                        job.gate_checked_at = _tz.now()
                        job.save(
                            update_fields=[
                                "hard_gate_passed", "gate_status", "vet_lane",
                                "pipeline_reason_code", "pipeline_reason_detail",
                                "hard_gate_failures", "hard_gate_checks",
                                "data_quality_score", "trust_score", "candidate_fit_score", "vet_priority_score",
                                "quality_score", "validation_score", "validation_result", "validation_run_at",
                                "gate_checked_at",
                            ]
                        )
                        payload = dict(rj.raw_payload or {})
                        payload["vet_gate"] = {
                            "status": "eligible",
                            "lane": gate.lane,
                            "reason_code": gate.reason_code,
                            "checks": gate.checks,
                            "scores": {
                                "data_quality": gate.data_quality_score,
                                "trust": gate.trust_score,
                                "candidate_fit": gate.candidate_fit_score,
                                "vet_priority": gate.vet_priority_score,
                            },
                            "job_id": job.pk,
                            "checked_at": _tz.now().isoformat(),
                        }
                        try:
                            from jobs.marketing_role_routing import assign_marketing_roles_to_job

                            assign_marketing_roles_to_job(job, raw_job=rj)
                        except Exception as _mr_exc:
                            logger.warning(
                                "Could not assign marketing roles to job %s from raw job %s: %s",
                                job.pk, rj.pk, _mr_exc,
                            )
                        rj.sync_status = "SYNCED"
                        rj.raw_payload = payload
                        rj.save(update_fields=["sync_status", "raw_payload", "updated_at"])
                        PipelineEvent.record(
                            job=job,
                            from_stage=Job.Stage.ENRICHED,
                            to_stage=Job.Stage.VETTED,
                            task_name="harvest.sync_harvested_to_pool",
                            celery_id=getattr(self.request, "id", "") or "",
                            status=PipelineEvent.Status.SUCCESS,
                            meta={
                                "raw_job_id": rj.pk,
                                "qualified_only": bool(qualified_only),
                                "gate_status": gate.status,
                                "lane": gate.lane,
                                "reason_code": gate.reason_code,
                                "scores": {
                                    "data_quality": gate.data_quality_score,
                                    "trust": gate.trust_score,
                                    "candidate_fit": gate.candidate_fit_score,
                                    "vet_priority": gate.vet_priority_score,
                                },
                            },
                        )
                        synced += 1
                except Exception as e:
                    payload = dict(rj.raw_payload or {})
                    payload["vet_gate"] = {
                        "status": "failed",
                        "reason_code": "POOL_SYNC_ERROR",
                        "error": str(e)[:240],
                        "checked_at": _tz.now().isoformat(),
                    }
                    rj.sync_status = "FAILED"
                    rj.raw_payload = payload
                    try:
                        rj.save(update_fields=["sync_status", "raw_payload", "updated_at"])
                    except Exception as _save_exc:
                        # If we can't persist the failure status, just log and continue —
                        # don't let the error-handler crash the whole task run.
                        logger.warning("Could not save error status for RawJob %s: %s", rj.pk, _save_exc)
                    PipelineEvent.record(
                        url_hash=rj.url_hash or "",
                        from_stage="ENRICHED",
                        to_stage="ERROR",
                        task_name="harvest.sync_harvested_to_pool",
                        celery_id=getattr(self.request, "id", "") or "",
                        status=PipelineEvent.Status.FAILED,
                        error=str(e)[:240],
                        meta={
                            "raw_job_id": rj.pk,
                            "qualified_only": bool(qualified_only),
                            "reason_code": "POOL_SYNC_ERROR",
                        },
                    )
                    logger.error("Sync failed for RawJob %s: %s", rj.pk, e)
                    failed += 1
    
                if total_target:
                    update_task_progress(
                        self,
                        current=processed,
                        total=total_target,
                        message=(
                            f"Qualified sync {processed:,}/{total_target:,}"
                            if qualified_only
                            else f"Sync {processed:,}/{total_target:,}"
                        ),
                    )

        _invalidate_rawjobs_dashboard_cache()
        logger.info(
            "Sync complete: qualified_only=%s processed=%d synced=%d skipped=%d failed=%d target=%d candidates=%d skipped_reasons=%s",
            qualified_only, processed, synced, skipped, failed, total_target, total_candidates, skipped_reasons,
        )
        out = {
            "qualified_only": bool(qualified_only),
            "processed": processed,
            "candidates": total_candidates,
            "target": total_target,
            "synced": synced,
            "skipped": skipped,
            "failed": failed,
            "skipped_reasons": skipped_reasons,
        }
        # Reconcile location/country drift on already-synced jobs (flag-gated).
        # When a RawJob's country is resolved AFTER sync, the pool Job kept the
        # stale value — this propagates the corrected value forward.
        try:
            out["location_reconcile"] = _reconcile_synced_job_locations()
        except Exception as _re:
            logger.exception("location reconcile failed (non-fatal): %s", _re)
            out["location_reconcile"] = {"error": str(_re)[:200]}
        finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, out)
        return out

    except Exception as e:
        logger.exception("sync_harvested_to_pool_task failed: %s", e)
        finish_ops_run(
            ops_run,
            HarvestOpsRun.Status.FAILED,
            {
                "error": str(e)[:500],
                "qualified_only": bool(qualified_only),
                "processed": processed,
                "candidates": total_candidates,
                "target": total_target,
                "synced": synced,
                "skipped": skipped,
                "failed": failed,
                "skipped_reasons": skipped_reasons,
            },
        )
        raise


# ─── Job Jarvis — single-URL ingestion ───────────────────────────────────────

@shared_task(bind=True, name="harvest.jarvis_ingest")
def jarvis_ingest_task(self, url: str, user_id: int | None = None):
    """
    Fetch *url* with JobJarvis, extract all job fields, find-or-create the
    Company, and persist a RawJob (platform_slug="jarvis" or detected slug).

    Even when the individual job is expired/unavailable:
    - Detects ATS/platform from the URL
    - Finds or creates the Company + CompanyPlatformLabel
    - Auto-triggers a background full-company scrape (if fetch_all_supported
      and the company hasn't been scraped in the last 24 h)

    Returns a dict with the extracted data plus ``raw_job_id`` when saved
    successfully, or ``error`` + ``discovery`` on failure.
    """
    from .jarvis import JobJarvis, _detect_platform
    from .models import RawJob, JobBoardPlatform, CompanyFetchRun
    from .normalizer import compute_url_hash, compute_content_hash

    update_task_progress(self, current=0, total=3, message="Fetching job page…")

    # ── Step 1: Detect ATS from URL (no HTTP, pure pattern match) ────────────
    detected_ats_early = _detect_platform(url)
    company_name_early = _extract_company_from_url(url)
    logger.info("Jarvis: url=%s detected_ats=%s company_hint=%s", url, detected_ats_early, company_name_early)

    # ── Step 2: Fetch the individual job ─────────────────────────────────────
    jarvis = JobJarvis()
    job_fetch_ok = True
    job_error = ""
    data: dict = {}
    try:
        data = jarvis.ingest(url)
        if data.get("error"):
            job_fetch_ok = False
            job_error = data["error"]
    except Exception as exc:
        job_fetch_ok = False
        job_error = str(exc)
        data = {}

    if not job_fetch_ok:
        logger.warning("Jarvis: job fetch failed (%s) — running company/platform discovery for %s", job_error, url)

    update_task_progress(self, current=1, total=3, message="Resolving company & platform…")

    # ── Step 3: Resolve Company — always runs, even on job fetch failure ──────
    company_name = (data.get("company_name") or company_name_early or "").strip()
    if not company_name:
        company_name = company_name_early

    company = _jarvis_resolve_company(company_name, url)

    # ── Resolve platform ──────────────────────────────────────────────────────
    # Always tag Jarvis imports as platform_slug="jarvis" so they form their
    # own namespace and the recent-imports list is easy to filter.
    # The real detected ATS (greenhouse, lever, etc.) is stored in raw_payload.
    # Bug fix: when job fetch fails data={} so platform_slug is "". Fall back
    # to the URL-pattern detection that ran before the HTTP call.
    platform_slug = "jarvis"
    detected_ats = data.get("platform_slug") or detected_ats_early or ""
    job_platform = None
    if detected_ats:
        job_platform = JobBoardPlatform.objects.filter(slug=detected_ats).first()
        if not job_platform and detected_ats == "dayforce":
            job_platform, _ = JobBoardPlatform.objects.get_or_create(
                slug="dayforce",
                defaults={
                    "name": "Dayforce",
                    "url_patterns": ["jobs.dayforcehcm.com", "dayforcehcm.com"],
                    "api_type": JobBoardPlatform.ApiType.UNKNOWN,
                    "notes": "Auto-created by Jarvis from Dayforce import detection.",
                },
            )

    # ── Build RawJob ──────────────────────────────────────────────────────────
    from datetime import timedelta
    original_url = data.get("original_url") or url
    url_hash = compute_url_hash(original_url)

    # ── Resolve company label context for fetch-all workflows ───────────────
    platform_label, board_ctx = _jarvis_ensure_company_platform_label(
        company=company,
        detected_ats=detected_ats,
        source_url=original_url,
        job_platform=job_platform,
    )

    # ── Step 4: Auto-trigger full-company scrape in background ───────────────
    # Fires when: platform supports fetch-all AND no run started in last 24h
    # AND no run is currently in progress (prevents duplicate concurrent scrapes).
    bg_scrape_triggered = False
    bg_scrape_reason = ""
    if board_ctx.get("fetch_all_supported") and platform_label:
        from datetime import timedelta as _td
        cutoff = timezone.now() - _td(hours=24)
        # Bug fix: also block if a run is currently RUNNING — not just recent ones
        blocking_run = CompanyFetchRun.objects.filter(
            label=platform_label,
        ).filter(
            models.Q(started_at__gte=cutoff) |
            models.Q(status=CompanyFetchRun.Status.RUNNING)
        ).exists()
        if not blocking_run:
            # countdown=30 gives the newly created label time to fully commit
            fetch_raw_jobs_for_company_task.apply_async(
                args=[platform_label.pk],
                kwargs={"triggered_by": "JARVIS", "fetch_all": True},
                countdown=30,
            )
            bg_scrape_triggered = True
            logger.info(
                "Jarvis: triggered background scrape for label=%s company=%s platform=%s",
                platform_label.pk, company.name, detected_ats,
            )
        else:
            bg_scrape_reason = "scraped_recently_or_running"

    # ── If individual job fetch failed, return discovery result now ──────────
    if not job_fetch_ok:
        update_task_progress(self, current=3, total=3, message="Discovery complete (job unavailable)")
        return {
            "ok": False,
            "job_error": job_error,
            "job_unavailable": True,
            "url": url,
            "discovery": {
                "company_name": company.name if company else company_name,
                "company_id": company.pk if company else None,
                "detected_ats": detected_ats or detected_ats_early,
                "platform_label_id": platform_label.pk if platform_label else None,
                "fetch_all_supported": board_ctx.get("fetch_all_supported", False),
                "bg_scrape_triggered": bg_scrape_triggered,
                "bg_scrape_reason": bg_scrape_reason,
                "company_jobs_url": board_ctx.get("company_jobs_url", ""),
            },
        }

    # Parse posted_date
    posted_date = _jarvis_parse_date(data.get("posted_date_raw", ""))
    closing_date = _jarvis_parse_date(data.get("closing_date_raw", ""))

    from .enrichments import clean_job_content, clean_job_text
    from .location_resolver import evaluate_rawjob_scope, extract_location_candidates

    # Normalize HTML-heavy scraped content into cleaner plain text.
    desc_meta = clean_job_content(data.get("description") or "", max_len=50000)
    description = desc_meta["clean_text"]
    requirements = clean_job_text(data.get("requirements") or "", max_len=20000)
    benefits = clean_job_text(data.get("benefits") or "", max_len=10000)

    # Enrich raw_payload with Jarvis metadata
    raw_payload = data.get("raw_payload") or {}
    raw_payload["jarvis_detected_ats"] = detected_ats
    raw_payload["jarvis_strategy"] = data.get("strategy", "")
    raw_payload["jarvis_source_url"] = url
    raw_payload["jarvis_tenant_id"] = board_ctx.get("tenant_id") or ""
    raw_payload["jarvis_company_jobs_url"] = board_ctx.get("company_jobs_url") or ""
    raw_payload["jarvis_platform_label_id"] = platform_label.pk if platform_label else None
    raw_payload["jarvis_fetch_all_supported"] = bool(board_ctx.get("fetch_all_supported"))
    supplied_location_candidates = data.get("location_candidates")
    if not isinstance(supplied_location_candidates, list):
        supplied_location_candidates = []
    location_candidates = (
        supplied_location_candidates
        or extract_location_candidates(
            location_raw=data.get("location_raw") or "",
            city=data.get("city") or "",
            state=data.get("state") or "",
            country=data.get("country") or "",
            vendor_location_block=data.get("vendor_location_block") or "",
            raw_payload=raw_payload,
        )
    )

    # ── Run enrichment extraction ─────────────────────────────────────────
    from .enrichments import CURRENT_DOMAIN_VERSION, CURRENT_ENRICHMENT_VERSION, extract_enrichments
    enriched = extract_enrichments(build_enrichment_input(
        data,
        overrides={
            "description": description,
            "description_clean": description[:50000],
            "description_raw_html": (desc_meta.get("raw_html") or "")[:120000],
            "has_html_content": bool(desc_meta.get("has_html_content")),
            "cleaning_version": (desc_meta.get("cleaning_version") or "v2")[:20],
            "requirements": requirements,
            "benefits": benefits,
            "raw_payload": raw_payload,
        },
        company_name=company_name or "",
        posted_date=posted_date,
    ))

    external_id = (data.get("external_id") or "").strip()[:512]
    raw_job_defaults = {
        "company": company,
        "platform_label": platform_label,
        "job_platform": job_platform,
        "platform_slug": platform_slug,
        "external_id": external_id,
        "original_url": original_url[:1024],
        "apply_url": (data.get("apply_url") or original_url)[:1024],
        "title": (data.get("title") or "Untitled")[:512],
        "company_name": company_name[:256] if company_name else "",
        "department": (data.get("department") or "")[:256],
        "team": (data.get("team") or "")[:256],
        "location_raw": (data.get("location_raw") or "")[:512],
        "city": (data.get("city") or "")[:128],
        "state": (data.get("state") or "")[:128],
        "country": (data.get("country") or "")[:128],
        "location_candidates": location_candidates,
        "country_codes": data.get("country_codes") if isinstance(data.get("country_codes"), list) else [],
        "is_remote": bool(data.get("is_remote")),
        "location_type": data.get("location_type") or "UNKNOWN",
        "employment_type": data.get("employment_type") or "UNKNOWN",
        "experience_level": data.get("experience_level") or "UNKNOWN",
        "salary_min": data.get("salary_min"),
        "salary_max": data.get("salary_max"),
        "salary_currency": (data.get("salary_currency") or "USD")[:8],
        "salary_period": (data.get("salary_period") or "")[:16],
        "salary_raw": (data.get("salary_raw") or "")[:256],
        "description": description,
        "description_clean": (enriched.get("description_clean") or description)[:50000],
        "description_raw_html": (desc_meta.get("raw_html") or "")[:120000],
        "has_html_content": bool(desc_meta.get("has_html_content")),
        "cleaning_version": (desc_meta.get("cleaning_version") or "v2")[:20],
        "requirements": requirements,
        "benefits": benefits,
        "posted_date": posted_date,
        "closing_date": closing_date,
        "raw_payload": raw_payload,
        "content_hash": compute_content_hash(
            company.pk,
            data.get("title") or "",
            data.get("location_raw") or "",
        ),
        "sync_status": "PENDING",
        "is_active": True,
        "expires_at": timezone.now() + timedelta(days=30),
        **_company_snapshot_fields(company),
        # ── enrichment fields ─────────────────────────────────────────
        **enriched,
    }

    from .services.rawjob_upsert import upsert_raw_job_with_dedupe

    upsert = upsert_raw_job_with_dedupe(
        company=company,
        defaults=raw_job_defaults,
        url_hash=url_hash,
        original_url=original_url,
        external_id=external_id,
        job_platform=job_platform,
        platform_slug=(job_platform.slug if job_platform else platform_slug),
    )
    raw_job = upsert.raw_job
    created = upsert.created
    if upsert.action == "duplicate" or raw_job is None:
        logger.info(
            "Jarvis ingest skipped (%s duplicate=%s): %s | %s",
            upsert.reason,
            upsert.duplicate_pk,
            data.get("title"),
            company_name,
        )
        return {
            "ok": False,
            "skipped": True,
            "reason": upsert.reason or "duplicate",
            "title": data.get("title", ""),
            "company_name": company_name,
        }
    if raw_job:
        try:
            from .models import RawJobPayloadSnapshot
            from .payload_archive import capture_rawjob_payload_snapshot
            capture_rawjob_payload_snapshot(
                raw_job,
                payload=data.get("raw_payload") or {},
                raw_html=desc_meta.get("raw_html") or "",
                payload_kind=RawJobPayloadSnapshot.PayloadKind.DETAIL,
                source_url=url,
                platform_slug=platform_slug,
                source_metadata={
                    "ingest": "jarvis_single_job",
                    "strategy": data.get("strategy", ""),
                    "detected_ats": detected_ats,
                    "external_id": external_id,
                },
            )
        except Exception:
            logger.exception("Failed to archive Jarvis source payload for RawJob %s", url_hash)
        evaluate_rawjob_scope(raw_job, use_provider=None, save=True)
    _invalidate_rawjobs_dashboard_cache()

    update_task_progress(self, current=3, total=3, message="Done ✓")

    logger.info(
        "Jarvis ingested: %s | %s | raw_job_id=%d (%s)",
        data.get("title"), company_name, raw_job.pk,
        "created" if created else "updated",
    )

    return {
        "ok": True,
        "raw_job_id": raw_job.pk,
        "created": created,
        "title": data.get("title", ""),
        "company_name": company.name,
        "company_id": company.pk,
        "platform_slug": platform_slug,
        "strategy": data.get("strategy", ""),
        "detected_ats": detected_ats,
        "tenant_id": board_ctx.get("tenant_id") or "",
        "company_jobs_url": board_ctx.get("company_jobs_url") or "",
        "platform_label_id": platform_label.pk if platform_label else None,
        "fetch_all_supported": bool(board_ctx.get("fetch_all_supported")),
        "bg_scrape_triggered": bg_scrape_triggered,
        "data": {k: v for k, v in data.items() if k != "raw_payload"},
    }


def _jarvis_company_jobs_url(platform_slug: str, tenant_id: str) -> str:
    """Build a user-facing company jobs URL from platform + tenant."""
    if not platform_slug or not tenant_id:
        return ""
    try:
        # Dayforce board root is cleaner than /jobs for user navigation.
        if platform_slug == "dayforce":
            if "|" in tenant_id:
                tenant, board = tenant_id.split("|", 1)
                tenant = (tenant or "").strip()
                board = (board or "").strip() or "CANDIDATEPORTAL"
                if tenant:
                    return f"https://jobs.dayforcehcm.com/en-US/{tenant}/{board}"
            t = tenant_id.strip()
            if t:
                return f"https://jobs.dayforcehcm.com/en-US/{t}/CANDIDATEPORTAL"

        from .career_url import build_career_url
        return build_career_url(platform_slug, tenant_id)
    except Exception:
        return ""


def _jarvis_ensure_company_platform_label(*, company, detected_ats: str, source_url: str, job_platform=None):
    """
    Ensure CompanyPlatformLabel exists for Jarvis-ingested company.

    Returns ``(label_or_none, board_context_dict)`` where board_context includes:
      - tenant_id
      - company_jobs_url
      - fetch_all_supported
      - platform_slug
    """
    from .detectors import extract_tenant
    from .harvesters import get_harvester
    from .models import CompanyPlatformLabel, JobBoardPlatform

    platform_slug = (detected_ats or "").strip().lower()
    tenant_id = extract_tenant(platform_slug, source_url) if platform_slug and source_url else ""
    company_jobs_url = _jarvis_company_jobs_url(platform_slug, tenant_id)
    board_ctx = {
        "platform_slug": platform_slug,
        "tenant_id": tenant_id,
        "company_jobs_url": company_jobs_url,
        "fetch_all_supported": False,
    }
    if not company:
        return None, board_ctx

    now_ts = timezone.now()

    platform = job_platform if getattr(job_platform, "slug", "") == platform_slug else None
    if not platform and platform_slug:
        platform = JobBoardPlatform.objects.filter(slug=platform_slug).first()
    if not platform and platform_slug == "dayforce":
        platform, _ = JobBoardPlatform.objects.get_or_create(
            slug="dayforce",
            defaults={
                "name": "Dayforce",
                "url_patterns": ["jobs.dayforcehcm.com", "dayforcehcm.com"],
                "api_type": JobBoardPlatform.ApiType.UNKNOWN,
                "notes": "Auto-created by Jarvis from Dayforce import detection.",
            },
        )

    # Prefer an existing label that matches the detected platform/tenant instead
    # of taking an arbitrary first label for the company.
    label = None
    company_labels = CompanyPlatformLabel.objects.filter(company=company)
    if platform:
        label = (
            company_labels
            .filter(platform=platform)
            .order_by("-is_verified", "pk")
            .first()
        )
    if not label and tenant_id:
        label = (
            company_labels
            .filter(tenant_id=tenant_id)
            .order_by("-is_verified", "pk")
            .first()
        )
    if not label:
        label = company_labels.order_by("-is_verified", "pk").first()
    if not label and not platform_slug:
        return None, board_ctx
    if not label:
        label = CompanyPlatformLabel.objects.create(
            company=company,
            platform=platform,
            tenant_id=tenant_id,
            confidence=CompanyPlatformLabel.Confidence.HIGH,
            detection_method=CompanyPlatformLabel.DetectionMethod.URL_PATTERN,
            detected_at=now_ts if platform else None,
            last_checked_at=now_ts,
        )

    changed: list[str] = []
    is_manual_locked = bool(
        label.is_verified
        or label.detection_method == CompanyPlatformLabel.DetectionMethod.MANUAL
    )

    # Jarvis URL should correct stale auto-detected platform labels unless manually locked.
    if platform:
        if not label.platform_id:
            label.platform = platform
            changed.append("platform")
        elif label.platform_id != platform.pk and not is_manual_locked:
            label.platform = platform
            changed.append("platform")

    # Prefer extracted tenant when we have one. If platform changed, refresh tenant too.
    if tenant_id:
        platform_changed = "platform" in changed
        if not (label.tenant_id or "").strip() or platform_changed or (label.tenant_id != tenant_id and not is_manual_locked):
            label.tenant_id = tenant_id
            changed.append("tenant_id")

    # Keep detection metadata healthy for auto-detected labels.
    if platform and label.detection_method == CompanyPlatformLabel.DetectionMethod.UNDETECTED:
        label.detection_method = CompanyPlatformLabel.DetectionMethod.URL_PATTERN
        changed.append("detection_method")
    if platform and not label.detected_at:
        label.detected_at = now_ts
        changed.append("detected_at")
    label.last_checked_at = now_ts
    changed.append("last_checked_at")

    if changed:
        # Preserve field order while removing duplicates.
        deduped_fields = list(dict.fromkeys(changed))
        label.save(update_fields=deduped_fields)

    resolved_platform_slug = (
        (label.platform.slug if label.platform else "")
        or platform_slug
        or ""
    )
    resolved_tenant = (label.tenant_id or "").strip() or tenant_id
    resolved_company_jobs_url = (
        _jarvis_company_jobs_url(resolved_platform_slug, resolved_tenant)
        or label.career_page_url
        or company_jobs_url
        or ""
    )

    if resolved_company_jobs_url and not (company.career_site_url or "").strip():
        company.career_site_url = resolved_company_jobs_url
        company.save(update_fields=["career_site_url", "updated_at"])

    board_ctx["platform_slug"] = resolved_platform_slug
    board_ctx["tenant_id"] = resolved_tenant
    board_ctx["company_jobs_url"] = resolved_company_jobs_url
    board_ctx["fetch_all_supported"] = bool(
        resolved_platform_slug
        and resolved_tenant
        and get_harvester(resolved_platform_slug) is not None
    )
    return label, board_ctx


def _extract_company_from_url(url: str) -> str:
    """Best-effort: pull a human-readable company name from the URL hostname."""
    import re as _re
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()

        # Dayforce URLs include tenant in path: /en-US/{tenant}/{board}/jobs/{id}
        # Use tenant as the company fallback instead of the ATS hostname.
        if "dayforcehcm.com" in host:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and _re.match(r"^[a-z]{2}-[a-z]{2}$", parts[0], _re.I):
                tenant = parts[1]
                if tenant:
                    return tenant.replace("-", " ").replace("_", " ").title().strip()

        # Strip www. / jobs. / careers. prefixes
        for prefix in ("www.", "jobs.", "careers.", "boards."):
            if host.startswith(prefix):
                host = host[len(prefix):]
        # Remove known ATS domains: greenhouse.io, lever.co, etc.
        for suffix in (
            ".greenhouse.io", ".lever.co", ".ashbyhq.com",
            ".myworkdayjobs.com", ".workable.com", ".bamboohr.com",
            ".dayforcehcm.com", ".icims.com", ".smartrecruiters.com",
            ".taleo.net", ".jobvite.com", ".zohorecruit.com",
        ):
            suffix_root = suffix.lstrip(".")
            if host == suffix_root:
                host = ""
                break
            if host.endswith(suffix):
                host = host[: -len(suffix)]
        if host in {"dayforcehcm.com", "greenhouse.io", "lever.co", "ashbyhq.com"}:
            return "Unknown"
        # Convert hyphens/dots to spaces, title-case
        company = host.replace("-", " ").replace(".", " ").title()
        return company.strip() or "Unknown"
    except Exception:
        return "Unknown"


def _root_url(url: str) -> str:
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        ats_hosts = (
            "dayforcehcm.com",
            "greenhouse.io",
            "lever.co",
            "ashbyhq.com",
            "myworkdayjobs.com",
            "workable.com",
            "bamboohr.com",
            "smartrecruiters.com",
            "icims.com",
            "taleo.net",
            "jobvite.com",
            "zohorecruit.com",
        )
        if any(host == d or host.endswith("." + d) for d in ats_hosts):
            return ""
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def _jarvis_parse_date(raw: str):
    """Parse an ISO-8601 or YYYY-MM-DD string into a date object (or None)."""
    if not raw:
        return None
    import re as _re
    from datetime import date
    # Extract YYYY-MM-DD from strings like "2026-04-15T00:00:00Z"
    m = _re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _jarvis_company_name_key(raw: str) -> str:
    """Normalize name for duplicate checks (ignore punctuation/legal suffix noise)."""
    import re as _re

    if not raw:
        return ""
    text = raw.strip().lower().replace("&", " and ")
    text = _re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [t for t in text.split() if t]
    if not tokens:
        return ""
    stop = {
        "the", "inc", "incorporated", "llc", "ltd", "ltda", "corp", "corporation",
        "co", "company", "group", "holdings", "plc", "gmbh",
        "sa", "bv", "srl", "pte", "and", "of", "for", "a", "an", "do", "de", "da",
    }
    reduced = [t for t in tokens if t not in stop] or tokens
    return " ".join(reduced)


def _jarvis_clean_company_name(raw: str) -> str:
    import re as _re

    text = (raw or "").strip()
    if not text:
        return ""
    return _re.sub(r"\s+", " ", text).strip(" -_,.")


def _jarvis_resolve_company(company_name: str, job_url: str):
    """
    Smart company lookup for Jarvis imports.

    Priority:
      1. Domain match   — extract root domain from URL, look for company with
                          matching .domain or .website (most reliable)
      2. Exact name     — Company.name == company_name
      3. Fuzzy contains — one name is a substring of the other
                          e.g. "Bayview" ↔ "Bayview Asset Management"
      4. Create new     — only when all matching strategies fail
    """
    from django.db.models import Q
    from companies.models import Company
    from urllib.parse import urlparse

    company_name = _jarvis_clean_company_name(company_name)

    # ── 1. Domain match ──────────────────────────────────────────────────────
    root_domain = ""
    try:
        host = urlparse(job_url).netloc.lower()
        # Strip well-known ATS/career subdomains
        for sub in ("careers.", "jobs.", "boards.", "apply.", "recruiting.",
                    "career.", "job.", "hire.", "talent.", "work."):
            if host.startswith(sub):
                host = host[len(sub):]
                break
        # Remove known ATS root domains entirely (they are not the company domain)
        ATS_DOMAINS = (
            ".greenhouse.io", ".lever.co", ".ashbyhq.com",
            ".myworkdayjobs.com", ".workable.com", ".bamboohr.com",
            ".icims.com", ".taleo.net", ".jobvite.com", ".smartrecruiters.com",
            ".dayforcehcm.com",
        )
        for ats in ATS_DOMAINS:
            ats_root = ats.lstrip(".")
            if host == ats_root or host.endswith(ats):
                host = ""
                break
        if host:
            parts = host.split(".")
            root_domain = ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        pass

    if root_domain:
        match = Company.objects.filter(
            Q(domain__iexact=root_domain) |
            Q(domain__iendswith="." + root_domain) |
            Q(website__icontains=root_domain)
        ).first()
        if match:
            logger.info("Jarvis company match by domain: %s → %s", root_domain, match.name)
            return match

    # ── 2. Exact match (case-insensitive) + normalized key match ───────────
    if company_name:
        exact = (
            Company.objects.filter(Q(name__iexact=company_name) | Q(alias__iexact=company_name))
            .order_by(Length("name"), "name")
            .first()
        )
        if exact:
            logger.info("Jarvis exact company match: '%s' → '%s'", company_name, exact.name)
            return exact

        key = _jarvis_company_name_key(company_name)
        compact = key.replace(" ", "")
        if key:
            token_q = Q()
            for tok in key.split()[:3]:
                token_q |= Q(name__icontains=tok) | Q(alias__icontains=tok)
            candidate_qs = Company.objects.filter(token_q) if token_q else Company.objects.all()
            # Compact-name inputs like "Appliedsystems" should still match existing
            # canonical rows like "Applied Systems". If the token filter returns
            # nothing, widen the scan and rely on normalized compact comparison.
            if token_q and not candidate_qs.exists():
                candidate_qs = Company.objects.all()
            best = None
            for cand in candidate_qs.only("id", "name", "alias").order_by("name")[:300]:
                for cand_name in (cand.name, cand.alias):
                    cand_key = _jarvis_company_name_key(cand_name or "")
                    if not cand_key:
                        continue
                    cand_compact = cand_key.replace(" ", "")
                    if cand_key == key or (compact and cand_compact == compact):
                        if best is None or len(cand.name) < len(best.name):
                            best = cand
                        break
            if best:
                logger.info("Jarvis normalized company match: '%s' → '%s'", company_name, best.name)
                return best

    # ── 3. Word-by-word fuzzy scan ───────────────────────────────────────────
    # NOTE: intentionally skipping a plain exact-name match here.
    # If a previous Jarvis run created a stub company (e.g. "Bayview Asset
    # Management"), an exact match would return that stub instead of the
    # real "Bayview" company. The word scan is smarter: it checks each
    # significant word independently and prefers the shorter / canonical name.
    # Handles variants like:
    #   "BRA 3M do Brasil Ltda." → finds "3M"   (first_word "BRA" wouldn't work)
    #   "Bayview Asset Management" → finds "Bayview"
    #   "3M Company" → finds "3M"
    _STOP = {"the", "inc", "llc", "ltd", "ltda", "corp", "co", "company",
             "group", "holdings", "do", "de", "da", "di", "du", "van", "and",
             "of", "for", "a", "an", "&"}

    if company_name:
        name_lower = company_name.lower()
        words = [w.strip(".,") for w in company_name.split()
                 if len(w.strip(".,")) >= 2 and w.lower().strip(".,") not in _STOP]

        best = None
        seen_ids: set[int] = set()

        for word in words:
            # First try: exact company name == this single word (e.g., "3M")
            try:
                exact_word = Company.objects.get(name__iexact=word)
                if exact_word.pk not in seen_ids:
                    logger.info(
                        "Jarvis word-exact match: '%s' (word '%s') → '%s'",
                        company_name, word, exact_word.name,
                    )
                    return exact_word
            except Company.DoesNotExist:
                pass
            except Company.MultipleObjectsReturned:
                pass

            # Second try: word-boundary scan.
            # Advanced technique: use \bword\b regex instead of substring containment.
            # This means "Whop" will NOT match "Whoop" (different tokens) but
            # "Bayview" WILL match "Bayview Asset Management" (whole word present).
            # No length-ratio hacks needed — word boundaries handle it cleanly.
            import re as _re2
            word_pat = _re2.compile(r'\b' + _re2.escape(word.lower()) + r'\b')
            for cand in Company.objects.filter(name__icontains=word).order_by("name")[:10]:
                if cand.pk in seen_ids:
                    continue
                seen_ids.add(cand.pk)
                cn = cand.name.lower()
                # Build reverse pattern: first significant word of candidate in our name
                cand_first = (cn.split() or [""])[0]
                cand_pat = (
                    _re2.compile(r'\b' + _re2.escape(cand_first) + r'\b')
                    if len(cand_first) >= 3 else None
                )
                # Accept if our word appears as a whole token in candidate,
                # OR candidate's first word appears as a whole token in our name.
                if word_pat.search(cn) or (cand_pat and cand_pat.search(name_lower)):
                    if best is None or len(cand.name) < len(best.name):
                        best = cand

        if best:
            # ── Fuzzy match found — DO NOT auto-merge ────────────────────────
            # "Bayview" and "Bayview Asset Management" could be two completely
            # different companies. Create the new company and flag for human
            # review instead of silently merging and corrupting data.
            clean_name = company_name or "Unknown (Jarvis Import)"
            new_company, created = Company.objects.get_or_create(
                name=clean_name,
                defaults={
                    "website": _root_url(job_url),
                    "possible_duplicate_of": best,
                    "needs_review": True,
                },
            )
            if created:
                logger.warning(
                    "Jarvis: created new company '%s' (flagged for review) — "
                    "possible duplicate of '%s' (id=%d). "
                    "Admin should verify and merge if same company.",
                    clean_name, best.name, best.pk,
                )
            else:
                logger.info("Jarvis: existing company '%s' matched (get_or_create)", clean_name)
            return new_company

    # ── 4. Create new ────────────────────────────────────────────────────────
    clean_name = company_name or "Unknown (Jarvis Import)"
    company, created = Company.objects.get_or_create(
        name=clean_name,
        defaults={"website": _root_url(job_url)},
    )
    if created:
        logger.info("Jarvis created new company: %s", company.name)
    return company


# ─── Description Backfill ────────────────────────────────────────────────────

def _fast_workday_description(job_url: str) -> str:
    """Fetch a Workday JD directly via the CXS JSON API — no Jarvis/scraping needed.

    Handles URL patterns:
      - https://{sub}.myworkdayjobs.com/{board}/job/{loc}/{slug}
      - https://{sub}.myworkdayjobs.com/{board}/details/{loc}/{slug}
      - https://{sub}.wd1.myworkdayjobs.com/{board}/job/...  (versioned subdomains)
      - https://{sub}.myworkdayjobs.com/en-US/{board}/job/...  (locale-prefixed)
    """
    import re as _re2
    import requests as _req

    # Pattern 1: direct board path — /{board}/(job|details)/...
    m = _re2.match(
        r"https?://([\w.-]+?)\.myworkdayjobs\.com/([^/?#]+)(/(?:details|job)/[^?#]+)",
        job_url, _re2.I,
    )
    if m:
        full_subdomain = m.group(1)
        jobboard = m.group(2)
        ext_path = m.group(3).split("?")[0]
    else:
        # Pattern 2: locale-prefixed — /en-US/{board}/(job|details)/...
        m2 = _re2.match(
            r"https?://([\w.-]+?)\.myworkdayjobs\.com/[a-z]{2}-[A-Z]{2}/([^/?#]+)(/(?:details|job)/[^?#]+)",
            job_url, _re2.I,
        )
        if not m2:
            return ""
        full_subdomain = m2.group(1)
        jobboard = m2.group(2)
        ext_path = m2.group(3).split("?")[0]

    tenant = _re2.sub(r"\.wd\d+$", "", full_subdomain, flags=_re2.I)
    cxs_url = f"https://{full_subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{jobboard}{ext_path}"
    _BROWSER_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://{full_subdomain}.myworkdayjobs.com/",
        "Origin": f"https://{full_subdomain}.myworkdayjobs.com",
    }
    try:
        resp = _req.get(cxs_url, headers=_BROWSER_HEADERS, timeout=12)
        if resp.status_code == 403:
            # Cloudflare / WAF blocking this server IP — signal caller to use Jarvis.
            return "__BLOCKED__"
        if not resp.ok:
            return ""
        data = resp.json()
        if not isinstance(data, dict):
            return ""
        info = data.get("jobPostingInfo") or data
        for key in ("jobDescription", "jobPostingDescription", "externalJobDescription", "shortDescription"):
            val = info.get(key) or data.get(key) or ""
            if isinstance(val, dict):
                val = val.get("content", "") or ""
            val = str(val).strip()
            if val:
                return val
    except Exception:
        pass
    return ""


def _html_jd_extract(url: str, extra_selectors: list[str] | None = None, timeout: int = 15) -> str:
    """Generic HTML JD extractor used by platform fast paths as a fallback.

    Tries in order:
      1. JSON-LD <script type="application/ld+json"> JobPosting schema
      2. <meta property="og:description"> (short but structured)
      3. Platform-specific CSS class selectors (passed via extra_selectors)
      4. Largest <div> block containing 'experience'/'responsibilities'/'qualifications'
    Returns the best non-empty string found, or "".
    """
    import json as _json
    import re as _re
    import requests as _req

    try:
        resp = _req.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 (compatible; GoCareers-Bot/1.0)",
            },
            timeout=timeout,
        )
        if not resp.ok:
            return ""
        html = resp.text
    except Exception:
        return ""

    # 1. JSON-LD JobPosting
    for block in _re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, _re.S | _re.I
    ):
        try:
            schema = _json.loads(block)
            if isinstance(schema, list):
                schema = next((s for s in schema if isinstance(s, dict) and s.get("@type") == "JobPosting"), schema[0] if schema else {})
            if isinstance(schema, dict):
                if schema.get("@type") in ("JobPosting", "jobPosting"):
                    for k in ("description", "responsibilities", "qualifications"):
                        val = schema.get(k) or ""
                        if val and len(str(val)) > 80:
                            return str(val).strip()
        except Exception:
            continue

    # 2. og:description
    og_m = _re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html, _re.I)
    if not og_m:
        og_m = _re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']', html, _re.I)
    og_desc = og_m.group(1).strip() if og_m else ""

    # 3. Extra selectors provided by caller
    if extra_selectors:
        for sel_class in extra_selectors:
            pat = rf'<[^>]+class=["\'][^"\']*{_re.escape(sel_class)}[^"\']*["\'][^>]*>([\s\S]{{100,8000}}?)</[^>]+>'
            sel_m = _re.search(pat, html, _re.I)
            if sel_m:
                text = _re.sub(r"<[^>]+>", " ", sel_m.group(1))
                text = _re.sub(r"\s+", " ", text).strip()
                if len(text) > 100:
                    return text

    # 4. Largest paragraph-heavy block (heuristic)
    blocks = _re.findall(r'<(?:div|section|article)[^>]*>([\s\S]{300,10000}?)</(?:div|section|article)>', html, _re.I)
    best = ""
    for b in blocks:
        text = _re.sub(r"<[^>]+>", " ", b)
        text = _re.sub(r"\s+", " ", text).strip()
        kws = sum(1 for w in ("experience", "responsibilities", "qualifications", "requirements", "role", "position") if w in text.lower())
        if kws >= 2 and len(text) > len(best):
            best = text
    if best:
        return best

    return og_desc


def _fast_greenhouse_description(job_url: str) -> str:
    """Greenhouse public boards API — returns full description in one JSON call."""
    import re as _re
    import requests as _req

    m = _re.search(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", job_url, _re.I)
    if not m:
        m = _re.search(r"greenhouse\.io/(?:jobs|careers)/(\d+)", job_url, _re.I)
        if m:
            return ""  # can't derive board token from this URL form
    if not m:
        return ""
    board_token, job_id = m.group(1), m.group(2)
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}?content=true"
    try:
        resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=10)
        if not resp.ok:
            return ""
        d = resp.json()
        return str(d.get("content") or "").strip()
    except Exception:
        return ""


def _fast_lever_description(job_url: str) -> str:
    """Lever public postings API — returns full description including lists."""
    import re as _re
    import requests as _req

    m = _re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", job_url, _re.I)
    if not m:
        return ""
    company, posting_id = m.group(1), m.group(2)
    api_url = f"https://api.lever.co/v0/postings/{company}/{posting_id}"
    try:
        resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=10)
        if not resp.ok:
            return ""
        d = resp.json()
        parts = []
        desc = d.get("descriptionPlain") or d.get("description") or ""
        if desc:
            parts.append(str(desc).strip())
        for lst in d.get("lists") or d.get("listsPlain") or []:
            if isinstance(lst, dict):
                text = lst.get("content") or lst.get("text") or ""
                if text:
                    parts.append(str(text).strip())
        return "\n\n".join(p for p in parts if p)
    except Exception:
        return ""


def _fast_ashby_description(job_url: str) -> str:
    """Ashby public REST job-board API — fetches full board and finds job by ID.

    api.ashbyhq.com/posting-api/job-board/{company} returns all jobs with
    descriptionHtml inline; no per-job auth required.
    """
    import re as _re
    import requests as _req

    m = _re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", job_url, _re.I)
    if not m:
        return ""
    company, job_id = m.group(1), m.group(2)
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
    try:
        resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=12)
        if not resp.ok:
            return ""
        jobs = resp.json().get("jobs") or []
        match = next((j for j in jobs if (j.get("id") or "").lower() == job_id.lower()), None)
        if not match:
            return ""
        desc = match.get("descriptionHtml") or match.get("descriptionPlain") or ""
        return str(desc).strip()
    except Exception:
        return ""


def _fast_bamboohr_description(job_url: str) -> str:
    """BambooHR — extract job description from JSON-LD on the careers detail page."""
    import json as _json
    import re as _re
    import requests as _req

    m = _re.search(r"([\w-]+)\.bamboohr\.com/(?:careers|jobs)/(\d+)", job_url, _re.I)
    if not m:
        return ""
    slug, job_id = m.group(1), m.group(2)
    detail_url = f"https://{slug}.bamboohr.com/careers/{job_id}"
    try:
        resp = _req.get(
            detail_url,
            headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 (compatible; GoCareers-Bot/1.0)"},
            timeout=15,
        )
        if not resp.ok:
            return ""
        html = resp.text
        for block in _re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, _re.S | _re.I
        ):
            try:
                schema = _json.loads(block)
                if isinstance(schema, list):
                    schema = schema[0]
                if isinstance(schema, dict) and schema.get("@type") in ("JobPosting",):
                    desc = schema.get("description") or ""
                    if desc:
                        return str(desc).strip()
            except Exception:
                continue
        return ""
    except Exception:
        return ""


def _fast_workable_description(job_url: str) -> str:
    """Workable public API v1 — per-job detail endpoint."""
    import re as _re
    import requests as _req

    m = _re.search(r"apply\.workable\.com/([^/]+)/j/([A-Z0-9]+)", job_url, _re.I)
    if not m:
        return ""
    slug, shortcode = m.group(1), m.group(2)
    api_url = f"https://apply.workable.com/api/v1/accounts/{slug}/jobs/{shortcode}"
    try:
        resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=12)
        if not resp.ok:
            return ""
        d = resp.json()
        parts = [
            str(d.get("description") or "").strip(),
            str(d.get("requirements") or "").strip(),
            str(d.get("benefits") or "").strip(),
        ]
        return "\n\n".join(p for p in parts if p)
    except Exception:
        return ""


def _fast_icims_description(job_url: str) -> str:
    """iCIMS — extract structured JSON-LD from job detail page."""
    import json as _json
    import re as _re
    import requests as _req

    m = _re.search(r"([\w-]+)\.icims\.com/jobs/(\d+)/", job_url, _re.I)
    if not m:
        return ""
    tenant, job_id = m.group(1), m.group(2)
    detail_url = f"https://{tenant}.icims.com/jobs/{job_id}/job"
    try:
        resp = _req.get(
            detail_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "Mozilla/5.0 (compatible; GoCareers-Bot/1.0)",
            },
            timeout=15,
        )
        if not resp.ok:
            return ""
        html = resp.text
        for block in _re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, _re.S | _re.I):
            try:
                schema = _json.loads(block)
                if isinstance(schema, list):
                    schema = schema[0]
                if isinstance(schema, dict) and schema.get("@type") in ("JobPosting", "jobPosting"):
                    desc = schema.get("description") or schema.get("responsibilities") or ""
                    if desc:
                        return str(desc).strip()
            except Exception:
                continue
        m2 = _re.search(r'<div[^>]+class=["\'][^"\']*iCIMS_JobDescription[^"\']*["\'][^>]*>(.*?)</div>', html, _re.S | _re.I)
        if m2:
            text = _re.sub(r"<[^>]+>", " ", m2.group(1)).strip()
            if len(text) > 50:
                return text
        return ""
    except Exception:
        return ""


def _fast_oracle_description(job_url: str) -> str:
    """Oracle HCM — fetch description for a specific requisition via REST API.

    Queries the Oracle HCM REST API filtering directly by requisitionId so we
    always get the right job regardless of how many postings the tenant has.
    Falls back to HTML scraping if the API is unavailable.
    """
    import re as _re
    import requests as _req

    m = _re.search(
        r"([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/(?:requisitions?(?:/preview)?|jobs?)/(\d+)",
        job_url, _re.I,
    )
    if not m:
        m = _re.search(r"([\w.-]+\.oraclecloud\.com).*?/(\d{5,})", job_url, _re.I)
        if not m:
            return _html_jd_extract(job_url)
        host, sites_id, req_num = m.group(1), "", m.group(2)
    else:
        host, sites_id, req_num = m.group(1), m.group(2), m.group(3)

    # Build finder — always filter by requisitionId so we hit exactly the one job.
    # siteNumber scopes to the right tenant portal when available.
    finder = f"findReqs;requisitionId={req_num}"
    if sites_id:
        finder = f"findReqs;siteNumber={sites_id},requisitionId={req_num}"

    api_url = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&expand=requisitionList&limit=1&finder={finder}"
    )
    try:
        resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=15)
        if resp.ok:
            items = resp.json().get("items") or []
            req_list = items[0].get("requisitionList", []) if items else []
            for req in req_list:
                if str(req.get("Id") or "") == req_num:
                    for key in ("ExternalDescriptionStr", "ShortDescriptionStr",
                                "ExternalQualificationsStr", "ExternalResponsibilitiesStr"):
                        val = str(req.get(key) or "").strip()
                        if val:
                            return val
    except Exception:
        pass

    # HTML fallback — og:description is usually populated on Oracle CX pages
    return _html_jd_extract(job_url, extra_selectors=["requisition-description", "job-detail__description"])


def _fast_smartrecruiters_description(job_url: str) -> str:
    """SmartRecruiters public API — per-job detail endpoint."""
    import re as _re
    import requests as _req

    m = _re.search(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)", job_url, _re.I)
    if not m:
        m = _re.search(r"smartrecruiters\.com/([^/]+)/(\d+)", job_url, _re.I)
    if not m:
        return ""
    company, job_id = m.group(1), m.group(2)
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}"
    try:
        resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=12)
        if not resp.ok:
            return ""
        d = resp.json()
        sections = (d.get("jobAd") or {}).get("sections") or {}
        parts = [
            str((sections.get("jobDescription") or {}).get("text") or "").strip(),
            str((sections.get("qualifications") or {}).get("text") or "").strip(),
            str((sections.get("additionalInformation") or {}).get("text") or "").strip(),
        ]
        return "\n\n".join(p for p in parts if p)
    except Exception:
        return ""


def _fast_taleo_description(job_url: str) -> str:
    """Taleo — fetch job detail page and extract via JSON-LD or HTML."""
    return _html_jd_extract(job_url, extra_selectors=["ATSJobDetailContainer", "requisitionDescriptionInterface"])


def _fast_ultipro_description(job_url: str) -> str:
    """UltiPro/UKG — fetch OpportunityDetail page and extract description.

    UltiPro detail URL: .../JobBoard/{guid}/OpportunityDetail?opportunityId={id}
    The SPA embeds job data in a <script> or renders it in known CSS classes.
    Also tries the internal GetJob JSON endpoint.
    """
    import re as _re
    import requests as _req

    # Extract company code and GUID from the URL
    m = _re.search(
        r"recruiting\.ultipro\.com/([^/]+)/JobBoard/([0-9a-f-]{36})/OpportunityDetail",
        job_url, _re.I,
    )
    if m:
        company_code, jobboard_id = m.group(1), m.group(2)
        opp_id_m = _re.search(r"opportunityId=([^&]+)", job_url, _re.I)
        if opp_id_m:
            opp_id = opp_id_m.group(1)
            # Try the internal JSON endpoint first
            get_job_url = (
                f"https://recruiting.ultipro.com/{company_code}/JobBoard/{jobboard_id}"
                f"/JobBoardView/GetJob?opportunityId={opp_id}"
            )
            try:
                resp = _req.get(get_job_url, headers={"Accept": "application/json"}, timeout=12)
                if resp.ok:
                    d = resp.json()
                    desc = (
                        d.get("Description") or d.get("description")
                        or d.get("JobDescription") or d.get("jobDescription")
                        or d.get("FullDescription") or ""
                    )
                    if desc and len(str(desc)) > 80:
                        return str(desc).strip()
            except Exception:
                pass

    return _html_jd_extract(job_url, extra_selectors=["opportunity-description", "job-description", "oppDetailDescription"])


def _fast_dayforce_description(job_url: str) -> str:
    """Dayforce HCM — extract job description from detail page.

    Dayforce portals are Next.js SPAs; the og:description meta tag and JSON-LD
    usually have the short description. Full description is in the SPA data.
    """
    import re as _re
    import requests as _req

    # Try Dayforce per-job API (GEO API)
    # URL: https://jobs.dayforcehcm.com/en-US/{slug}/CANDIDATEPORTAL/jobs/{id}
    m = _re.search(
        r"jobs\.dayforcehcm\.com/en-US/([^/]+)/[^/]+/jobs/([^/?#]+)", job_url, _re.I
    )
    if m:
        slug, job_id = m.group(1), m.group(2)
        session = _req.Session()
        # Warm session for Cloudflare
        try:
            session.get(
                f"https://jobs.dayforcehcm.com/en-US/{slug}/CANDIDATEPORTAL",
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=12,
            )
        except Exception:
            pass
        # Try direct job API
        for api_path in [
            f"https://jobs.dayforcehcm.com/api/geo/{slug}/jobposting/{job_id}",
            f"https://jobs.dayforcehcm.com/api/{slug}/jobs/{job_id}",
        ]:
            try:
                resp = session.get(
                    api_path,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    },
                    timeout=12,
                )
                if resp.ok:
                    d = resp.json()
                    desc = (
                        d.get("JobDescription") or d.get("FullDescription")
                        or d.get("description") or d.get("Description") or ""
                    )
                    if desc and len(str(desc)) > 80:
                        return str(desc).strip()
            except Exception:
                pass

    return _html_jd_extract(job_url, extra_selectors=["job-description", "dayforce-job-detail"])


def _fast_jobvite_description(job_url: str) -> str:
    """Jobvite — fetch job detail page and extract via JSON-LD."""
    return _html_jd_extract(job_url, extra_selectors=["jv-job-detail-description", "jv-job-detail-main"])


def _fast_breezy_description(job_url: str) -> str:
    """Breezy HR — fetch job detail page via public API or HTML."""
    import re as _re
    import requests as _req

    # Breezy has a public REST API: GET https://{company}.breezy.hr/p/{job-slug}/json
    m = _re.search(r"([\w-]+)\.breezy\.hr/p/([^/?#]+)", job_url, _re.I)
    if m:
        subdomain, slug = m.group(1), m.group(2)
        api_url = f"https://{subdomain}.breezy.hr/json"
        try:
            resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=12)
            if resp.ok:
                data = resp.json()
                positions = data if isinstance(data, list) else (data.get("positions") or [])
                # Find matching position by slug
                for pos in positions:
                    if pos.get("friendly_id") == slug or slug in (pos.get("friendly_id") or ""):
                        desc = pos.get("description") or pos.get("jobDescription") or ""
                        if desc and len(str(desc)) > 80:
                            return str(desc).strip()
        except Exception:
            pass

    return _html_jd_extract(job_url, extra_selectors=["body-description", "job-description"])


def _fast_teamtailor_description(job_url: str) -> str:
    """Teamtailor — fetch job detail via public REST API."""
    import re as _re
    import requests as _req

    # Teamtailor has a public API: GET https://{company}.teamtailor.com/api/v1/jobs/{id}
    m = _re.search(r"([\w-]+)\.teamtailor\.com/jobs/(\d+)", job_url, _re.I)
    if not m:
        m = _re.search(r"teamtailor\.com/(?:en/)?jobs/(\d+)", job_url, _re.I)

    # Try HTML extraction which is fast for Teamtailor (server-rendered)
    return _html_jd_extract(job_url, extra_selectors=["job-body", "body-text", "job-description__description"])


def _fast_zoho_description(job_url: str) -> str:
    """Zoho Recruit — extract description from job detail page."""
    return _html_jd_extract(job_url, extra_selectors=["jobs-details-description", "job-description", "jobdesc"])


def _fast_recruitee_description(job_url: str) -> str:
    """Recruitee public API — single endpoint returns all fields including description."""
    import re as _re
    import requests as _req

    m = _re.search(r"([\w-]+)\.recruitee\.com/o/([^/?#]+)", job_url, _re.I)
    if not m:
        return ""
    slug, offer_slug = m.group(1), m.group(2)
    api_url = f"https://{slug}.recruitee.com/api/offers/{offer_slug}"
    try:
        resp = _req.get(api_url, headers={"Accept": "application/json"}, timeout=10)
        if resp.ok:
            d = resp.json()
            offer = d.get("offer") or d
            desc = offer.get("description") or offer.get("requirements") or ""
            return str(desc).strip()
    except Exception:
        pass
    return _html_jd_extract(job_url)


# _fast_workday_description_v2 merged into _fast_workday_description (handles locale prefix URLs)


# Maps platform_slug → fast-fetch function. Each function returns a description
# string or "" on failure. Jarvis is the universal fallback for all platforms.
_FAST_FETCH_REGISTRY: dict = {
    "workday":         _fast_workday_description,
    "greenhouse":      _fast_greenhouse_description,
    "lever":           _fast_lever_description,
    "ashby":           _fast_ashby_description,
    "bamboohr":        _fast_bamboohr_description,
    "workable":        _fast_workable_description,
    "icims":           _fast_icims_description,
    "oracle":          _fast_oracle_description,
    "smartrecruiters": _fast_smartrecruiters_description,
    "taleo":           _fast_taleo_description,
    "ultipro":         _fast_ultipro_description,
    "dayforce":        _fast_dayforce_description,
    "jobvite":         _fast_jobvite_description,
    "breezy":          _fast_breezy_description,
    "teamtailor":      _fast_teamtailor_description,
    "zoho":            _fast_zoho_description,
    "recruitee":       _fast_recruitee_description,
    "html_scrape":     _html_jd_extract,  # generic fallback for any HTML-scrape platform
}


def _backfill_process_one_job(job, jarvis, force_jarvis: bool = False):
    """
    Fetch JD for a single RawJob row that was already claim-locked.
    Clears jd_backfill_locked_at on every exit path.
    Returns one of: ``updated``, ``skipped``, ``failed`` and a log dict.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    from .enrichments import extract_enrichments
    from .location_resolver import evaluate_rawjob_scope, extract_location_candidates
    from .models import RawJob

    fetch_url = (job.original_url or "").strip()
    platform = (job.platform_slug or "").lower()

    if platform == "smartrecruiters":
        from .smartrecruiters_support import backfill_fetch_url_for_raw_job
        fetch_url = backfill_fetch_url_for_raw_job(job) or fetch_url

    log_base = {
        "pk": job.pk,
        "title": (job.title or "")[:60],
        "company": (job.company_name or "")[:40],
        "platform": job.platform_slug or "",
        "url": (fetch_url or "")[:120],
    }

    # Platforms with reliable public JSON APIs — if the fast path returns empty it
    # means the job is expired/gone (API returned 404/empty). Skip Jarvis for these:
    # falling through to Jarvis wastes 30-60s per job for no gain.
    _API_ONLY_PLATFORMS = frozenset({
        "greenhouse", "lever", "ashby", "workable", "bamboohr",
        "workday", "smartrecruiters", "recruitee",
    })

    # Fast path: try platform-native JSON API before falling back to Jarvis scraping.
    # Platforms like Greenhouse/Lever have public APIs that return structured JSON
    # 10-50x faster than Jarvis browser-based scraping.
    fast_fn = _FAST_FETCH_REGISTRY.get(platform)
    fast_desc = ""
    fast_strategy = ""
    _fast_blocked = False  # True when CDN/WAF returned 403 — need Jarvis fallback
    if fast_fn and fetch_url and not force_jarvis:
        try:
            raw = fast_fn(fetch_url) or ""
            if raw == "__BLOCKED__":
                _fast_blocked = True
                fast_desc = ""
            else:
                fast_desc = raw
            if fast_desc:
                fast_strategy = f"{platform}_api"
        except Exception:
            fast_desc = ""

    # Cooldown lock: when a job gets no description, set a future lock so it is
    # not immediately re-queued. This prevents the infinite-retry loop where the
    # same dead/empty jobs cycle through the backfill every round.
    _COOLDOWN_HOURS = 12

    if fast_desc:
        data = {"description": fast_desc, "strategy": fast_strategy}
    elif not force_jarvis and fast_fn and platform in _API_ONLY_PLATFORMS and not _fast_blocked:
        # API-only platform: fast path returned "" (not blocked) → job is expired or gone.
        # Skip Jarvis — it will also fail and waste 30-60s. Apply cooldown lock.
        # NOTE: if _fast_blocked is True (403), we fall through to Jarvis below so it
        # can use browser-like behaviour to bypass the WAF.
        future_lock = timezone.now() + timedelta(hours=_COOLDOWN_HOURS)
        RawJob.objects.filter(pk=job.pk).update(
            description=" ", has_description=False, jd_backfill_locked_at=future_lock
        )
        log = {
            **log_base,
            "status": "skipped",
            "reason": f"API returned no description — cooldown {_COOLDOWN_HOURS}h",
            "strategy": f"{platform}_api",
        }
        return "skipped", log
    else:
        try:
            data = jarvis.ingest(fetch_url)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            logger.warning("Backfill failed for job %s: %s", job.pk, exc)
            future_lock = timezone.now() + timedelta(hours=_COOLDOWN_HOURS)
            RawJob.objects.filter(pk=job.pk).update(
                description=" ", has_description=False, jd_backfill_locked_at=future_lock
            )
            log = {**log_base, "status": "failed", "reason": str(exc)[:80]}
            return "failed", log

    desc_str = _backfill_str(data.get("description")).strip()

    if not desc_str:
        future_lock = timezone.now() + timedelta(hours=_COOLDOWN_HOURS)
        upd: dict = {"description": " ", "has_description": False, "jd_backfill_locked_at": future_lock}
        pl = {}
        if data.get("raw_payload"):
            # Prefer newest API/Jarvis payload over stale DB rows (fixes SmartRecruiters active flag).
            pl = {**(job.raw_payload or {}), **dict(data["raw_payload"])}
            upd["raw_payload"] = pl
        if isinstance(pl, dict) and pl.get("active") is False:
            upd["is_active"] = False
        RawJob.objects.filter(pk=job.pk).update(**upd)
        log = {
            **log_base,
            "status": "skipped",
            "reason": (_backfill_str(data.get("error")).strip() or "No description")[:80],
            "strategy": _backfill_str(data.get("strategy"))[:30],
        }
        return "skipped", log

    update_fields: dict = {"description": desc_str[:50000], "has_description": True, "jd_backfill_locked_at": None}

    for f, mx in (("requirements", 20000), ("responsibilities", 20000), ("benefits", 10000)):
        v = _backfill_str(data.get(f)).strip()
        if v:
            update_fields[f] = v[:mx]

    for f in ("salary_min", "salary_max"):
        v = data.get(f)
        if v is not None:
            update_fields[f] = v

    for f in ("salary_currency", "salary_period", "salary_raw"):
        v = _backfill_str(data.get(f)).strip()
        if v:
            update_fields[f] = v[:256]

    for f in ("employment_type", "experience_level"):
        v = _backfill_str(data.get(f)).strip()
        if v and v != "UNKNOWN":
            update_fields[f] = v

    for f in ("department", "city", "state", "country", "location_raw"):
        v = _backfill_str(data.get(f)).strip()
        if v:
            update_fields[f] = v[:256]

    for f, mx in (
        ("postal_code", 32),
        ("job_category", 64),
        ("education_required", 12),
        ("schedule_type", 32),
        ("shift_schedule", 128),
        ("shift_details", 255),
        ("vendor_job_identification", 128),
        ("vendor_job_category", 128),
        ("vendor_degree_level", 128),
        ("vendor_job_schedule", 128),
        ("vendor_job_shift", 128),
        ("vendor_location_block", 512),
    ):
        v = _backfill_str(data.get(f)).strip()
        if v:
            update_fields[f] = v[:mx]

    for f in ("is_remote", "location_type"):
        v = data.get(f)
        if v is not None and v not in ("", "UNKNOWN"):
            update_fields[f] = v

    if data.get("raw_payload"):
        # Prefer fresh Jarvis/API keys over older harvest list payloads (e.g. active, sections).
        merged_pl = {**(job.raw_payload or {}), **dict(data["raw_payload"])}
        update_fields["raw_payload"] = merged_pl
        if isinstance(merged_pl, dict) and merged_pl.get("active") is False:
            update_fields["is_active"] = False

    merged_payload = update_fields.get("raw_payload") or job.raw_payload or {}
    supplied_candidates = data.get("location_candidates")
    if not isinstance(supplied_candidates, list):
        supplied_candidates = []
    location_candidates = (
        supplied_candidates
        or extract_location_candidates(
            location_raw=update_fields.get("location_raw") or job.location_raw or "",
            city=update_fields.get("city") or job.city or "",
            state=update_fields.get("state") or job.state or "",
            country=update_fields.get("country") or job.country or "",
            vendor_location_block=update_fields.get("vendor_location_block") or job.vendor_location_block or "",
            raw_payload=merged_payload,
        )
    )
    if location_candidates:
        update_fields["location_candidates"] = location_candidates

    update_fields.update(extract_enrichments(build_enrichment_input(
        job,
        overrides={**update_fields, "raw_payload": merged_payload},
    )))

    for field, value in update_fields.items():
        setattr(job, field, value)
    update_fields.update(evaluate_rawjob_scope(job, use_provider=None, save=False))

    RawJob.objects.filter(pk=job.pk).update(**update_fields)
    if data.get("raw_payload"):
        try:
            from .models import RawJobPayloadSnapshot
            from .payload_archive import capture_rawjob_payload_snapshot
            capture_rawjob_payload_snapshot(
                job,
                payload=data.get("raw_payload") or {},
                raw_html=desc_meta.get("raw_html") or "",
                payload_kind=RawJobPayloadSnapshot.PayloadKind.DETAIL,
                source_url=job.original_url,
                platform_slug=job.platform_slug,
                source_metadata={
                    "ingest": "jd_backfill",
                    "strategy": _backfill_str(data.get("strategy"))[:80],
                },
            )
        except Exception:
            logger.exception("Failed to archive backfill source payload for RawJob %s", job.pk)
    log = {
        **log_base,
        "status": "updated",
        "desc_len": len(desc_str),
        "strategy": _backfill_str(data.get("strategy"))[:30],
    }
    logger.info("Backfill updated job %s (%s)", job.pk, job.title[:60])
    return "updated", log


def _backfill_descriptions_chunk_impl(
    claim_size: int,
    platform_slug: str | None,
    *,
    progress_hook=None,
    shard_index: int = 0,
    shard_count: int = 1,
    force_jarvis: bool = False,
    include_cold: bool = False,
) -> dict:
    """
    Claim up to *claim_size* rows (SKIP LOCKED on Postgres) and fetch JDs sequentially.
    Used by the Celery chunk task (standalone), the orchestrator (inline or thread pool),
    and must be safe to call from worker threads (fresh DB connections per thread).

    If *progress_hook* is set, it is invoked with
    ``(event, job=..., entry=..., lu=..., ls=..., lf=...)`` where event is
    ``"job_start"`` before HTTP fetch and ``"job_done"`` after each job so the
    orchestrator can update Celery PROGRESS every row (live UI).
    """
    import time as _time

    from celery.exceptions import SoftTimeLimitExceeded

    from .jarvis import JobJarvis
    from .models import RawJob

    updated = skipped = failed = 0
    logs: list[dict] = []

    try:
        jobs = _claim_backfill_job_batch(
            claim_size,
            platform_slug,
            shard_index=shard_index,
            shard_count=shard_count,
            include_cold=include_cold,
        )
    except Exception as exc:
        logger.exception("backfill chunk claim failed: %s", exc)
        return {"claimed": 0, "updated": 0, "skipped": 0, "failed": 0, "log": [], "error": str(exc)}

    if not jobs:
        return {"claimed": 0, "updated": 0, "skipped": 0, "failed": 0, "log": []}

    jarvis = JobJarvis()
    for idx, job in enumerate(jobs):
        if progress_hook:
            progress_hook("job_start", job=job, entry=None, lu=updated, ls=skipped, lf=failed)
        try:
            outcome, entry = _backfill_process_one_job(job, jarvis, force_jarvis=force_jarvis)
        except SoftTimeLimitExceeded:
            logger.warning("backfill chunk soft time limit at job %s", job.pk)
            RawJob.objects.filter(pk__in=[j.pk for j in jobs[idx:]]).update(
                jd_backfill_locked_at=None,
            )
            return {
                "claimed": len(jobs),
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
                "log": logs if not progress_hook else [],
                "soft_time_limit": True,
            }
        logs.append(entry)
        if outcome == "updated":
            updated += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1
        if progress_hook:
            progress_hook("job_done", job=job, entry=entry, lu=updated, ls=skipped, lf=failed)
        _time.sleep(_backfill_inter_job_delay_sec())

    return {
        "claimed": len(jobs),
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "log": logs if not progress_hook else [],
    }


@shared_task(
    name="harvest.backfill_single_rawjob_description",
    soft_time_limit=900,
    time_limit=960,
    max_retries=0,
)
def backfill_single_rawjob_description_task(raw_job_id: int, force_jarvis: bool = False) -> dict:
    """Fetch a JD for one RawJob after manual skipped-title recovery."""
    from .jarvis import JobJarvis
    from .models import RawJob

    job = RawJob.objects.filter(pk=raw_job_id).first()
    if not job:
        return {"updated": 0, "skipped": 0, "failed": 1, "error": "RawJob not found"}

    RawJob.objects.filter(pk=job.pk).update(
        is_cold=False,
        jd_fetch_skipped=False,
        filter_decision=job.filter_decision or "POSSIBLE",
        jd_backfill_locked_at=None,
    )
    outcome, entry = _backfill_process_one_job(job, JobJarvis(), force_jarvis=force_jarvis)
    fallback_task_id = ""
    if outcome != "updated" and job.platform_label_id:
        fallback = fetch_raw_jobs_for_company_task.apply_async(
            args=[job.platform_label_id],
            kwargs={"max_jobs": 25, "triggered_by": "MANUAL"},
            countdown=10,
            queue="harvest",
        )
        fallback_task_id = fallback.id
    return {
        "raw_job_id": raw_job_id,
        "updated": 1 if outcome == "updated" else 0,
        "skipped": 1 if outcome == "skipped" else 0,
        "failed": 1 if outcome not in {"updated", "skipped"} else 0,
        "fallback_company_fetch_task_id": fallback_task_id,
        "log": entry,
    }


@shared_task(
    name="harvest.backfill_descriptions_chunk",
    soft_time_limit=3600,
    time_limit=3900,
    max_retries=0,
)
def backfill_descriptions_chunk_task(
    claim_size: int,
    platform_slug: str | None,
    shard_index: int = 0,
    shard_count: int = 1,
    force_jarvis: bool = False,
):
    """Celery entry point for :func:`_backfill_descriptions_chunk_impl`."""
    return _backfill_descriptions_chunk_impl(
        claim_size,
        platform_slug,
        shard_index=shard_index,
        shard_count=shard_count,
        force_jarvis=force_jarvis,
    )


@shared_task(bind=True, name="harvest.backfill_descriptions", soft_time_limit=86400, time_limit=90000)
def backfill_descriptions_task(
    self,
    batch_size: int | None = None,
    parallel_workers: int | None = None,
    platform_slug: str | None = None,
    offset: int = 0,
    _chain_depth: int = 0,
    _skip_streak: int = 0,
    force_jarvis: bool = False,
    reset_locks: bool | None = None,
    include_cold: bool | None = None,
):
    """
    Fetch JDs for RawJobs with no description using parallel chunk workers.

    All tunable parameters fall back to HarvestEngineConfig values when not
    explicitly provided by the caller:
      batch_size       → 200 (default)
      parallel_workers → HarvestEngineConfig.backfill_jd_workers (default 4)
      reset_locks      → HarvestEngineConfig.backfill_jd_reset_locks (default True)
      include_cold     → HarvestEngineConfig.backfill_jd_include_cold (default False)

    Without ``SKIP LOCKED``, chunks use PK modulo sharding so rows are not doubled.
    """
    import time as _time

    from celery.exceptions import SoftTimeLimitExceeded

    from .models import HarvestEngineConfig, HarvestOpsRun, RawJob
    from .ops_audit import begin_ops_run, finish_ops_run

    # Resolve config defaults for any param not explicitly supplied
    cfg = HarvestEngineConfig.get()
    if batch_size is None:
        batch_size = 200
    if parallel_workers is None:
        parallel_workers = cfg.backfill_jd_workers
    if reset_locks is None:
        reset_locks = cfg.backfill_jd_reset_locks
    if include_cold is None:
        include_cold = cfg.backfill_jd_include_cold

    task_id = getattr(self.request, "id", "") or ""
    singleton_lock = _acquire_ops_singleton(
        HarvestOpsRun.Operation.BACKFILL_JD,
        task_id,
        queue={
            "batch_size": batch_size,
            "parallel_workers": parallel_workers,
            "platform_slug": platform_slug or "",
            "force_jarvis": force_jarvis,
            "reset_locks": reset_locks,
            "include_cold": include_cold,
        },
        stale_minutes=90,
        cache_ttl_seconds=3 * 60 * 60,
    )
    if singleton_lock is None:
        return {"message": "Skipped — another Backfill JD run is active.", "updated": 0}

    if offset:
        logger.warning(
            "backfill_descriptions_task: offset=%s is ignored.",
            offset,
        )

    if reset_locks:
        from django.utils import timezone as _tz
        cleared = RawJob.objects.filter(jd_backfill_locked_at__gt=_tz.now()).update(jd_backfill_locked_at=None)
        logger.info("backfill reset_locks: cleared %s cooldown locks", cleared)

    # Duplicate-run guard: Beat fires every hour; a full backfill can run for hours.
    # If another instance is already active, skip this firing instead of stacking workers.
    try:
        from celery import current_app as _capp
        _inspect = _capp.control.inspect(timeout=2)
        _active = _inspect.active() or {}
        for _worker_tasks in _active.values():
            for _t in (_worker_tasks or []):
                if (
                    _t.get("name") == "harvest.backfill_descriptions"
                    and _t.get("id") != self.request.id
                ):
                    logger.info(
                        "backfill_descriptions: instance %s already running — skipping this Beat firing.",
                        _t["id"],
                    )
                    dup_op = begin_ops_run(
                        HarvestOpsRun.Operation.BACKFILL_JD,
                        getattr(self.request, "id", "") or "",
                        queue={
                            "skipped_duplicate_instance": True,
                            "other_task_id": _t.get("id"),
                            "batch_size": batch_size,
                            "parallel_workers": parallel_workers,
                        },
                    )
                    finish_ops_run(
                        dup_op,
                        HarvestOpsRun.Status.SKIPPED,
                        {"reason": "duplicate_worker", "other_task_id": _t.get("id")},
                    )
                    _release_ops_singleton(singleton_lock, task_id)
                    return {"message": "Skipped — another backfill instance is already running.", "updated": 0}
    except Exception:
        pass  # inspect is best-effort; proceed if it fails

    claim_size = max(10, min(int(batch_size), 500))
    workers_requested = max(1, min(int(parallel_workers), BACKFILL_MAX_PARALLEL))
    parallelism = workers_requested
    parallelism_notes: list[str] = []

    use_pk_sharding = not _supports_select_for_update_skip_locked()
    if use_pk_sharding and parallelism > 1:
        parallelism_notes.append(
            "Parallel chunks use PK modulo sharding (MOD id) — safe on SQLite; "
            "PostgreSQL + SKIP LOCKED is still best for even load."
        )

    parallelism_note = " ".join(parallelism_notes)

    total = _backfill_eligible_queryset(platform_slug, include_cold=include_cold).count()
    if total == 0:
        empty_op = begin_ops_run(
            HarvestOpsRun.Operation.BACKFILL_JD,
            getattr(self.request, "id", "") or "",
            queue={
                "eligible_total": 0,
                "claim_size": claim_size,
                "parallel_workers": parallelism,
                "platform_slug": platform_slug or "",
                "force_jarvis": force_jarvis,
            },
        )
        finish_ops_run(
            empty_op,
            HarvestOpsRun.Status.SUCCESS,
            {"message": "All jobs already have descriptions.", "updated": 0},
        )
        _release_ops_singleton(singleton_lock, task_id)
        return {"message": "All jobs already have descriptions.", "updated": 0}

    updated = skipped = failed = 0
    processed = 0
    recent_log: list[dict] = []
    _LOG_MAX = 25
    _start_time = _time.monotonic()
    round_num = 0

    def _push_log(entries: list[dict]) -> None:
        nonlocal recent_log
        for e in entries:
            recent_log.append(e)
        recent_log = recent_log[-_LOG_MAX:]

    def _make_detail(
        cur_job=None,
        *,
        disp_u=None,
        disp_s=None,
        disp_f=None,
    ) -> dict:
        u = disp_u if disp_u is not None else updated
        s = disp_s if disp_s is not None else skipped
        f = disp_f if disp_f is not None else failed
        elapsed = _time.monotonic() - _start_time
        done = u + s + f
        speed = done / elapsed if elapsed > 0 else 0
        remaining = max(0, total - done)
        eta_secs = int(remaining / speed) if speed > 0 else 0
        eta_min = eta_secs // 60
        eta_hrs = eta_min // 60
        eta_str = (
            f"~{eta_hrs}h {eta_min % 60}m"
            if eta_hrs > 0
            else (f"~{eta_min}m" if eta_min > 0 else f"~{eta_secs}s")
        )
        d = {
            "updated": u,
            "skipped": s,
            "failed": f,
            "remaining_global": remaining,
            "batch_num": round_num,
            "parallel_workers": parallelism,
            "workers_requested": workers_requested,
            "claim_size": claim_size,
            "parallelism_note": parallelism_note,
            "speed": round(speed, 1),
            "elapsed_secs": int(elapsed),
            "eta": eta_str,
            "log": list(recent_log),
        }
        if cur_job is not None:
            d["current_job"] = {
                "pk": cur_job.pk,
                "title": (cur_job.title or "")[:60],
                "company": (cur_job.company_name or "")[:40],
                "platform": cur_job.platform_slug or "",
                "url": (cur_job.original_url or "")[:200],
            }
        return d

    start_msg = (
        f"{'[DEEP SCAN] ' if force_jarvis else ''}Starting — {total} jobs — {parallelism} worker(s) × {claim_size} rows/chunk"
    )
    if workers_requested != parallelism:
        start_msg += f" (you asked for {workers_requested})"
    if parallelism_note:
        start_msg += f" — {parallelism_note}"

    update_task_progress(
        self,
        current=0,
        total=total,
        message=start_msg,
        detail=_make_detail(),
    )

    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.BACKFILL_JD,
        getattr(self.request, "id", "") or "",
        progress_total=total,
        queue={
            "eligible_total": total,
            "claim_size": claim_size,
            "parallel_workers": parallelism,
            "workers_requested": workers_requested,
            "platform_slug": platform_slug or "",
            "force_jarvis": force_jarvis,
            "reset_locks": reset_locks,
            "use_pk_sharding": use_pk_sharding,
        },
    )
    tick_ops_run_progress(ops_run, 0, total, start_msg, force=True)

    try:
        while True:
            round_num += 1
            base_u, base_s, base_f = updated, skipped, failed

            if parallelism == 1:

                def _inline_progress(event, job=None, entry=None, lu=0, ls=0, lf=0):
                    nonlocal recent_log
                    if event == "job_done" and entry is not None:
                        recent_log.append(entry)
                        recent_log = recent_log[-_LOG_MAX:]
                    du, ds, df = base_u + lu, base_s + ls, base_f + lf
                    done = du + ds + df
                    cur = job
                    msg = (
                        f"Fetching: {(job.title or 'Untitled')[:50]} ({job.platform_slug or '?'})…"
                        if event == "job_start" and job is not None
                        else (
                            f"Progress — {du} updated, {ds} skipped, {df} failed"
                            if event == "job_done"
                            else "…"
                        )
                    )
                    update_task_progress(
                        self,
                        current=min(done, total),
                        total=total,
                        message=msg,
                        detail=_make_detail(
                            cur,
                            disp_u=du,
                            disp_s=ds,
                            disp_f=df,
                        ),
                    )

                chunk_results = [
                    _backfill_descriptions_chunk_impl(
                        claim_size,
                        platform_slug,
                        progress_hook=_inline_progress,
                        shard_index=0,
                        shard_count=1,
                        force_jarvis=force_jarvis,
                    )
                ]
            else:
                # Parallel chunks run in a thread pool inside this task. Do not use
                # Celery group()+Result.get() here — Celery forbids blocking on subtask
                # results from within a task (RuntimeError), and join_native can still
                # call nested .get() with unsafe defaults.
                from concurrent.futures import ThreadPoolExecutor

                from django.db import close_old_connections

                def _one_shard(shard_index: int, shard_count: int) -> dict:
                    close_old_connections()
                    try:
                        return _backfill_descriptions_chunk_impl(
                            claim_size,
                            platform_slug,
                            shard_index=shard_index,
                            shard_count=shard_count,
                            force_jarvis=force_jarvis,
                        )
                    finally:
                        close_old_connections()

                if _supports_select_for_update_skip_locked():
                    shard_specs = [(0, 1)] * parallelism
                else:
                    shard_specs = [(i, parallelism) for i in range(parallelism)]

                with ThreadPoolExecutor(max_workers=parallelism) as pool:
                    futures = [pool.submit(_one_shard, si, sc) for si, sc in shard_specs]
                    chunk_results = [fut.result() for fut in futures]

            round_claimed = 0
            for cr in chunk_results:
                if not isinstance(cr, dict):
                    continue
                round_claimed += int(cr.get("claimed") or 0)
                updated += int(cr.get("updated") or 0)
                skipped += int(cr.get("skipped") or 0)
                failed += int(cr.get("failed") or 0)
                _push_log(cr.get("log") or [])

            processed = updated + skipped + failed

            last_job = None
            if recent_log:
                last_pk = recent_log[-1].get("pk")
                if last_pk:
                    last_job = RawJob.objects.filter(pk=last_pk).first()

            _bf_msg = (
                f"Round {round_num} — {parallelism} workers — "
                f"{updated} updated, {skipped} skipped, {failed} failed "
                f"(claimed {round_claimed} this round)"
            )
            update_task_progress(
                self,
                current=processed,
                total=total,
                message=_bf_msg,
                detail=_make_detail(last_job),
            )
            tick_ops_run_progress(ops_run, processed, total, _bf_msg)

            if round_claimed == 0:
                break

    except SoftTimeLimitExceeded:
        logger.warning(
            "Backfill orchestrator soft time limit — processed %s jobs",
            processed,
        )
        completion = {
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
            "remaining": _backfill_eligible_queryset(platform_slug, include_cold=include_cold).count(),
            "parallel_workers": parallelism,
            "soft_time_limit": True,
        }
        finish_ops_run(ops_run, HarvestOpsRun.Status.PARTIAL, completion)
        _release_ops_singleton(singleton_lock, task_id)
        return completion

    except Exception as e:
        logger.exception("backfill_descriptions_task failed: %s", e)
        finish_ops_run(
            ops_run,
            HarvestOpsRun.Status.FAILED,
            {
                "error": str(e)[:500],
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
            },
        )
        _release_ops_singleton(singleton_lock, task_id)
        raise

    remaining_n = _backfill_eligible_queryset(platform_slug, include_cold=include_cold).count()
    result = {
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "total_processed": processed,
        "remaining": remaining_n,
        "parallel_workers": parallelism,
        "claim_size": claim_size,
        "chained_next": False,
    }
    logger.info("Backfill descriptions FINISHED: %s", result)
    finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, result)
    _release_ops_singleton(singleton_lock, task_id)
    return result


# ─── Enrich Existing Jobs (no HTTP) ──────────────────────────────────────────

@shared_task(bind=True, name="harvest.enrich_existing_jobs")
def enrich_existing_jobs_task(
    self,
    batch_size: int = 2000,
    platform_slug: str | None = None,
    only_unenriched: bool = True,
    offset: int = 0,
):
    """
    Run extract_enrichments() on jobs already in the DB — no HTTP calls.

    Perfect for:
      - Jobs that already have descriptions (Greenhouse, Lever ~11k)
      - After a schema update adds new enrichment fields
      - Re-enriching all jobs after improving the extractor

    `only_unenriched=True`  → skips jobs that already have skills/category set
    `only_unenriched=False` → re-runs on every job (full re-enrich)

    Processes `batch_size` jobs per run at ~1000 jobs/sec (pure Python, no I/O).
    Safe to run multiple times.
    """
    from .enrichments import extract_enrichments
    from .models import RawJob

    # Scoped harvest gate: enrich only PRIORITY (target-country) jobs.
    # Cold + unknown country jobs remain as cheap inventory.
    qs = RawJob.objects.filter(is_priority=True)
    if platform_slug:
        qs = qs.filter(platform_slug=platform_slug)
    if only_unenriched:
        # Re-enrich if: no skills yet, no category yet, OR category_confidence never computed
        # (covers the 106k jobs enriched before v3 that never got category_confidence)
        from django.db.models import Q
        qs = qs.filter(
            Q(skills=[], job_category="") |
            Q(category_confidence__isnull=True) |
            ~Q(enrichment_version=CURRENT_ENRICHMENT_VERSION) |
            ~Q(domain_version=CURRENT_DOMAIN_VERSION)
        )

    total = qs.count()
    if total == 0:
        return {"message": "Nothing to enrich.", "updated": 0}

    update_task_progress(self, current=0, total=total,
                         message=f"Found {total:,} jobs to enrich…")

    updated = skipped = 0
    jobs = list(qs.order_by("id")[offset: offset + batch_size])

    # Bulk-update in chunks of 500 for efficiency
    CHUNK = 500
    bulk_updates: list[RawJob] = []

    ENRICH_FIELDS = [
        "skills", "tech_stack", "job_category",
        "normalized_title", "title_keywords",
        "years_required", "years_required_max", "education_required",
        "visa_sponsorship", "work_authorization", "clearance_required", "clearance_level",
        "salary_equity", "signing_bonus", "relocation_assistance",
        "travel_required", "travel_pct_min", "travel_pct_max",
        "schedule_type", "shift_schedule", "shift_details", "hours_hint", "weekend_required",
        "certifications", "licenses_required", "benefits_list",
        "languages_required", "encouraged_to_apply",
        "job_keywords", "department_normalized",
        "word_count", "quality_score", "jd_quality_score",
        "classification_confidence", "category_confidence",
        "classification_source", "enrichment_version",
        "classification_provenance",
        "field_confidence", "field_provenance",
        "resume_ready_score", "description_clean", "description_raw_html",
        "has_html_content", "cleaning_version",
        # Section extraction
        "requirements", "responsibilities",
        # Domain taxonomy
        "job_domain", "domain_version",
    ]

    for idx, job in enumerate(jobs, start=1):
        enriched = extract_enrichments(build_enrichment_input(job))

        has_change = False
        for field in ENRICH_FIELDS:
            val = enriched.get(field)
            current = getattr(job, field, None)
            # Skip if no new value
            if val in (None, [], "", 0):
                continue
            if val != current:
                setattr(job, field, val)
                has_change = True

        if has_change:
            bulk_updates.append(job)
            updated += 1
        else:
            skipped += 1

        # Flush chunk
        if len(bulk_updates) >= CHUNK:
            RawJob.objects.bulk_update(bulk_updates, ENRICH_FIELDS)
            bulk_updates.clear()

        if idx % 100 == 0:
            update_task_progress(
                self,
                current=idx,
                total=len(jobs),
                message=f"Enriched {updated:,} / {idx:,} processed…",
            )

    # Flush remainder
    if bulk_updates:
        RawJob.objects.bulk_update(bulk_updates, ENRICH_FIELDS)

    result = {
        "updated":         updated,
        "skipped":         skipped,
        "total_processed": len(jobs),
        "total_eligible":  total,
        "remaining":       max(0, total - (offset + len(jobs))),
    }
    logger.info("Enrich existing jobs complete: %s", result)
    return result


@shared_task(bind=True, name="harvest.backfill_resume_contract")
def backfill_resume_contract_task(
    self,
    batch_size: int = 1500,
    offset: int = 0,
):
    """
    Backfill new resume-classification contract fields for historical rows.
    Safe to run repeatedly and in chunks.
    """
    from .enrichments import clean_job_content, extract_enrichments, normalize_job_title
    from .models import RawJob

    qs = RawJob.objects.select_related("company").order_by("pk")
    total = qs.count()
    if total == 0:
        return {"message": "No RawJobs found.", "updated": 0}

    jobs = list(qs[offset: offset + batch_size])
    if not jobs:
        return {"message": "No jobs in requested chunk.", "updated": 0, "remaining": 0}

    update_task_progress(
        self,
        current=0,
        total=len(jobs),
        message=f"Backfilling resume contract ({len(jobs)} jobs)…",
    )

    updated = 0
    CHUNK = 300
    bulk_updates: list[RawJob] = []
    update_fields = [
        "description_clean",
        "description_raw_html",
        "has_html_content",
        "cleaning_version",
        "normalized_title",
        "title_keywords",
        "schedule_type",
        "shift_details",
        "hours_hint",
        "weekend_required",
        "clearance_level",
        "travel_pct_min",
        "travel_pct_max",
        "licenses_required",
        "jd_quality_score",
        "classification_confidence",
        "classification_provenance",
        "field_confidence",
        "field_provenance",
        "resume_ready_score",
        "company_industry",
        "company_stage",
        "company_funding",
        "company_size",
        "company_employee_count_band",
        "company_founding_year",
    ]

    for idx, job in enumerate(jobs, start=1):
        desc_meta = clean_job_content(job.description or "", max_len=50000)
        enriched = extract_enrichments(build_enrichment_input(job))
        company = job.company
        job.description_clean = (enriched.get("description_clean") or desc_meta["clean_text"] or "")[:50000]
        job.description_raw_html = (enriched.get("description_raw_html") or desc_meta["raw_html"] or "")[:120000]
        job.has_html_content = bool(enriched.get("has_html_content", desc_meta["has_html_content"]))
        job.cleaning_version = (enriched.get("cleaning_version") or "v2")[:20]
        job.normalized_title = (enriched.get("normalized_title") or normalize_job_title(job.title or ""))[:255]
        job.title_keywords = enriched.get("title_keywords") or job.title_keywords or []
        job.schedule_type = (enriched.get("schedule_type") or job.schedule_type or "")[:32]
        job.shift_details = (enriched.get("shift_details") or job.shift_details or "")[:255]
        job.hours_hint = (enriched.get("hours_hint") or job.hours_hint or "")[:64]
        job.weekend_required = enriched.get("weekend_required", job.weekend_required)
        job.clearance_level = (enriched.get("clearance_level") or job.clearance_level or "")[:64]
        job.travel_pct_min = enriched.get("travel_pct_min", job.travel_pct_min)
        job.travel_pct_max = enriched.get("travel_pct_max", job.travel_pct_max)
        job.licenses_required = enriched.get("licenses_required") or job.licenses_required or []
        job.jd_quality_score = enriched.get("jd_quality_score", job.jd_quality_score)
        job.classification_confidence = enriched.get("classification_confidence", job.classification_confidence)
        job.classification_provenance = enriched.get("classification_provenance") or job.classification_provenance or {}
        job.field_confidence = enriched.get("field_confidence") or job.field_confidence or {}
        job.field_provenance = enriched.get("field_provenance") or job.field_provenance or {}
        job.resume_ready_score = enriched.get("resume_ready_score", job.resume_ready_score)
        job.company_industry = ((job.company_industry or "") or (company.industry if company else "") or "")[:255]
        job.company_stage = ((job.company_stage or "") or (company.funding_stage if company else "") or "")[:64]
        job.company_funding = ((job.company_funding or "") or (company.funding_amount if company else "") or "")[:128]
        job.company_size = ((job.company_size or "") or (company.size_band if company else "") or (company.headcount_range if company else "") or "")[:64]
        job.company_employee_count_band = ((job.company_employee_count_band or "") or (company.employee_count_band if company else "") or (company.headcount_range if company else "") or "")[:64]
        job.company_founding_year = job.company_founding_year or (company.founding_year if company else None)
        bulk_updates.append(job)
        updated += 1

        if len(bulk_updates) >= CHUNK:
            RawJob.objects.bulk_update(bulk_updates, update_fields)
            bulk_updates.clear()

        if idx % 100 == 0:
            update_task_progress(
                self,
                current=idx,
                total=len(jobs),
                message=f"Backfilled {idx}/{len(jobs)} jobs…",
            )

    if bulk_updates:
        RawJob.objects.bulk_update(bulk_updates, update_fields)

    _invalidate_rawjobs_dashboard_cache()
    processed = len(jobs)
    remaining = max(0, total - (offset + processed))
    result = {
        "updated": updated,
        "total_processed": processed,
        "total_eligible": total,
        "remaining": remaining,
        "next_offset": offset + processed,
    }
    logger.info("Backfill resume contract complete: %s", result)
    return result


@shared_task(
    bind=True,
    name="harvest.run_duplicate_detection",
    soft_time_limit=3600,
    time_limit=4000,
    max_retries=0,
)
def run_duplicate_detection_task(self, limit: int = 5000, company_slug: str = ""):
    """
    Background Celery task for duplicate detection.
    Runs chunked + throttled — will NOT spike CPU or block web workers.
    """
    from .duplicate_engine import run_detection
    logger.info("Duplicate detection task started (limit=%d, company=%s)", limit, company_slug or "all")
    result = run_detection(
        limit=limit,
        company_slug=company_slug,
        skip_existing=True,
        company_chunk_size=10,        # 10 companies per micro-batch
        sleep_between_chunks=0.5,     # 500ms sleep between batches — never pegs CPU
        max_jobs_per_company=40,      # hard cap: O(40²/2)=780 pairs max per company
    )
    logger.info("Duplicate detection task finished: %s", result)
    return result


@shared_task(bind=True, name="harvest.backfill_job_marketing_roles", max_retries=0)
def backfill_job_marketing_roles_task(
    self,
    batch_size: int = 500,
    overwrite: bool = True,
):
    """
    Celery task: re-assign marketing roles for every SYNCED RawJob's Job.

    Called from the "Re-assign All Jobs" button on the Marketing Roles page.
    Safe to run repeatedly — preserves manually-assigned roles, only replaces
    auto-detected ones. With overwrite=True (default) it refreshes all jobs
    even if they already have roles (so a newly-added MarketingRole is applied).

    Progress is visible in the Live Ops Monitor.
    """
    from .models import HarvestOpsRun, RawJob
    from .ops_audit import begin_ops_run, finish_ops_run
    from jobs.models import Job
    from jobs.marketing_role_routing import (
        assign_marketing_roles_to_job,
        clear_marketing_role_cache,
    )

    # Bust cache first so the freshest roles are used throughout this run
    clear_marketing_role_cache()

    qs = (
        RawJob.objects
        .filter(sync_status=RawJob.SyncStatus.SYNCED)
        .order_by("pk")
    )
    total = qs.count()

    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.BACKFILL_ROLES,
        getattr(self.request, "id", "") or "",
        queue={"batch_size": batch_size, "overwrite": overwrite, "total": total},
    )

    assigned = skipped = missing_job = 0
    processed = 0
    last_pk = None

    while True:
        page_qs = qs if last_pk is None else qs.filter(pk__gt=last_pk)
        batch = list(page_qs[:batch_size])
        if not batch:
            break
        last_pk = batch[-1].pk

        for rj in batch:
            processed += 1
            job: Job | None = (
                Job.objects.filter(source_raw_job=rj).first()
                or (Job.objects.filter(url_hash=rj.url_hash).first() if rj.url_hash else None)
            )
            if not job:
                missing_job += 1
                continue

            if not overwrite and job.marketing_roles.exists():
                skipped += 1
                continue

            if assign_marketing_roles_to_job(job, raw_job=rj):
                assigned += 1

        # Report progress to Live Ops Monitor
        msg = f"Processed {processed:,}/{total:,} — assigned: {assigned:,}"
        HarvestOpsRun.objects.filter(pk=ops_run.pk).update(
            progress_current=processed,
            progress_total=total,
            progress_message=msg[:256],
        )
        try:
            self.update_state(state="PROGRESS", meta={"current": processed, "total": total, "message": msg})
        except Exception:
            pass

    completion = {"assigned": assigned, "skipped": skipped, "missing_job": missing_job, "processed": processed}
    finish_ops_run(ops_run, completion=completion)
    logger.info("backfill_job_marketing_roles_task done: %s", completion)
    return completion


@shared_task(bind=True, name="harvest.reclassify_stale_rawjobs", max_retries=0)
def reclassify_stale_rawjobs_task(self, batch_size: int = 1000):
    """Re-run the title filter over RawJobs whose decisions predate the current
    phrase bank — queued from the Selective Filter page's 'Apply to existing'
    button so phrase edits take effect without a shell."""
    from django.core.management import call_command
    from .models import HarvestOpsRun
    from .ops_audit import begin_ops_run, finish_ops_run

    run = begin_ops_run(
        HarvestOpsRun.Operation.CLASSIFY,
        getattr(self.request, "id", "") or "",
        queue={"source": "role_studio_apply", "batch_size": batch_size},
    )
    try:
        call_command("reclassify_stale_rawjobs", batch_size=batch_size)
        finish_ops_run(run, HarvestOpsRun.Status.SUCCESS, {"batch_size": batch_size})
        return {"ok": True}
    except Exception as e:
        finish_ops_run(run, HarvestOpsRun.Status.FAILED, {"error": str(e)[:300]})
        raise

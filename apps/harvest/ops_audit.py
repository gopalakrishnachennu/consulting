"""Structured audit rows + grep-friendly logs for non-batch harvest/jobs ops."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db.models import Q
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import HarvestOpsRun

logger = logging.getLogger(__name__)

# Monotonic timestamps for tick throttling (per ops_run pk) — process-local only.
# Sufficient: tick writes are best-effort; we only need one write per 3 s per run.
_TICK_LAST: dict[int, float] = {}
_TICK_MIN_INTERVAL = 3.0  # seconds between DB writes per run
DEFAULT_STALE_HEARTBEAT_MINUTES = 30


def tick_ops_run_progress(
    run: HarvestOpsRun | None,
    current: int,
    total: int,
    message: str = "",
    *,
    force: bool = False,
) -> None:
    """
    Write DB-backed progress to ``HarvestOpsRun`` so the Live Ops panel
    can display accurate progress that survives page refresh.

    Throttled to one DB UPDATE per run per ``_TICK_MIN_INTERVAL`` seconds to
    avoid hammering the DB inside tight loops. Pass ``force=True`` at the
    start and end of a task to always write those bookend states.
    """
    if run is None:
        return
    now = time.monotonic()
    last = _TICK_LAST.get(run.pk, 0.0)
    if not force and (now - last) < _TICK_MIN_INTERVAL:
        return
    _TICK_LAST[run.pk] = now
    try:
        heartbeat_at = timezone.now()
        HarvestOpsRun.objects.filter(pk=run.pk).update(
            progress_current=max(0, int(current)),
            progress_total=max(0, int(total)),
            progress_message=(message or "")[:256],
            last_heartbeat_at=heartbeat_at,
        )
        # Keep local copy consistent so callers can read run.progress_* directly.
        run.progress_current = max(0, int(current))
        run.progress_total = max(0, int(total))
        run.progress_message = (message or "")[:256]
        run.last_heartbeat_at = heartbeat_at
    except Exception:
        logger.debug("tick_ops_run_progress: DB write skipped (run=%s)", run.pk)


# Hard ceiling: no harvest op legitimately runs this long. A hung worker that keeps
# heartbeating ("zombie") would otherwise hold the duplicate guard forever — this is
# what produced "scheduler skipped (duplicate guard): backfill jd ×60".
MAX_OPS_RUNTIME_MINUTES = 360  # 6 hours


def mark_stale_running_ops(
    operation: str | None = None,
    *,
    exclude_operations: list[str] | tuple[str, ...] | set[str] | None = None,
    stale_after_minutes: int = DEFAULT_STALE_HEARTBEAT_MINUTES,
    max_runtime_minutes: int = MAX_OPS_RUNTIME_MINUTES,
    reason: str = "heartbeat_stale",
) -> int:
    """Downgrade orphaned RUNNING ops so monitors and duplicate guards stay truthful.

    Two rules: (a) heartbeat older than stale_after_minutes — dead worker;
    (b) total runtime past max_runtime_minutes — zombie worker that still heartbeats.
    """
    now_ts = timezone.now()
    cutoff = now_ts - timedelta(minutes=max(1, int(stale_after_minutes)))
    runtime_ceiling = now_ts - timedelta(minutes=max(1, int(max_runtime_minutes)))
    qs = HarvestOpsRun.objects.filter(status=HarvestOpsRun.Status.RUNNING)
    if operation:
        qs = qs.filter(operation=operation)
    if exclude_operations:
        qs = qs.exclude(operation__in=list(exclude_operations))
    qs = qs.filter(
        Q(last_heartbeat_at__lt=cutoff)
        | Q(last_heartbeat_at__isnull=True, created_at__lt=cutoff)
        | Q(created_at__lt=runtime_ceiling)
    ).order_by("created_at")

    marked = 0
    now = timezone.now()
    for run in qs[:100]:
        runtime_exceeded = run.created_at < runtime_ceiling
        payload = dict(run.audit_payload or {})
        payload["stale"] = {
            "marked_at": now.isoformat(),
            "reason": ("max_runtime_exceeded" if runtime_exceeded else reason),
            "stale_after_minutes": max(1, int(stale_after_minutes)),
            "max_runtime_minutes": max(1, int(max_runtime_minutes)),
            "last_heartbeat_at": run.last_heartbeat_at.isoformat() if run.last_heartbeat_at else "",
        }
        run.audit_payload = payload
        run.status = HarvestOpsRun.Status.PARTIAL
        run.finished_at = now
        run.progress_message = (
            "Marked partial: exceeded max runtime (zombie guard); safe to re-run."
            if runtime_exceeded else
            "Marked partial: no worker heartbeat; safe to re-run."
        )
        run.save(update_fields=["audit_payload", "status", "finished_at", "progress_message"])
        marked += 1
    if marked:
        logger.warning(
            "Marked %s stale harvest ops run(s) as PARTIAL operation=%s reason=%s",
            marked,
            operation or "*",
            reason,
        )
    return marked


def mark_stale_fetch_batches(
    *,
    stale_after_minutes: int = 120,
    reason: str = "fetch_heartbeat_stale",
) -> dict:
    """Move orphaned FetchBatch/CompanyFetchRun rows out of RUNNING state.

    Company fetches do not stream DB heartbeats, so this uses a conservative
    age threshold. Stale child rows are marked SKIPPED, not FAILED, so Resume
    can re-queue those companies from the checkpoint.
    """
    from .models import CompanyFetchRun, FetchBatch

    stale_minutes = max(30, int(stale_after_minutes))
    cutoff = timezone.now() - timedelta(minutes=stale_minutes)
    now = timezone.now()

    stale_child_qs = CompanyFetchRun.objects.filter(
        batch__isnull=False,
        batch__status__in=[FetchBatch.Status.RUNNING, FetchBatch.Status.PENDING],
        status=CompanyFetchRun.Status.RUNNING,
    ).filter(
        Q(started_at__lt=cutoff)
        | Q(started_at__isnull=True, batch__created_at__lt=cutoff)
    )
    stale_batch_ids = set(stale_child_qs.values_list("batch_id", flat=True))
    child_count = stale_child_qs.update(
        status=CompanyFetchRun.Status.SKIPPED,
        completed_at=now,
        error_type=CompanyFetchRun.ErrorType.TIMEOUT,
        issue_code=CompanyFetchRun.IssueCode.FETCH_TIMEOUT,
        error_message=(
            f"Marked skipped by stale fetch-batch guard after {stale_minutes} minutes; "
            "safe to retry via Resume."
        ),
    )

    stale_batch_qs = FetchBatch.objects.filter(
        status__in=[FetchBatch.Status.RUNNING, FetchBatch.Status.PENDING],
    ).filter(
        Q(started_at__lt=cutoff)
        | Q(started_at__isnull=True, created_at__lt=cutoff)
        | Q(pk__in=stale_batch_ids)
    ).order_by("created_at")

    batch_count = 0
    for batch in stale_batch_qs[:100]:
        has_recent_child = CompanyFetchRun.objects.filter(
            batch=batch,
            status=CompanyFetchRun.Status.RUNNING,
            started_at__gte=cutoff,
        ).exists()
        if has_recent_child:
            continue
        payload = dict(batch.audit_payload or {})
        payload["stale"] = {
            "marked_at": now.isoformat(),
            "reason": reason,
            "stale_after_minutes": stale_minutes,
            "stale_child_runs_marked": child_count,
        }
        batch.audit_payload = payload
        batch.status = FetchBatch.Status.PARTIAL
        batch.completed_at = now
        batch.save(update_fields=["audit_payload", "status", "completed_at"])
        batch_count += 1

    if child_count or batch_count:
        logger.warning(
            "Marked stale fetch work child_runs=%s batches=%s reason=%s",
            child_count,
            batch_count,
            reason,
        )
    return {"company_runs": child_count, "batches": batch_count}


def active_ops_run_exists(
    operation: str,
    *,
    exclude_task_id: str = "",
    stale_after_minutes: int = DEFAULT_STALE_HEARTBEAT_MINUTES,
) -> bool:
    """Return True only for RUNNING ops that still have a fresh heartbeat."""
    mark_stale_running_ops(
        operation,
        stale_after_minutes=stale_after_minutes,
        reason="singleton_preflight",
    )
    qs = HarvestOpsRun.objects.filter(
        operation=operation,
        status=HarvestOpsRun.Status.RUNNING,
    )
    if exclude_task_id:
        qs = qs.exclude(celery_task_id=(exclude_task_id or "")[:128])
    return qs.exists()


def begin_ops_run(
    operation: str,
    celery_task_id: str,
    *,
    user_id: int | None = None,
    queue: dict | None = None,
    progress_total: int = 0,
) -> HarvestOpsRun:
    user = None
    now = timezone.now()
    if user_id:
        User = get_user_model()
        user = User.objects.filter(pk=user_id).first()
    payload = {"queue": queue or {}}
    run = HarvestOpsRun.objects.create(
        operation=operation,
        celery_task_id=(celery_task_id or "")[:128],
        status=HarvestOpsRun.Status.RUNNING,
        audit_payload=payload,
        triggered_by_user=user,
        progress_total=max(0, int(progress_total)),
        progress_current=0,
        progress_message="Starting…",
        last_heartbeat_at=now,
    )
    logger.info(
        "[HARVEST_AUDIT ops_queue] operation=%s ops_run_id=%s celery_id=%s queue=%s",
        operation,
        run.pk,
        celery_task_id or "",
        payload["queue"],
    )
    return run


def finish_ops_run(
    run: HarvestOpsRun | None,
    status: str,
    completion: dict | None = None,
) -> None:
    if run is None:
        return
    merged = dict(run.audit_payload or {})
    merged["completion"] = completion or {}
    run.audit_payload = merged
    run.status = status
    run.finished_at = timezone.now()
    run.last_heartbeat_at = run.finished_at
    # Snap progress to final state so UI shows 100% / 0% correctly on refresh
    total = run.progress_total or 0
    if status == HarvestOpsRun.Status.SUCCESS and total:
        run.progress_current = total
    elif status in (HarvestOpsRun.Status.FAILED, HarvestOpsRun.Status.SKIPPED):
        pass  # leave as-is (partial progress visible)
    run.save(update_fields=["audit_payload", "status", "finished_at", "progress_current", "last_heartbeat_at"])
    logger.info(
        "[HARVEST_AUDIT ops_done] operation=%s ops_run_id=%s status=%s celery_id=%s completion=%s",
        run.operation,
        run.pk,
        status,
        run.celery_task_id or "",
        merged.get("completion"),
    )

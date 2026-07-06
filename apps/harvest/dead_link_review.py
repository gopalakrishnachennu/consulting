from __future__ import annotations

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def raw_job_requires_admin_review(raw_job) -> bool:
    """Pipeline-touching raw rows need admin sign-off before purge."""
    from harvest.models import RawJob

    if raw_job.sync_status == RawJob.SyncStatus.SYNCED:
        return True

    from jobs.models import Job

    if Job.objects.filter(
        source_raw_job=raw_job,
        is_archived=False,
        status__in=[Job.Status.OPEN, Job.Status.POOL],
    ).exists():
        return True

    from submissions.models import ApplicationSubmission

    if ApplicationSubmission.objects.filter(job__source_raw_job=raw_job).exists():
        return True

    return False


def raw_job_auto_purge_eligible(raw_job, *, pending_review_exists: bool | None = None) -> bool:
    """Whether nightly cleanup may hard-delete this inactive raw row."""
    if raw_job.is_active:
        return False
    if raw_job_requires_admin_review(raw_job):
        if pending_review_exists is None:
            from harvest.models import DeadLinkReviewItem

            pending_review_exists = DeadLinkReviewItem.objects.filter(
                raw_job=raw_job,
                status=DeadLinkReviewItem.Status.PENDING,
            ).exists()
        if pending_review_exists:
            return False
        # Synced / pipeline rows without a pending item still require explicit purge approval.
        return False
    return True


def _primary_linked_job(raw_job):
    from jobs.models import Job

    return (
        Job.objects.filter(
            source_raw_job=raw_job,
            is_archived=False,
            status__in=[Job.Status.OPEN, Job.Status.POOL],
        )
        .order_by("status", "id")
        .first()
    )


def flag_dead_raw_jobs_for_review(raw_job_ids: list[int], *, checked_at=None) -> int:
    """Create or refresh pending admin review rows for definitive dead links."""
    if not raw_job_ids:
        return 0

    from harvest.models import DeadLinkReviewItem, RawJob
    from submissions.models import ApplicationSubmission

    checked_at = checked_at or timezone.now()
    flagged = 0

    submission_counts = {
        row["job__source_raw_job_id"]: row["c"]
        for row in ApplicationSubmission.objects.filter(
            job__source_raw_job_id__in=raw_job_ids,
        )
        .values("job__source_raw_job_id")
        .annotate(c=Count("id"))
    }

    raw_jobs = RawJob.objects.filter(id__in=raw_job_ids, is_active=False)
    for raw in raw_jobs:
        if not raw_job_requires_admin_review(raw):
            continue
        payload = raw.raw_payload if isinstance(raw.raw_payload, dict) else {}
        link_health = payload.get("link_health") or {}
        if (link_health.get("state") or "").upper() != "DEAD":
            continue

        linked = _primary_linked_job(raw)
        checked_raw = (link_health.get("checked_at") or "").strip()
        link_checked_at = parse_datetime(checked_raw) if checked_raw else checked_at

        item, _created = DeadLinkReviewItem.objects.update_or_create(
            raw_job=raw,
            defaults={
                "linked_job": linked,
                "status": DeadLinkReviewItem.Status.PENDING,
                "link_health_reason": (link_health.get("reason") or "")[:120],
                "link_health_state": (link_health.get("state") or "DEAD")[:16],
                "link_checked_at": link_checked_at,
                "submission_count": int(submission_counts.get(raw.id, 0)),
                "flagged_at": checked_at,
                "reviewed_at": None,
                "reviewed_by": None,
                "review_note": "",
            },
        )
        if item.status == DeadLinkReviewItem.Status.PENDING:
            flagged += 1
    return flagged


def _archive_linked_jobs_for_raw(raw_job, *, actor, note: str, task_name: str) -> list[int]:
    from harvest.tasks import _archive_active_job
    from jobs.models import Job

    archived_ids: list[int] = []
    jobs = Job.objects.filter(
        source_raw_job=raw_job,
        is_archived=False,
        status__in=[Job.Status.OPEN, Job.Status.POOL],
    )
    for job in jobs:
        _archive_active_job(
            job,
            reason_code="dead_link",
            reason_detail=(note or "Posting link confirmed dead.")[:2000],
            task_name=task_name,
            meta={"raw_job_id": raw_job.id, "actor_id": getattr(actor, "id", None)},
        )
        job.possibly_filled = True
        job.save(update_fields=["possibly_filled", "updated_at"])
        archived_ids.append(job.id)
    return archived_ids


def dismiss_dead_link_review(item, *, actor, note: str = "") -> None:
    """False positive — restore monitoring without deleting storage."""
    from jobs.link_health import JOB_LINK_HEALTH_UPDATE_FIELDS, apply_link_health_payload_to_job
    from jobs.models import Job

    raw = item.raw_job
    raw.is_active = True
    raw.save(update_fields=["is_active", "updated_at"])

    for job in Job.objects.filter(source_raw_job=raw, is_archived=False):
        job.possibly_filled = False
        job.original_link_is_live = True
        job.original_link_health = Job.LinkHealthState.INCONCLUSIVE
        job.save(update_fields=JOB_LINK_HEALTH_UPDATE_FIELDS)

    now = timezone.now()
    item.status = item.Status.DISMISSED
    item.reviewed_at = now
    item.reviewed_by = actor
    item.review_note = (note or "")[:500]
    item.save(
        update_fields=["status", "reviewed_at", "reviewed_by", "review_note", "updated_at"],
    )


def archive_dead_link_review(item, *, actor, note: str = "") -> dict:
    """Archive linked jobs but keep the raw row for audit."""
    raw = item.raw_job
    archived_ids = _archive_linked_jobs_for_raw(
        raw,
        actor=actor,
        note=note,
        task_name="dead_link_review.archive",
    )
    now = timezone.now()
    item.status = item.Status.ARCHIVED
    item.reviewed_at = now
    item.reviewed_by = actor
    item.review_note = (note or "")[:500]
    item.save(
        update_fields=["status", "reviewed_at", "reviewed_by", "review_note", "updated_at"],
    )
    return {"archived_job_ids": archived_ids, "raw_job_id": raw.id}


def purge_dead_link_review(item, *, actor, note: str = "") -> dict:
    """Archive linked jobs, then delete raw row (+ payload snapshots)."""
    raw = item.raw_job
    snapshot_count = raw.payload_snapshots.count()
    archived_ids = _archive_linked_jobs_for_raw(
        raw,
        actor=actor,
        note=note,
        task_name="dead_link_review.purge",
    )
    raw_id = raw.id
    raw.delete()
    return {
        "raw_job_id": raw_id,
        "archived_job_ids": archived_ids,
        "snapshots_deleted": snapshot_count,
    }


@transaction.atomic
def apply_dead_link_review_action(
    item_ids: list[int],
    action: str,
    *,
    actor,
    note: str = "",
) -> dict:
    from harvest.models import DeadLinkReviewItem

    items = list(
        DeadLinkReviewItem.objects.filter(
            id__in=item_ids,
            status=DeadLinkReviewItem.Status.PENDING,
        ).select_related("raw_job", "linked_job")
    )
    results = {
        "requested": len(item_ids),
        "processed": 0,
        "archived_jobs": 0,
        "purged_raw_jobs": 0,
        "dismissed": 0,
        "errors": [],
    }
    for item in items:
        try:
            if action == "dismiss":
                dismiss_dead_link_review(item, actor=actor, note=note)
                results["dismissed"] += 1
            elif action == "archive":
                out = archive_dead_link_review(item, actor=actor, note=note)
                results["archived_jobs"] += len(out.get("archived_job_ids") or [])
            elif action == "purge":
                out = purge_dead_link_review(item, actor=actor, note=note)
                results["archived_jobs"] += len(out.get("archived_job_ids") or [])
                results["purged_raw_jobs"] += 1
            else:
                results["errors"].append(f"Unknown action: {action}")
                continue
            results["processed"] += 1
        except Exception as exc:
            results["errors"].append(f"#{item.pk}: {exc!s:.120}")
    return results


def pending_dead_link_review_count() -> int:
    from harvest.models import DeadLinkReviewItem

    return DeadLinkReviewItem.objects.filter(status=DeadLinkReviewItem.Status.PENDING).count()


def dead_link_review_queue_summary() -> dict:
    from harvest.models import DeadLinkReviewItem

    qs = DeadLinkReviewItem.objects.filter(status=DeadLinkReviewItem.Status.PENDING)
    return {
        "pending": qs.count(),
        "with_submissions": qs.filter(submission_count__gt=0).count(),
        "synced": qs.filter(raw_job__sync_status="SYNCED").count(),
    }

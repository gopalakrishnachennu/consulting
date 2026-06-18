"""Phase 2 shadow-mode: write PipelineEvent rows for lifecycle transitions.

Listens to Job + harvest.RawJob post_save. Records events without changing any
existing behavior so we can validate the audit trail against live data before
cutting over the actual tasks to stage-driven flow (Phase 3).
"""
import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from harvest.normalizer import compute_url_hash

from .models import Job, PipelineEvent

log = logging.getLogger(__name__)

_STATUS_TO_STAGE = {
    'POOL':   Job.Stage.VETTED,
    'OPEN':   Job.Stage.LIVE,
    'CLOSED': Job.Stage.ARCHIVED,
    'DRAFT':  Job.Stage.DISCOVERED,
}


def _safe_record(**kwargs):
    try:
        PipelineEvent.record(**kwargs)
    except Exception:
        log.exception("PipelineEvent.record failed (shadow mode — swallowed)")


@receiver(pre_save, sender=Job)
def _job_pre_save_capture_prev_stage(sender, instance: Job, **kwargs):
    """Stash previous stage on the instance so post_save knows the transition."""
    if not instance.pk:
        instance._prev_stage = None
        return
    try:
        instance._prev_stage = Job.objects.only('stage').get(pk=instance.pk).stage
    except Job.DoesNotExist:
        instance._prev_stage = None


@receiver(post_save, sender=Job)
def _job_post_save_record_event(sender, instance: Job, created: bool, **kwargs):
    if created:
        # New Job = reached at least VETTED (sync_harvested_to_pool) or DISCOVERED (manual).
        stage = instance.stage or _STATUS_TO_STAGE.get(instance.status, Job.Stage.DISCOVERED)
        _safe_record(
            job=instance,
            url_hash=instance.url_hash or '',
            to_stage=stage,
            task_name='signal.job_created',
            status=PipelineEvent.Status.SUCCESS,
            meta={'source': instance.job_source or '', 'company': instance.company},
        )
        return

    prev = getattr(instance, '_prev_stage', None)
    if prev and prev != instance.stage:
        _safe_record(
            job=instance,
            url_hash=instance.url_hash or '',
            from_stage=prev,
            to_stage=instance.stage,
            task_name='signal.stage_changed',
            status=PipelineEvent.Status.SUCCESS,
        )


def _rawjob_post_save_record_event(sender, instance, created: bool, **kwargs):
    if not created:
        return
    url_hash = getattr(instance, 'url_hash', '') or ''
    if not url_hash and getattr(instance, 'original_url', ''):
        url_hash = compute_url_hash(instance.original_url)
    _safe_record(
        url_hash=url_hash,
        to_stage=Job.Stage.FETCHED,
        task_name='signal.rawjob_created',
        status=PipelineEvent.Status.SUCCESS,
        meta={
            'platform': getattr(instance, 'platform_slug', '') or '',
            'company': getattr(instance, 'company_name', '') or '',
            'rawjob_id': instance.pk,
        },
    )


def _rawjob_has_enough_jd_text(instance) -> bool:
    title = (getattr(instance, "title", "") or "").strip()
    description = (
        getattr(instance, "description_clean", "")
        or getattr(instance, "description", "")
        or ""
    ).strip()
    return bool(title) and len(description) >= 80


def _queue_rawjob_shadow_classification(raw_job_id: int) -> None:
    from .tasks import run_rawjob_dual_classification_shadow_task

    run_rawjob_dual_classification_shadow_task.delay(raw_job_id)


def _rawjob_post_save_queue_dual_classification(sender, instance, created: bool, **kwargs):
    from .dual_classification.config import shadow_enabled
    from .dual_classification.orchestrator import invalidate_approved_snapshot_for_raw_job_input_change

    if not created:
        try:
            invalidate_approved_snapshot_for_raw_job_input_change(instance)
        except Exception:
            log.exception("Could not invalidate stale dual-classification approval for raw_job=%s", instance.pk)
    if not shadow_enabled():
        return
    if not _rawjob_has_enough_jd_text(instance):
        return
    cache_key = f"jobs:rawjob_dual_shadow:queued:{instance.pk}"
    if not cache.add(cache_key, "1", timeout=45):
        return
    transaction.on_commit(lambda: _queue_rawjob_shadow_classification(instance.pk))


def wire_rawjob_signal():
    """Called from JobsConfig.ready() — avoids import cycle at module load."""
    try:
        from harvest.models import RawJob
    except Exception:
        log.exception("Could not import harvest.RawJob; skipping shadow signal")
        return
    post_save.connect(_rawjob_post_save_record_event, sender=RawJob, dispatch_uid='jobs.shadow.rawjob_created')
    post_save.connect(_rawjob_post_save_queue_dual_classification, sender=RawJob, dispatch_uid='jobs.shadow.rawjob_dual_classification')

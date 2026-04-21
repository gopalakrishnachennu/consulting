"""Phase 4: stale-pool escalation.

Jobs that reach VETTED but stall there (nobody approves → LIVE) get flagged.
Records a PipelineEvent per stall so ops can triage from /jobs/pipeline/health/.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Job, PipelineEvent

logger = logging.getLogger(__name__)


@shared_task(name="jobs.escalate_stale_vetted_jobs")
def escalate_stale_vetted_jobs_task(stale_days: int = 7):
    """Flag VETTED jobs unchanged for stale_days. Idempotent per day."""
    cutoff = timezone.now() - timedelta(days=stale_days)
    stale = Job.objects.filter(
        stage=Job.Stage.VETTED,
        is_archived=False,
    ).filter(
        # Pick stage_changed_at if set, else fall back to created_at.
        models_q(stage_changed_at__lte=cutoff) | models_q(stage_changed_at__isnull=True, created_at__lte=cutoff),
    ).only('id', 'title', 'url_hash', 'stage_changed_at')

    count = 0
    for job in stale.iterator(chunk_size=200):
        PipelineEvent.record(
            job=job,
            url_hash=job.url_hash or '',
            from_stage=Job.Stage.VETTED,
            to_stage=Job.Stage.VETTED,
            task_name='jobs.escalate_stale_vetted_jobs',
            status=PipelineEvent.Status.SKIPPED,
            meta={'reason': 'stale', 'stale_days': stale_days},
        )
        count += 1
    logger.info("Stale escalation: flagged %d VETTED jobs older than %d days.", count, stale_days)
    return {'flagged': count, 'stale_days': stale_days}


def models_q(**kwargs):
    from django.db.models import Q
    return Q(**kwargs)

"""Backfill Job.stage from Job.status and Job.url_hash from Job.original_link."""
import hashlib
from django.db import migrations
from django.utils import timezone


STATUS_TO_STAGE = {
    'POOL':   'VETTED',
    'OPEN':   'LIVE',
    'CLOSED': 'ARCHIVED',
    'DRAFT':  'DISCOVERED',
}


def forwards(apps, schema_editor):
    Job = apps.get_model('jobs', 'Job')
    now = timezone.now()
    for job in Job.objects.all().only('id', 'status', 'original_link', 'stage', 'url_hash', 'is_archived'):
        updates = {}
        target_stage = 'ARCHIVED' if job.is_archived else STATUS_TO_STAGE.get(job.status, 'DISCOVERED')
        if job.stage != target_stage:
            updates['stage'] = target_stage
            updates['stage_changed_at'] = now
        if not job.url_hash and job.original_link:
            updates['url_hash'] = hashlib.sha256(job.original_link.encode('utf-8')).hexdigest()
        if updates:
            Job.objects.filter(pk=job.pk).update(**updates)


def backwards(apps, schema_editor):
    # Non-destructive reverse: leave stage/url_hash populated.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('jobs', '0015_job_quality_score_job_stage_job_stage_changed_at_and_more'),
    ]
    operations = [
        migrations.RunPython(forwards, backwards),
    ]

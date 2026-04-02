# Backfill status history for existing submissions (one event per submission)

from django.db import migrations


def backfill(apps, schema_editor):
    ApplicationSubmission = apps.get_model('submissions', 'ApplicationSubmission')
    SubmissionStatusHistory = apps.get_model('submissions', 'SubmissionStatusHistory')
    for sub in ApplicationSubmission.objects.all():
        if sub.status_history.exists():
            continue
        SubmissionStatusHistory.objects.create(
            submission=sub,
            from_status='',
            to_status=sub.status,
            created_at=sub.updated_at or sub.created_at,
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('submissions', '0005_submission_status_history'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]

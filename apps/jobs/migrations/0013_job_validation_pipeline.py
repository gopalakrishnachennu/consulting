import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0012_phase4_5'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Add POOL choice + change default status to POOL
        migrations.AlterField(
            model_name='job',
            name='status',
            field=models.CharField(
                choices=[
                    ('POOL', 'In Pool'),
                    ('OPEN', 'Open'),
                    ('CLOSED', 'Closed'),
                    ('DRAFT', 'Draft'),
                ],
                default='POOL',
                max_length=20,
            ),
        ),
        # Validation score
        migrations.AddField(
            model_name='job',
            name='validation_score',
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text='Quality score 0–100 computed by validate_job_quality()',
            ),
        ),
        # Full validation result JSON
        migrations.AddField(
            model_name='job',
            name='validation_result',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='Full breakdown: issues[], passed[], auto_approved',
            ),
        ),
        # When validation was last run
        migrations.AddField(
            model_name='job',
            name='validation_run_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Who approved
        migrations.AddField(
            model_name='job',
            name='validated_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='validated_jobs',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # Rejection reason (text)
        migrations.AddField(
            model_name='job',
            name='rejection_reason',
            field=models.TextField(blank=True),
        ),
        # Who rejected
        migrations.AddField(
            model_name='job',
            name='rejected_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='rejected_jobs',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # When rejected
        migrations.AddField(
            model_name='job',
            name='rejected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

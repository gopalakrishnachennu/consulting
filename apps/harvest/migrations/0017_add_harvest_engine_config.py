from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("harvest", "0016_drop_harvestrun_harvestedjob"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="HarvestEngineConfig",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("worker_concurrency", models.PositiveSmallIntegerField(
                    default=3,
                    verbose_name="Worker concurrency",
                    help_text="How many company-fetch tasks run in parallel per worker.",
                )),
                ("task_rate_limit", models.PositiveSmallIntegerField(
                    default=6,
                    verbose_name="Tasks per worker per minute",
                    help_text="Max fetch tasks each worker runs per minute. Applied immediately via Celery broadcast.",
                )),
                ("api_stagger_ms", models.PositiveIntegerField(
                    default=100,
                    verbose_name="API platform stagger (ms)",
                    help_text="Milliseconds between queuing tasks for JSON-API platforms.",
                )),
                ("scraper_stagger_ms", models.PositiveIntegerField(
                    default=1500,
                    verbose_name="Scraper platform stagger (ms)",
                    help_text="Milliseconds between queuing tasks for HTML-scraper platforms.",
                )),
                ("min_hours_since_fetch", models.PositiveSmallIntegerField(
                    default=6,
                    verbose_name="Min hours between re-fetches",
                    help_text="Skip companies fetched within this many hours. Set 0 to force re-fetch all.",
                )),
                ("task_soft_time_limit_secs", models.PositiveSmallIntegerField(
                    default=480,
                    verbose_name="Soft time limit (seconds)",
                    help_text="Company-fetch tasks running longer than this are gracefully cancelled.",
                )),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"verbose_name": "Harvest Engine Config"},
        ),
    ]

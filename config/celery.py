import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic tasks (enable Celery Beat in production).
app.conf.beat_schedule = {
    "weekly-consultant-pipeline-digest": {
        "task": "core.tasks.send_weekly_consultant_pipeline_digest_task",
        "schedule": crontab(hour=8, minute=0, day_of_week=1),  # Monday 08:00 UTC
    },
    "harvest-detect-platforms-weekly": {
        "task": "harvest.detect_company_platforms",
        "schedule": crontab(hour=1, minute=0, day_of_week=1),  # Monday 01:00 UTC
    },
    "harvest-jobs-daily": {
        "task": "harvest.harvest_jobs",
        "schedule": crontab(hour=2, minute=0),  # Daily 02:00 UTC
    },
    "harvest-cleanup-daily": {
        "task": "harvest.cleanup_harvested_jobs",
        "schedule": crontab(hour=0, minute=0),  # Daily midnight UTC
    },
}


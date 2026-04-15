import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# ─────────────────────────────────────────────────────────────────────────────
# FULL PERIODIC TASK SCHEDULE
# All tasks run inside the Celery Beat + Worker processes — completely
# independent of the web server / browser. Tasks keep running even when
# the browser is closed or the web dyno restarts.
# ─────────────────────────────────────────────────────────────────────────────
app.conf.beat_schedule = {

    # ── EMAIL INGEST ─────────────────────────────────────────────────────────
    "poll-email-ingest-every-5min": {
        "task": "core.tasks.poll_email_ingest_task",
        "schedule": crontab(minute="*/5"),           # every 5 min, 24/7
    },

    # ── JOB PIPELINE ─────────────────────────────────────────────────────────
    "validate-job-urls-daily": {
        "task": "jobs.tasks.validate_job_urls_task",
        "schedule": crontab(hour=3, minute=0),       # daily 03:00 UTC
        "kwargs": {"batch_size": 100},
    },
    "auto-close-stale-jobs-daily": {
        "task": "jobs.tasks.auto_close_jobs_task",
        "schedule": crontab(hour=4, minute=0),       # daily 04:00 UTC
    },

    # ── SUBMISSIONS ───────────────────────────────────────────────────────────
    "send-followup-reminders-every-4h": {
        "task": "submissions.tasks.send_followup_reminders",
        "schedule": crontab(minute=0, hour="*/4"),   # every 4 hours
    },
    "detect-stale-submissions-daily": {
        "task": "submissions.tasks.detect_stale_submissions",
        "schedule": crontab(hour=6, minute=0),       # daily 06:00 UTC
    },

    # ── COMPANY ENRICHMENT ────────────────────────────────────────────────────
    "validate-company-links-weekly": {
        "task": "companies.tasks.validate_company_links_task",
        "schedule": crontab(hour=2, minute=0, day_of_week=0),  # Sunday 02:00 UTC
        "kwargs": {"batch_size": 100},
    },
    "re-enrich-stale-companies-weekly": {
        "task": "companies.tasks.re_enrich_stale_companies_task",
        "schedule": crontab(hour=3, minute=0, day_of_week=0),  # Sunday 03:00 UTC
        "kwargs": {"stale_days": 30},
    },

    # ── REPORTS & DIGESTS ─────────────────────────────────────────────────────
    "weekly-consultant-pipeline-digest": {
        "task": "core.tasks.send_weekly_consultant_pipeline_digest_task",
        "schedule": crontab(hour=8, minute=0, day_of_week=1),  # Monday 08:00 UTC
    },
    "weekly-executive-report": {
        "task": "core.tasks.send_weekly_executive_report_task",
        "schedule": crontab(hour=9, minute=0, day_of_week=1),  # Monday 09:00 UTC
    },

    # ── HARVEST ENGINE ────────────────────────────────────────────────────────
    "harvest-detect-platforms-weekly": {
        "task": "harvest.detect_company_platforms",
        "schedule": crontab(hour=1, minute=0, day_of_week=1),  # Monday 01:00 UTC
        "kwargs": {"batch_size": 200},
    },
    "harvest-jobs-daily": {
        "task": "harvest.harvest_jobs",
        "schedule": crontab(hour=2, minute=0),       # daily 02:00 UTC
    },
    "harvest-sync-to-pool-daily": {
        "task": "harvest.sync_harvested_to_pool",
        "schedule": crontab(hour=6, minute=30),      # daily 06:30 UTC
        "kwargs": {"max_jobs": 200},
    },
    "harvest-cleanup-daily": {
        "task": "harvest.cleanup_harvested_jobs",
        "schedule": crontab(hour=0, minute=0),       # daily midnight UTC
    },
}

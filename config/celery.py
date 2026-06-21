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

    # ── JOB CLASSIFICATION ───────────────────────────────────────────────────
    "classify-jobs-daily": {
        "task": "jobs.classify_all",
        "schedule": crontab(hour=7, minute=0),       # daily 07:00 UTC — after harvest sync (06:30)
        "kwargs": {"force_reclassify": False},        # only new / needs_reclassification jobs
    },

    # ── JOB PIPELINE ─────────────────────────────────────────────────────────
    "validate-job-urls-daily": {
        "task": "jobs.tasks.validate_job_urls_task",
        "schedule": crontab(hour=3, minute=30),      # daily 03:30 UTC — only manually-created jobs (harvest-linked jobs handled by harvest-validate-live-links-daily)
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

    # ── ANALYTICS ────────────────────────────────────────────────────────────
    "analytics-daily-snapshot": {
        "task": "analytics.tasks.take_daily_snapshot_task",
        "schedule": crontab(hour=23, minute=30),      # daily 23:30 UTC
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
    "harvest-backfill-labels-from-jobs-daily": {
        "task": "harvest.backfill_platform_labels_from_jobs",
        "schedule": crontab(hour=1, minute=30),      # daily 01:30 UTC — after any bulk imports
    },
    "harvest-detect-platforms-weekly": {
        "task": "harvest.detect_company_platforms",
        "schedule": crontab(hour=1, minute=0, day_of_week=1),  # Monday 01:00 UTC
        "kwargs": {"batch_size": 50},
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
    "harvest-validate-live-links-daily": {
        "task": "harvest.validate_raw_job_urls",
        "schedule": crontab(hour=3, minute=0),       # daily 03:00 UTC
        "kwargs": {"batch_size": 200, "concurrency": 8, "pending_only": False, "recent_hours": 0},
        "options": {"queue": "harvest"},
    },
    "harvest-release-stale-jd-locks": {
        "task": "harvest.release_stale_jd_backfill_locks",
        "schedule": crontab(minute="*/10"),          # every 10 min
        "options": {"queue": "harvest"},
    },

    # Continuously fetch missing JDs — runs every hour so new harvests get
    # their descriptions filled without manual intervention. The task itself
    # loops until all eligible rows are processed, so a single run covers
    # the full backlog; this schedule ensures it restarts after deploys.
    "harvest-backfill-descriptions-hourly": {
        "task": "harvest.backfill_descriptions",
        "schedule": crontab(minute=15),              # every hour at :15
        "kwargs": {"batch_size": 200, "parallel_workers": 1},
    },

    # Tier-2 JD content gate — runs every 30 min to process AMBIGUOUS jobs.
    # Gate is a no-op when jd_gate_enabled=False (safe to always have scheduled).
    # Runs at :00 and :30 each hour; offset from backfill (:15) to avoid overlap.
    "harvest-jd-gate-30min": {
        "task": "harvest.run_jd_gate",
        "schedule": crontab(minute="0,30"),          # every 30 min
        "kwargs": {"batch_size": 100, "trigger_backfill": True},
        "options": {"queue": "harvest"},
    },
}

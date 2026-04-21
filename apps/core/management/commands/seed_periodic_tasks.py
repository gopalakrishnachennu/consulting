"""
management command: seed_periodic_tasks
Idempotently creates / updates all 13 GoCareers periodic tasks in the
django_celery_beat PeriodicTask table.

Run once after first deploy, or whenever you add new tasks:
    python manage.py seed_periodic_tasks

Safe to re-run — uses update_or_create so existing tweaks are preserved
unless --reset is passed.
"""
from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask, CrontabSchedule
import json


TASKS = [
    # ── EMAIL ──────────────────────────────────────────────────────────────
    {
        "name": "Email Ingest — IMAP poll",
        "task": "core.tasks.poll_email_ingest_task",
        "category": "email",
        "description": "Polls the configured IMAP mailbox for incoming emails and routes them.",
        "cron": {"minute": "*/5", "hour": "*", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Every 5 minutes",
        "kwargs": {},
    },

    # ── SUBMISSIONS ────────────────────────────────────────────────────────
    {
        "name": "Follow-up Reminders — send",
        "task": "submissions.tasks.send_followup_reminders",
        "category": "submissions",
        "description": "Sends follow-up reminder emails to consultants for overdue applications.",
        "cron": {"minute": "0", "hour": "*/4", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Every 4 hours",
        "kwargs": {},
    },
    {
        "name": "Stale Submissions — detect",
        "task": "submissions.tasks.detect_stale_submissions",
        "category": "submissions",
        "description": "Flags applications with no activity for 14+ days as stale.",
        "cron": {"minute": "0", "hour": "6", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Daily 06:00 UTC",
        "kwargs": {},
    },

    # ── JOBS ───────────────────────────────────────────────────────────────
    {
        "name": "Job URLs — validate",
        "task": "jobs.tasks.validate_job_urls_task",
        "category": "jobs",
        "description": "Checks that all active job posting URLs still resolve (HTTP 200).",
        "cron": {"minute": "0", "hour": "3", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Daily 03:00 UTC",
        "kwargs": {"batch_size": 100},
    },
    {
        "name": "Stale Jobs — auto-close",
        "task": "jobs.tasks.auto_close_jobs_task",
        "category": "jobs",
        "description": "Automatically closes job postings that have been open past their expiry date.",
        "cron": {"minute": "0", "hour": "4", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Daily 04:00 UTC",
        "kwargs": {},
    },

    # ── COMPANIES ──────────────────────────────────────────────────────────
    {
        "name": "Company Links — validate",
        "task": "companies.tasks.validate_company_links_task",
        "category": "companies",
        "description": "Validates company website URLs and removes dead links.",
        "cron": {"minute": "0", "hour": "2", "day_of_week": "0", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Weekly Sunday 02:00 UTC",
        "kwargs": {"batch_size": 100},
    },
    {
        "name": "Companies — re-enrich stale",
        "task": "companies.tasks.re_enrich_stale_companies_task",
        "category": "companies",
        "description": "Re-fetches metadata for companies not enriched in 30+ days.",
        "cron": {"minute": "0", "hour": "3", "day_of_week": "0", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Weekly Sunday 03:00 UTC",
        "kwargs": {"stale_days": 30},
    },

    # ── REPORTS ────────────────────────────────────────────────────────────
    {
        "name": "Digest — weekly consultant pipeline",
        "task": "core.tasks.send_weekly_consultant_pipeline_digest_task",
        "category": "reports",
        "description": "Sends each consultant a personalized weekly summary of their pipeline.",
        "cron": {"minute": "0", "hour": "8", "day_of_week": "1", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Monday 08:00 UTC",
        "kwargs": {},
    },
    {
        "name": "Report — weekly executive summary",
        "task": "core.tasks.send_weekly_executive_report_task",
        "category": "reports",
        "description": "Emails superusers a high-level KPI report covering the past week.",
        "cron": {"minute": "0", "hour": "9", "day_of_week": "1", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Monday 09:00 UTC",
        "kwargs": {},
    },

    # ── ANALYTICS ──────────────────────────────────────────────────────────
    {
        "name": "Analytics — daily snapshot",
        "task": "analytics.tasks.take_daily_snapshot_task",
        "category": "analytics",
        "description": "Captures a point-in-time snapshot of platform health metrics (jobs, submissions, revenue, consultants).",
        "cron": {"minute": "55", "hour": "23", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Daily 23:55 UTC",
        "kwargs": {},
    },
    {
        "name": "Matching — refresh consultant embeddings",
        "task": "jobs.tasks.refresh_consultant_embeddings_task",
        "category": "analytics",
        "description": "Regenerates OpenAI embeddings for all active consultant profiles for semantic job matching.",
        "cron": {"minute": "0", "hour": "2", "day_of_week": "0", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Weekly Sunday 02:00 UTC",
        "kwargs": {},
    },

    # ── HARVEST ENGINE ─────────────────────────────────────────────────────
    {
        "name": "Harvest — detect company platforms",
        "task": "harvest.detect_company_platforms",
        "category": "harvest",
        "description": "Runs 3-step ATS detection (URL pattern → HTTP redirect → HTML parse) on undetected companies.",
        "cron": {"minute": "0", "hour": "1", "day_of_week": "1", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Monday 01:00 UTC",
        "kwargs": {"batch_size": 200},
    },
    {
        "name": "Harvest — fetch new jobs",
        "task": "harvest.harvest_jobs",
        "category": "harvest",
        "description": "Pulls last-24-hour job listings from Workday, Greenhouse, Lever, Ashby and scraping platforms.",
        "cron": {"minute": "0", "hour": "2", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Daily 02:00 UTC",
        "kwargs": {"since_hours": 24},
    },
    {
        "name": "Harvest — sync to job pool",
        "task": "harvest.sync_harvested_to_pool",
        "category": "harvest",
        "description": "Promotes reviewed harvest results into the live job pool for consultant matching.",
        "cron": {"minute": "30", "hour": "6", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Daily 06:30 UTC",
        "kwargs": {"max_jobs": 200},
    },
    {
        "name": "Harvest — cleanup expired jobs",
        "task": "harvest.cleanup_harvested_jobs",
        "category": "harvest",
        "description": "Purges harvested job listings older than 24 hours and trims run logs older than 30 days.",
        "cron": {"minute": "0", "hour": "0", "day_of_week": "*", "day_of_month": "*", "month_of_year": "*"},
        "schedule_label": "Daily midnight UTC",
        "kwargs": {},
    },
]

CATEGORY_ORDER = ["email", "submissions", "jobs", "companies", "reports", "harvest"]


class Command(BaseCommand):
    help = "Seed / sync all 13 GoCareers periodic tasks into django_celery_beat"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete ALL existing GoCareers periodic tasks and re-create from scratch.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            deleted, _ = PeriodicTask.objects.filter(
                name__in=[t["name"] for t in TASKS]
            ).delete()
            self.stdout.write(self.style.WARNING(f"🗑  Deleted {deleted} existing tasks."))

        created_count = 0
        updated_count = 0

        for task_def in TASKS:
            cron_data = task_def["cron"]
            crontab, _ = CrontabSchedule.objects.get_or_create(**cron_data)

            kwargs_json = json.dumps(task_def["kwargs"]) if task_def["kwargs"] else "{}"

            obj, created = PeriodicTask.objects.update_or_create(
                name=task_def["name"],
                defaults={
                    "task": task_def["task"],
                    "crontab": crontab,
                    "kwargs": kwargs_json,
                    "enabled": True,
                    "description": task_def.get("description", ""),
                },
            )

            if created:
                created_count += 1
                self.stdout.write(f"  ✅ Created: {obj.name}")
            else:
                updated_count += 1
                self.stdout.write(f"  🔄 Updated: {obj.name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"\n✅ Done — {created_count} created, {updated_count} updated. "
                f"Total: {PeriodicTask.objects.count()} tasks in DB."
            )
        )

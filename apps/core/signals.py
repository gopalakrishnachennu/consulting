from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import PlatformConfig


@receiver(post_save, sender=PlatformConfig)
def sync_periodic_tasks(sender, instance: PlatformConfig, **kwargs):
    """
    If django-celery-beat is installed, keep periodic tasks in sync with PlatformConfig.
    Currently manages:
    - IMAP email ingestion poller (Interval)
    - Weekly executive report (Crontab, Monday 08:00)
    """
    try:
        from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
    except Exception:
        return

    # IMAP poller
    task_name = "core.tasks.poll_email_ingest_task"
    periodic_name = "CHENN: IMAP email ingestion poller"

    enabled = bool(instance.email_ingest_enabled and instance.email_auto_poll_enabled)
    seconds = int(instance.email_poll_interval_seconds or 60)
    if seconds < 30:
        seconds = 30

    schedule, _ = IntervalSchedule.objects.get_or_create(every=seconds, period=IntervalSchedule.SECONDS)

    PeriodicTask.objects.update_or_create(
        name=periodic_name,
        defaults={
            "task": task_name,
            "interval": schedule,
            "enabled": enabled,
        },
    )

    # Weekly executive report – always configured, can be disabled via PeriodicTask admin if needed.
    weekly_task_name = "core.tasks.send_weekly_executive_report_task"
    weekly_name = "CHENN: Weekly Executive Report"
    # Every Monday at 08:00 (server timezone)
    cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="8",
        day_of_week="1",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.update_or_create(
        name=weekly_name,
        defaults={
            "task": weekly_task_name,
            "crontab": cron,
            "enabled": True,
        },
    )

    # Company link validator – daily at 03:00
    companies_task_name = "companies.tasks.validate_company_links_task"
    companies_name = "CHENN: Validate Company Links"
    companies_cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="3",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.update_or_create(
        name=companies_name,
        defaults={
            "task": companies_task_name,
            "crontab": companies_cron,
            "enabled": True,
        },
    )

    # Job URL validator – daily at 04:00
    jobs_task_name = "jobs.tasks.validate_job_urls_task"
    jobs_name = "CHENN: Validate Job URLs"
    jobs_cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="4",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.update_or_create(
        name=jobs_name,
        defaults={
            "task": jobs_task_name,
            "crontab": jobs_cron,
            "enabled": True,
        },
    )

    # Re-enrich stale companies – every 30 days (1st of month at 05:00)
    re_enrich_task_name = "companies.tasks.re_enrich_stale_companies_task"
    re_enrich_name = "CHENN: Re-enrich Stale Companies"
    re_enrich_cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="5",
        day_of_week="*",
        day_of_month="1",
        month_of_year="*",
    )
    PeriodicTask.objects.update_or_create(
        name=re_enrich_name,
        defaults={
            "task": re_enrich_task_name,
            "crontab": re_enrich_cron,
            "enabled": True,
        },
    )

    # Full re-enrich all companies – every 90 days (1st of Jan, Apr, Jul, Oct at 06:00)
    full_enrich_task_name = "companies.tasks.full_re_enrich_companies_task"
    full_enrich_name = "CHENN: Full Re-enrich All Companies"
    full_enrich_cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="6",
        day_of_week="*",
        day_of_month="1",
        month_of_year="1,4,7,10",
    )
    PeriodicTask.objects.update_or_create(
        name=full_enrich_name,
        defaults={
            "task": full_enrich_task_name,
            "crontab": full_enrich_cron,
            "enabled": True,
        },
    )



"""Delete old read notifications to keep the table bounded (ops / cron)."""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from core.models import Notification


class Command(BaseCommand):
    help = "Delete read notifications older than N days (default 180). Unread items are kept."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=180,
            help="Age threshold in days for read notifications (default 180).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print how many rows would be deleted without deleting.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)
        qs = Notification.objects.filter(read_at__isnull=False, read_at__lt=cutoff)
        n = qs.count()
        if dry_run:
            self.stdout.write(self.style.WARNING(f"Would delete {n} read notifications older than {days} days."))
            return
        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} notification row(s) (read, older than {days} days)."))

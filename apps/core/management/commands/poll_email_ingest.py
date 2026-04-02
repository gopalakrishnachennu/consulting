from django.core.management.base import BaseCommand

from core.email_ingest import fetch_unseen_and_process


class Command(BaseCommand):
    help = "Poll the configured IMAP inbox and process unread messages into EmailEvent records (rules-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run without writing EmailEvent records or changing submissions.",
        )
        parser.add_argument(
            "--max",
            type=int,
            default=20,
            help="Maximum number of messages to process in one run.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        max_messages = options["max"]

        result = fetch_unseen_and_process(dry_run=dry_run, max_messages=max_messages)

        if result.get("reason"):
            self.stdout.write(self.style.WARNING(f"Email ingest skipped: {result['reason']}"))
        else:
            msg = (
                f"Processed {result['processed']} messages "
                f"(auto_updated={result['auto_updated']}, needs_review={result['needs_review']}, dry_run={result['dry_run']})"
            )
            self.stdout.write(self.style.SUCCESS(msg))


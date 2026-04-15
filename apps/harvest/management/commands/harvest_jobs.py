from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Harvest jobs from all or a specific platform."

    def add_arguments(self, parser):
        parser.add_argument("--platform", type=str, default="", help="Platform slug (optional).")
        parser.add_argument("--since-hours", type=int, default=24, help="Look back N hours.")
        parser.add_argument("--max-companies", type=int, default=50, help="Max companies per run.")

    def handle(self, *args, **options):
        from harvest.tasks import harvest_jobs_task

        slug = options["platform"] or None
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Harvesting jobs for {'all platforms' if not slug else slug}..."
        ))
        result = harvest_jobs_task(
            platform_slug=slug,
            since_hours=options["since_hours"],
            max_companies=options["max_companies"],
        )
        self.stdout.write(self.style.SUCCESS(f"Harvest complete: {result}"))

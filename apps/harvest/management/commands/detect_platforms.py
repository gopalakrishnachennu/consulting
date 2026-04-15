from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Detect job board platforms for companies (batch)."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=200, help="Companies per run.")
        parser.add_argument("--force", action="store_true", help="Force re-check all companies.")

    def handle(self, *args, **options):
        from harvest.tasks import detect_company_platforms_task

        self.stdout.write(self.style.MIGRATE_HEADING("Running platform detection..."))
        result = detect_company_platforms_task(
            batch_size=options["batch_size"],
            force_recheck=options["force"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {result.get('detected', 0)} detected out of {result.get('total', 0)} companies."
            )
        )

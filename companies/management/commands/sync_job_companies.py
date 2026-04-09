"""
Management command: sync_job_companies

Backfill Company records for all existing Jobs that have a company name
but no company_obj FK yet. Safe to re-run — skips already-linked jobs.

Usage:
    python manage.py sync_job_companies            # dry run
    python manage.py sync_job_companies --commit   # actually create + enrich
    python manage.py sync_job_companies --commit --enrich  # also queue enrichment
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create Company records from existing Job.company text and link company_obj."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually write to the DB. Without this flag the command does a dry run.",
        )
        parser.add_argument(
            "--enrich",
            action="store_true",
            help="Queue enrich_company_task for newly created companies (requires Celery).",
        )

    def handle(self, *args, **options):
        commit = options["commit"]
        enrich = options["enrich"]

        from jobs.models import Job
        from companies.models import Company
        from companies.services import normalize_company_name
        from companies.tasks import enrich_company_task

        # All jobs that have a company name but no company_obj FK
        unlinked = Job.objects.filter(company_obj__isnull=True).exclude(company="")
        total = unlinked.count()
        self.stdout.write(f"Found {total} unlinked job(s).")

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to do."))
            return

        created_count = 0
        linked_count = 0
        skipped = 0

        for job in unlinked.iterator():
            raw = (job.company or "").strip()
            if not raw:
                skipped += 1
                continue

            name = normalize_company_name(raw)

            if commit:
                company = Company.objects.filter(name__iexact=name).first()
                was_created = False
                if not company:
                    company = Company.objects.create(name=name)
                    was_created = True

                if was_created:
                    created_count += 1
                    if enrich:
                        enrich_company_task.delay(company.pk)
                        self.stdout.write(f"  [NEW + queued enrich] {name}")
                    else:
                        self.stdout.write(f"  [NEW] {name}")

                Job.objects.filter(pk=job.pk).update(company_obj=company)
                linked_count += 1
            else:
                self.stdout.write(f"  [dry-run] would create/link: {name!r}")

        if commit:
            self.stdout.write(self.style.SUCCESS(
                f"\nDone. Created {created_count} new companies, linked {linked_count} jobs."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"\nDry run complete ({total} jobs). Re-run with --commit to apply."
            ))

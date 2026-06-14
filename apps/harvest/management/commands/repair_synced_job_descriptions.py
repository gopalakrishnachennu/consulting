from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Repair synced Job descriptions from their linked RawJob.description_clean text."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report changes without updating Jobs.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum Jobs to scan. 0 = no limit.")
        parser.add_argument(
            "--include-manual-edits",
            action="store_true",
            help="Also update Jobs with last_edited_by set. Default preserves manual edits.",
        )
        parser.add_argument(
            "--all-different",
            action="store_true",
            help="Update every linked Job whose description differs from the RawJob clean text, not only HTML-ish rows.",
        )

    def handle(self, *args, **options):
        from jobs.models import Job
        from harvest.services.job_descriptions import job_description_for_sync, looks_htmlish

        dry_run = options["dry_run"]
        include_manual = options["include_manual_edits"]
        all_different = options["all_different"]
        limit = options["limit"]

        qs = Job.objects.select_related("source_raw_job").filter(source_raw_job__isnull=False).order_by("pk")
        if not include_manual:
            qs = qs.filter(last_edited_by__isnull=True)
        if limit:
            qs = qs[:limit]

        scanned = changed = skipped_empty = skipped_clean = skipped_not_htmlish = 0
        examples: list[tuple[int, str]] = []

        for job in qs.iterator(chunk_size=500):
            scanned += 1
            clean_description = job_description_for_sync(job.source_raw_job)
            if not clean_description:
                skipped_empty += 1
                continue
            if (job.description or "") == clean_description:
                skipped_clean += 1
                continue
            if not all_different and not looks_htmlish(job.description or ""):
                skipped_not_htmlish += 1
                continue

            changed += 1
            if len(examples) < 5:
                examples.append((job.pk, job.title))
            if not dry_run:
                Job.objects.filter(pk=job.pk).update(
                    description=clean_description,
                    updated_at=timezone.now(),
                )

        verb = "Would repair" if dry_run else "Repaired"
        self.stdout.write(
            f"{verb} {changed:,} synced Job description(s). "
            f"Scanned {scanned:,}; already clean {skipped_clean:,}; "
            f"empty source {skipped_empty:,}; non-HTML differs skipped {skipped_not_htmlish:,}."
        )
        if examples:
            self.stdout.write("Examples:")
            for pk, title in examples:
                self.stdout.write(f"  #{pk}: {title}")

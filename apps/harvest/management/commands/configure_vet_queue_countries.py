"""
Configure vet-queue country scope and optionally archive non-matching POOL jobs.

Usage:
    python manage.py configure_vet_queue_countries --countries US --dry-run
    python manage.py configure_vet_queue_countries --countries US --disable-unknown --archive-pool --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from harvest.models import HarvestEngineConfig, VetGateConfig
from harvest.vet_queue import get_vet_queue_country_codes, job_vet_country_q
from jobs.models import Job


class Command(BaseCommand):
    help = "Set vet-queue country scope (default USA) and optionally archive non-matching pool jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--countries",
            default="US",
            help="Comma-separated ISO country codes for vet queue (default: US).",
        )
        parser.add_argument(
            "--disable-unknown",
            action="store_true",
            help="Turn off allow_unknown_country on VetGateConfig.",
        )
        parser.add_argument(
            "--archive-pool",
            action="store_true",
            help="Archive existing POOL jobs outside the allowed country list.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write config changes (without this flag, only reports).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Alias for preview mode when combined with --archive-pool.",
        )

    def handle(self, *args, **options):
        codes = [
            part.strip().upper()
            for part in (options["countries"] or "").split(",")
            if part.strip()
        ]
        if not codes:
            self.stderr.write(self.style.ERROR("Provide at least one country code."))
            return

        apply_writes = bool(options["apply"])
        preview = not apply_writes or options["dry_run"]

        engine = HarvestEngineConfig.get()
        vet = VetGateConfig.get()

        self.stdout.write(f"Vet queue countries: {codes}")
        self.stdout.write(f"Current engine target_countries: {engine.get_target_countries()}")
        self.stdout.write(f"Current vet_queue_country_codes: {vet.vet_queue_country_codes or []}")
        self.stdout.write(f"allow_unknown_country: {vet.allow_unknown_country}")

        if apply_writes and not preview:
            engine.target_countries = codes
            engine.save(update_fields=["target_countries", "updated_at"])
            vet.vet_queue_country_codes = codes
            if options["disable_unknown"]:
                vet.allow_unknown_country = False
            vet.save()
            self.stdout.write(self.style.SUCCESS("Config updated."))
        else:
            self.stdout.write(self.style.WARNING("Preview only — pass --apply to write config."))

        if options["archive_pool"]:
            allowed_q = job_vet_country_q(codes)
            qs = Job.objects.filter(status=Job.Status.POOL, is_archived=False)
            to_archive = qs.filter(~allowed_q) if allowed_q else qs.none()
            count = to_archive.count()
            self.stdout.write(f"POOL jobs outside {codes}: {count:,}")
            if count and apply_writes and not preview:
                from harvest.tasks import _archive_active_job

                archived = 0
                for job in to_archive.iterator(chunk_size=200):
                    _archive_active_job(
                        job,
                        reason_code="non_target_country",
                        reason_detail=f"Archived: country not in vet queue scope {','.join(codes)}",
                        task_name="configure_vet_queue_countries",
                    )
                    archived += 1
                self.stdout.write(self.style.SUCCESS(f"Archived {archived:,} non-matching POOL jobs."))
            elif count:
                sample = list(to_archive.values_list("id", "title", "country")[:10])
                for job_id, title, country in sample:
                    self.stdout.write(f"  - Job #{job_id}: {title[:60]} ({country})")

        self.stdout.write(f"Effective vet queue codes: {get_vet_queue_country_codes()}")

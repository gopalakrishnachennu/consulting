"""
Management command: backfill_raw_jobs_to_pool

Syncs high-quality PENDING RawJobs into the Job pool in bulk.
Safe to re-run — existing Jobs are skipped via url_hash dedup.

Usage:
    python manage.py backfill_raw_jobs_to_pool
    python manage.py backfill_raw_jobs_to_pool --batch 500 --min-desc-len 100
    python manage.py backfill_raw_jobs_to_pool --dry-run
"""
from django.core.management.base import BaseCommand
from django.db.models.functions import Greatest, Length


class Command(BaseCommand):
    help = "Bulk-sync high-quality PENDING RawJobs into the Job pool"

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=200, help="Jobs to process per chunk (default 200)")
        parser.add_argument("--max-total", type=int, default=5000, help="Max total to sync in one run (default 5000)")
        parser.add_argument("--min-desc-len", type=int, default=50, help="Minimum description length (default 50)")
        parser.add_argument("--dry-run", action="store_true", help="Count eligible rows without syncing")

    def handle(self, *args, **options):
        from harvest.models import RawJob
        from jobs.marketing_role_routing import assign_marketing_roles_to_job
        from jobs.quality import compute_quality_score
        from harvest.services.pool_job_sync import create_or_get_vetting_job_from_raw_job
        from django.contrib.auth import get_user_model

        User = get_user_model()
        system_user = User.objects.filter(is_superuser=True).first()
        if not system_user:
            self.stderr.write("No superuser found.")
            return

        batch = options["batch"]
        max_total = options["max_total"]
        min_len = options["min_desc_len"]
        dry_run = options["dry_run"]

        qs = (
            RawJob.objects
            .filter(sync_status="PENDING", is_active=True, company__isnull=False)
            .exclude(original_url="")
            .annotate(_desc_len=Greatest(Length("description_clean"), Length("description")))
            .filter(_desc_len__gt=min_len)
            .order_by("-fetched_at")
            .select_related("company", "job_platform")
        )

        total_eligible = qs.count()
        self.stdout.write(f"Eligible PENDING RawJobs (desc>{min_len}): {total_eligible:,}")
        if dry_run:
            return

        synced = skipped = failed = processed = 0
        offset = 0
        while processed < max_total:
            chunk = list(qs[offset : offset + batch])
            if not chunk:
                break
            for rj in chunk:
                if processed >= max_total:
                    break
                processed += 1
                try:
                    job, created_new, locked_raw = create_or_get_vetting_job_from_raw_job(
                        rj,
                        posted_by=system_user,
                        job_location=rj.location_raw or "",
                        job_country=rj.country or "",
                        mapped_department=rj.department_normalized or "",
                    )
                    if not created_new:
                        RawJob.objects.filter(pk=locked_raw.pk).update(
                            sync_status="SKIPPED",
                            sync_skip_reason="DUPLICATE_EXISTING",
                        )
                        skipped += 1
                        continue

                    job.quality_score = compute_quality_score(job)
                    job.save(update_fields=["quality_score", "updated_at"])
                    assign_marketing_roles_to_job(job, raw_job=locked_raw)
                    RawJob.objects.filter(pk=locked_raw.pk).update(sync_status="SYNCED")
                    synced += 1
                except Exception as e:
                    RawJob.objects.filter(pk=rj.pk).update(sync_status="FAILED")
                    failed += 1
                    self.stderr.write(f"  Failed RawJob {rj.pk}: {e}")

            self.stdout.write(f"  Chunk done — synced:{synced} skipped:{skipped} failed:{failed}")
            offset += batch

        self.stdout.write(self.style.SUCCESS(
            f"\nDone: {synced} synced, {skipped} skipped (already exist), {failed} failed. "
            f"Remaining PENDING: {qs.count():,}"
        ))

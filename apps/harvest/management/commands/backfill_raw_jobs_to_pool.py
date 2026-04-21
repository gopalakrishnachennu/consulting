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
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Bulk-sync high-quality PENDING RawJobs into the Job pool"

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=200, help="Jobs to process per chunk (default 200)")
        parser.add_argument("--max-total", type=int, default=5000, help="Max total to sync in one run (default 5000)")
        parser.add_argument("--min-desc-len", type=int, default=50, help="Minimum description length (default 50)")
        parser.add_argument("--dry-run", action="store_true", help="Count eligible rows without syncing")

    def handle(self, *args, **options):
        from harvest.models import RawJob
        from jobs.models import Job
        from jobs.dedup import find_existing_job_by_url
        from jobs.quality import compute_quality_score
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
            .filter(description__length__gt=min_len)
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
                existing = (
                    (Job.objects.filter(url_hash=rj.url_hash, is_archived=False).first() if rj.url_hash else None)
                    or find_existing_job_by_url(rj.original_url)
                    or Job.objects.filter(original_link=rj.original_url).first()
                )
                if existing:
                    RawJob.objects.filter(pk=rj.pk).update(sync_status="SKIPPED")
                    skipped += 1
                    continue
                try:
                    platform_slug = rj.platform_slug or (rj.job_platform.slug if rj.job_platform else "")
                    with transaction.atomic():
                        job = Job.objects.create(
                            title=rj.title,
                            company=rj.company_name or (rj.company.name if rj.company else ""),
                            company_obj=rj.company,
                            location=rj.location_raw or "",
                            description=rj.description or rj.title,
                            original_link=rj.original_url,
                            salary_range=rj.salary_raw or "",
                            job_type=rj.employment_type if rj.employment_type != "UNKNOWN" else "FULL_TIME",
                            status="POOL",
                            stage=Job.Stage.VETTED,
                            stage_changed_at=timezone.now(),
                            url_hash=rj.url_hash or "",
                            job_source=f"HARVESTED_{platform_slug.upper()}" if platform_slug else "HARVESTED",
                            posted_by=system_user,
                        )
                        job.quality_score = compute_quality_score(job)
                        Job.objects.filter(pk=job.pk).update(quality_score=job.quality_score)
                        RawJob.objects.filter(pk=rj.pk).update(sync_status="SYNCED")
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

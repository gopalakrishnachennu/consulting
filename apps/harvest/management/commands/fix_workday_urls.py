"""
fix_workday_urls — backfill jobboard path into existing Workday RawJob URLs

Workday job URLs must include the jobboard path:
  WRONG:  https://3m.wd1.myworkdayjobs.com/job/US-MN/Some-Job_R123
  RIGHT:  https://3m.wd1.myworkdayjobs.com/Search/job/US-MN/Some-Job_R123

The harvester was generating URLs without the jobboard (returns 404).
This command fixes all existing RawJob records by looking up the
jobboard from CompanyPlatformLabel.tenant_id (format: subdomain|jobboard)
and inserting it into the URL.

Also recomputes url_hash (SHA256 of original_url) so deduplication stays
correct on future crawls.

Usage:
    python manage.py fix_workday_urls
    python manage.py fix_workday_urls --dry-run      # preview only
    python manage.py fix_workday_urls --batch-size 500
"""
import hashlib
import re

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Backfill jobboard path into existing Workday RawJob URLs"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview changes, don't save")
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **options):
        from harvest.models import RawJob, CompanyPlatformLabel

        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        self.stdout.write("Building tenant_id → jobboard map from CompanyPlatformLabel...")

        # Build map: company_id → jobboard string
        # tenant_id format: "3m.wd1|Search" → jobboard = "Search"
        label_map: dict[int, str] = {}
        for label in CompanyPlatformLabel.objects.filter(
            platform__slug="workday"
        ).exclude(tenant_id="").values("company_id", "tenant_id"):
            tid = label["tenant_id"] or ""
            if "|" in tid:
                jobboard = tid.split("|", 1)[1].strip()
                if jobboard:
                    label_map[label["company_id"]] = jobboard

        self.stdout.write(f"  {len(label_map)} companies have a known jobboard")

        # Process Workday RawJobs in batches
        qs = RawJob.objects.filter(platform_slug="workday").exclude(original_url="")
        total = qs.count()
        self.stdout.write(f"Processing {total:,} Workday RawJob records...")

        updated = skipped_no_label = skipped_already_ok = already_has_board = 0
        offset = 0

        while offset < total:
            batch = list(
                qs.values("id", "original_url", "apply_url", "company_id")[offset:offset + batch_size]
            )
            if not batch:
                break

            to_update = []
            for row in batch:
                company_id = row["company_id"]
                orig_url = row["original_url"] or ""
                apply_url = row["apply_url"] or ""

                if not orig_url:
                    skipped_no_label += 1
                    continue

                jobboard = label_map.get(company_id)
                if not jobboard:
                    skipped_no_label += 1
                    continue

                # Check if the URL already has the jobboard inserted
                # Pattern: myworkdayjobs.com/job/ → needs jobboard prefix
                # Pattern: myworkdayjobs.com/{something}/job/ → already has it
                already_fixed = bool(re.search(
                    r"myworkdayjobs\.com/[^/]+/job/", orig_url, re.I
                ))
                if already_fixed:
                    skipped_already_ok += 1
                    continue

                # Insert jobboard: .../com/job/... → .../com/{jobboard}/job/...
                new_orig = re.sub(
                    r"(myworkdayjobs\.com)/job/",
                    rf"\1/{jobboard}/job/",
                    orig_url,
                    flags=re.I,
                )
                if new_orig == orig_url:
                    # URL doesn't match the /job/ pattern — skip
                    skipped_already_ok += 1
                    continue

                new_apply = re.sub(
                    r"(myworkdayjobs\.com)/job/",
                    rf"\1/{jobboard}/job/",
                    apply_url,
                    flags=re.I,
                ) if apply_url else new_orig

                new_hash = hashlib.sha256(new_orig.encode()).hexdigest()

                to_update.append({
                    "id": row["id"],
                    "original_url": new_orig,
                    "apply_url": new_apply,
                    "url_hash": new_hash,
                })
                updated += 1

            if to_update and not dry_run:
                with transaction.atomic():
                    for item in to_update:
                        RawJob.objects.filter(pk=item["id"]).update(
                            original_url=item["original_url"],
                            apply_url=item["apply_url"],
                            url_hash=item["url_hash"],
                        )

            offset += batch_size
            if dry_run:
                self.stdout.write(f"  [DRY RUN] batch {offset}/{total} — would update {len(to_update)}")
                if to_update:
                    sample = to_update[0]
                    self.stdout.write(f"    Sample: {sample['original_url'][:90]}")
            else:
                self.stdout.write(f"  Updated {offset:,}/{total:,} checked — {updated:,} fixed so far")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone! updated={updated:,} | no_label={skipped_no_label:,} | already_ok={skipped_already_ok:,}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes saved"))

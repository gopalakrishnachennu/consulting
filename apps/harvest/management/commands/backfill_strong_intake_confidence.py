"""
Backfill category_confidence for STRONG intake RawJobs.

After STRONG-only intake, legacy domain-regex confidence (often 0%) is no longer
used for gating — but stored values and analytics are cleaner when phrase-matched
rows are stamped with STRONG_INTAKE_CONFIDENCE (0.92).

Safety:
  - dry-run by default
  - only touches filter_decision=STRONG rows
  - never lowers an existing higher confidence
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from harvest.selective_intake import STRONG_INTAKE_CONFIDENCE
from harvest.role_filter import STRONG


class Command(BaseCommand):
    help = (
        "Set category_confidence to intake phrase-match level (0.92) for STRONG RawJobs "
        "that are missing or below that value."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write updates. Without this flag the command is a dry-run.",
        )
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument("--limit", type=int, default=0, help="Cap rows updated (0 = all).")
        parser.add_argument("--id-gt", type=int, default=0, help="Only RawJobs with id > this value.")
        parser.add_argument("--id-lte", type=int, default=0, help="Only RawJobs with id <= this value.")

    def handle(self, *args, **options):
        from harvest.models import RawJob

        apply = bool(options["apply"])
        batch_size = max(1, int(options["batch_size"] or 2000))
        limit = max(0, int(options["limit"] or 0))
        id_gt = max(0, int(options["id_gt"] or 0))
        id_lte = max(0, int(options["id_lte"] or 0))

        qs = RawJob.objects.filter(filter_decision=STRONG).filter(
            Q(category_confidence__isnull=True)
            | Q(category_confidence__lt=STRONG_INTAKE_CONFIDENCE)
        )
        if id_gt:
            qs = qs.filter(id__gt=id_gt)
        if id_lte:
            qs = qs.filter(id__lte=id_lte)
        qs = qs.order_by("id")

        total = qs.count()
        null_count = qs.filter(category_confidence__isnull=True).count()
        low_count = total - null_count

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nBackfill STRONG intake confidence"
        ))
        self.stdout.write(f"  Target confidence: {STRONG_INTAKE_CONFIDENCE}")
        self.stdout.write(f"  STRONG rows needing backfill: {total:,}")
        self.stdout.write(f"    NULL category_confidence: {null_count:,}")
        self.stdout.write(f"    Below {STRONG_INTAKE_CONFIDENCE}: {low_count:,}")

        if total == 0:
            self.stdout.write(self.style.SUCCESS("\nNothing to backfill."))
            return

        if not apply:
            sample = list(qs.values_list("id", "title", "category_confidence")[:5])
            if sample:
                self.stdout.write("\n  Sample rows:")
                for pk, title, conf in sample:
                    self.stdout.write(f"    #{pk}  conf={conf!r}  {title[:72]}")
            self.stdout.write(self.style.NOTICE(
                "\nDRY-RUN — no rows updated. Re-run with --apply to write.\n"
            ))
            return

        to_process = list(qs.values_list("id", flat=True))
        if limit:
            to_process = to_process[:limit]

        updated = 0
        for i in range(0, len(to_process), batch_size):
            chunk_ids = to_process[i : i + batch_size]
            rows = [
                RawJob(pk=pk, category_confidence=STRONG_INTAKE_CONFIDENCE)
                for pk in chunk_ids
            ]
            RawJob.objects.bulk_update(rows, ["category_confidence"])
            updated += len(chunk_ids)
            self.stdout.write(f"  …updated {updated:,} / {len(to_process):,}")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone — set category_confidence={STRONG_INTAKE_CONFIDENCE} on {updated:,} STRONG row(s)."
        ))

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from harvest.models import HarvestFilterSnapshot, RawJob
from harvest.role_filter import AMBIGUOUS, COLD, NO_MATCH, classify_title, classify_title_v2


class Command(BaseCommand):
    help = "Re-classify RawJobs whose stored filter snapshot hash differs from current rules."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument(
            "--include-unclassified",
            action="store_true",
            help="Also classify RawJobs with NULL filter_decision or filter_snapshot_id.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        limit = max(0, int(options["limit"] or 0))
        batch_size = max(1, int(options["batch_size"] or 1000))
        current = HarvestFilterSnapshot.create_snapshot(notes="reclassify_stale_rawjobs")
        categories = current.get_categories()
        hard_negatives = current.get_hard_negatives()
        from harvest.models import HarvestEngineConfig
        engine_cfg = HarvestEngineConfig.get()

        current_hash = current.phrase_hash
        stale_snapshot_ids = set(
            HarvestFilterSnapshot.objects.exclude(phrase_hash=current_hash)
            .values_list("snapshot_id", flat=True)
        )
        stale_q = Q(filter_snapshot_id__in=stale_snapshot_ids) | Q(title_gate_decision__isnull=True)
        if options["include_unclassified"]:
            stale_q |= Q(filter_snapshot_id__isnull=True) | Q(filter_decision__isnull=True)
        qs = RawJob.objects.select_related("platform_label").filter(stale_q).order_by("pk")
        if limit:
            qs = qs[:limit]

        counts: dict[str, int] = {}
        updates: list[RawJob] = []
        scanned = 0
        for raw_job in qs.iterator(chunk_size=batch_size):
            scanned += 1
            label = raw_job.platform_label
            result = classify_title(
                title=raw_job.title,
                department=raw_job.department,
                categories=categories,
                hard_negatives=hard_negatives,
                custom_phrases=(label.custom_include_phrases if label else []) or [],
                snapshot_id=str(current.snapshot_id),
            )
            title_gate = classify_title_v2(
                title=raw_job.title,
                department=raw_job.department,
                categories=categories,
                hard_negatives=hard_negatives,
                custom_phrases=(label.custom_include_phrases if label else []) or [],
                snapshot_id=str(current.snapshot_id),
                hard_yes_threshold=float(getattr(engine_cfg, "title_hard_yes_confidence", 0.80) or 0.80),
            )
            counts[result.decision] = counts.get(result.decision, 0) + 1
            if dry_run:
                continue
            raw_job.role_category = result.category
            raw_job.filter_decision = result.decision
            raw_job.filter_reason = result.reason[:512]
            raw_job.filter_snapshot_id = current.snapshot_id
            raw_job.is_cold = result.decision in {COLD, NO_MATCH}
            raw_job.jd_fetch_skipped = (
                result.decision in {COLD, NO_MATCH}
                and not raw_job.has_description
            )
            raw_job.title_gate_decision = title_gate.gate_decision
            raw_job.title_gate_confidence = title_gate.gate_confidence
            if title_gate.gate_decision == AMBIGUOUS and getattr(engine_cfg, "jd_gate_enabled", False):
                if raw_job.jd_gate_decision not in {"CONFIRMED", "REJECTED", "UNCERTAIN"}:
                    raw_job.jd_gate_decision = "PENDING"
            elif raw_job.jd_gate_decision == "PENDING" and title_gate.gate_decision != AMBIGUOUS:
                raw_job.jd_gate_decision = None
            updates.append(raw_job)
            if len(updates) >= batch_size:
                self._flush(updates)
                updates.clear()
        if updates and not dry_run:
            self._flush(updates)
        self.stdout.write(self.style.SUCCESS(
            f"snapshot={current.snapshot_id} dry_run={dry_run} scanned={scanned} counts={counts}"
        ))

    @staticmethod
    def _flush(rows: list[RawJob]):
        with transaction.atomic():
            RawJob.objects.bulk_update(
                rows,
                [
                    "role_category",
                    "filter_decision",
                    "filter_reason",
                    "filter_snapshot_id",
                    "is_cold",
                    "jd_fetch_skipped",
                    "title_gate_decision",
                    "title_gate_confidence",
                    "jd_gate_decision",
                ],
            )

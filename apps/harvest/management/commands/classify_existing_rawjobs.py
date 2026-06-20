from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from harvest.models import HarvestFilterSnapshot, RawJob
from harvest.role_filter import AMBIGUOUS, COLD, NO_MATCH, classify_title, classify_title_v2


class Command(BaseCommand):
    help = "Classify existing RawJobs with the current selective harvest role filter."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report counts without updating rows.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum rows to scan. 0 means all.")
        parser.add_argument("--batch-size", type=int, default=1000, help="Bulk update batch size.")
        parser.add_argument(
            "--only-unclassified",
            action="store_true",
            help="Only classify rows where filter_decision is NULL.",
        )
        parser.add_argument(
            "--only-missing-title-gate",
            action="store_true",
            help="Only classify rows where title_gate_decision is NULL.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        limit = max(0, int(options["limit"] or 0))
        batch_size = max(1, int(options["batch_size"] or 1000))
        snapshot = HarvestFilterSnapshot.create_snapshot(notes="classify_existing_rawjobs")
        categories = snapshot.get_categories()
        hard_negatives = snapshot.get_hard_negatives()
        from harvest.models import HarvestEngineConfig
        engine_cfg = HarvestEngineConfig.get()

        qs = RawJob.objects.select_related("platform_label").order_by("pk")
        if options["only_unclassified"]:
            qs = qs.filter(filter_decision__isnull=True)
        if options["only_missing_title_gate"]:
            qs = qs.filter(title_gate_decision__isnull=True)
        if limit:
            qs = qs[:limit]

        counts: dict[str, int] = {}
        updates: list[RawJob] = []
        for raw_job in qs.iterator(chunk_size=1000):
            label = raw_job.platform_label
            custom_phrases = label.custom_include_phrases if label else []
            result = classify_title(
                title=raw_job.title,
                department=raw_job.department,
                categories=categories,
                hard_negatives=hard_negatives,
                custom_phrases=custom_phrases or [],
                snapshot_id=str(snapshot.snapshot_id),
            )
            title_gate = classify_title_v2(
                title=raw_job.title,
                department=raw_job.department,
                categories=categories,
                hard_negatives=hard_negatives,
                custom_phrases=custom_phrases or [],
                snapshot_id=str(snapshot.snapshot_id),
                hard_yes_threshold=float(getattr(engine_cfg, "title_hard_yes_confidence", 0.80) or 0.80),
            )
            counts[result.decision] = counts.get(result.decision, 0) + 1
            if dry_run:
                continue
            raw_job.role_category = result.category
            raw_job.filter_decision = result.decision
            raw_job.filter_reason = result.reason[:512]
            raw_job.filter_snapshot_id = snapshot.snapshot_id
            raw_job.is_cold = result.decision in {COLD, NO_MATCH}
            raw_job.jd_fetch_skipped = (
                result.decision in {COLD, NO_MATCH}
                and not raw_job.has_description
            )
            raw_job.title_gate_decision = title_gate.gate_decision
            raw_job.title_gate_confidence = title_gate.gate_confidence
            if title_gate.gate_decision == AMBIGUOUS and getattr(engine_cfg, "jd_gate_enabled", False):
                if raw_job.jd_gate_decision in {"CONFIRMED", "REJECTED", "UNCERTAIN"}:
                    pass
                else:
                    raw_job.jd_gate_decision = "PENDING"
            elif raw_job.jd_gate_decision == "PENDING" and title_gate.gate_decision != AMBIGUOUS:
                raw_job.jd_gate_decision = None
            updates.append(raw_job)
            if len(updates) >= batch_size:
                with transaction.atomic():
                    RawJob.objects.bulk_update(
                        updates,
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
                updates.clear()

        if updates and not dry_run:
            with transaction.atomic():
                RawJob.objects.bulk_update(
                    updates,
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

        self.stdout.write(self.style.SUCCESS(f"snapshot={snapshot.snapshot_id} dry_run={dry_run} counts={counts}"))

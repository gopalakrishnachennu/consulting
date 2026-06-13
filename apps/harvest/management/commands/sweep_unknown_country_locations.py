from __future__ import annotations

from collections import Counter

from django.db import transaction

from harvest.location_resolver import evaluate_rawjob_scope
from harvest.models import HarvestEngineConfig, HarvestOpsRun, RawJob

from ._ops_base import OpsTrackedCommand


class Command(OpsTrackedCommand):
    """Re-run scope resolution over the Unknown-Country review backlog using the
    CURRENT resolver (expanded gazetteer + remote policy). Unlike
    refetch_ambiguous_locations this does NOT hit the network — it re-evaluates
    the data already on each RawJob, so it is fast and safe. Pass --provider to
    additionally allow on-demand Mapbox for the residual (quota-capped)."""

    help = "Re-resolve the REVIEW_UNKNOWN_COUNTRY backlog with the current offline resolver (+ optional Mapbox)."
    ops_operation = HarvestOpsRun.Operation.EVALUATE_SCOPE

    def add_arguments(self, parser):
        parser.add_argument("--platform", default="", help="Limit to one platform slug.")
        parser.add_argument("--limit", type=int, default=0, help="Maximum rows to process (0 = all).")
        parser.add_argument("--batch-size", type=int, default=1000, help="Bulk update batch size.")
        parser.add_argument("--include-inactive", action="store_true", help="Also sweep delisted (is_active=False) rows.")
        parser.add_argument("--provider", action="store_true", help="Allow on-demand Mapbox for the residual (quota-capped).")
        parser.add_argument("--dry-run", action="store_true", help="Resolve and report without writing.")

    def handle(self, *args, **options):
        cfg = HarvestEngineConfig.get()
        qs = RawJob.objects.filter(scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
        if not options["include_inactive"]:
            qs = qs.filter(is_active=True)
        if options["platform"]:
            qs = qs.filter(platform_slug=options["platform"])
        qs = qs.order_by("id").only(
            "id", "location_raw", "city", "state", "country", "vendor_location_block",
            "raw_payload", "location_candidates", "title", "description",
            "description_clean", "job_domain", "job_domain_candidates",
            "job_category", "department_normalized",
        )
        if options["limit"] and options["limit"] > 0:
            qs = qs[: options["limit"]]

        batch_size = max(100, min(int(options["batch_size"] or 1000), 5000))
        force_provider = bool(options["provider"])
        dry_run = bool(options["dry_run"])

        total_hint = qs.count() if hasattr(qs, "count") else 0
        self.stdout.write(
            f"Sweeping unknown-country backlog: rows~{total_hint:,}, platform={options['platform'] or 'all'}, "
            f"provider={force_provider}, include_inactive={options['include_inactive']}, dry_run={dry_run}"
        )
        self.ops_start(total=total_hint, message=f"Re-resolving ~{total_hint:,} unknown-country jobs…")

        fields = [
            "country_code", "country_confidence", "country_source", "country_codes",
            "location_candidates", "scope_status", "scope_reason", "is_priority",
            "last_scope_evaluated_at", "country", "state", "city", "location_raw",
        ]
        outcomes = Counter()
        buffer: list[RawJob] = []

        def flush():
            if buffer and not dry_run:
                with transaction.atomic():
                    RawJob.objects.bulk_update(buffer, fields, batch_size=batch_size)
            buffer.clear()

        processed = resolved = 0
        iterator = qs.iterator(chunk_size=batch_size) if not isinstance(qs, list) else iter(qs)
        for raw_job in iterator:
            updates = evaluate_rawjob_scope(
                raw_job, cfg=cfg, use_provider=force_provider or None,
                force_provider=force_provider, save=False,
            )
            new_status = updates.get("scope_status") or RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY
            outcomes[new_status] += 1
            if new_status != RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY:
                resolved += 1
            for field, value in updates.items():
                setattr(raw_job, field, value)
            buffer.append(raw_job)
            processed += 1
            if len(buffer) >= batch_size:
                flush()
                self.ops_progress(processed)
                self.stdout.write(f"Processed {processed:,}; cleared {resolved:,}...")

        flush()
        self.ops_progress(processed)
        self.stdout.write(self.style.SUCCESS(
            f"Done. processed={processed:,}; cleared_from_review={resolved:,}"
        ))
        for status, n in outcomes.most_common():
            self.stdout.write(f"  → {status}: {n:,}")

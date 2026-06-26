"""
Purge (soft-deactivate) stored RawJobs whose title does NOT match one of your
selective-filter categories — the legacy backlog that predates the pre-storage
gate (e.g. "Teller", "Territory Manager").

Safety model:
  • DRY-RUN by default. Nothing changes unless you pass --apply.
  • SOFT delete by default (is_active=False), which is reversible. Pass
    --hard-delete to actually DELETE (not recommended).
  • NEVER touches rows already promoted to the live pool (sync_status=SYNCED).
  • Only acts on rows that are already CLASSIFIED. Rows with no filter_decision
    (the "missing title-gate state" backlog) are reported, not purged — run
    `classify_existing_rawjobs` first to classify them, then re-run this.

Decisions purged:
  • default : COLD + NO_MATCH         (clearly non-tech / excluded)
  • --strict: + POSSIBLE + UNKNOWN    (STRONG category matches only)

Examples:
  python manage.py purge_non_matching_rawjobs                 # dry-run, COLD+NO_MATCH
  python manage.py purge_non_matching_rawjobs --strict        # dry-run, STRONG-only
  python manage.py purge_non_matching_rawjobs --strict --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count


class Command(BaseCommand):
    help = "Soft-deactivate stored RawJobs whose title does not match a selective-filter category."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually perform the change. Without this it is a dry-run.")
        parser.add_argument("--strict", action="store_true",
                            help="Also purge POSSIBLE + UNKNOWN (keep STRONG only). "
                                 "Default purges only COLD + NO_MATCH.")
        parser.add_argument("--hard-delete", action="store_true",
                            help="DELETE rows instead of soft-deactivating (is_active=False).")
        parser.add_argument("--limit", type=int, default=0,
                            help="Cap the number of rows affected (0 = no cap).")
        parser.add_argument("--batch-size", type=int, default=2000)

    def handle(self, *args, **opts):
        from harvest.models import RawJob

        apply = opts["apply"]
        strict = opts["strict"]
        hard = opts["hard_delete"]
        limit = opts["limit"]
        batch = max(1, opts["batch_size"])

        decisions = ["COLD", "NO_MATCH"]
        if strict:
            decisions += ["POSSIBLE", "UNKNOWN"]

        qs = (
            RawJob.objects
            .filter(is_active=True, filter_decision__in=decisions)
            .exclude(sync_status=RawJob.SyncStatus.SYNCED)
        )

        total = qs.count()
        by_decision = list(
            qs.values("filter_decision").annotate(n=Count("id")).order_by("-n")
        )
        unclassified = RawJob.objects.filter(
            is_active=True, filter_decision__isnull=True
        ).count()

        mode = "STRICT (STRONG-only)" if strict else "default (COLD + NO_MATCH)"
        action = "HARD DELETE" if hard else "soft-deactivate (is_active=False)"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nPurge non-matching RawJobs — mode: {mode} — action: {action}"
        ))
        self.stdout.write(f"  Candidates (active, classified, not live): {total:,}")
        for row in by_decision:
            self.stdout.write(f"    {row['filter_decision']:<10} {row['n']:,}")
        if limit:
            self.stdout.write(f"  --limit set: will affect at most {limit:,}")
        if unclassified:
            self.stdout.write(self.style.WARNING(
                f"  NOTE: {unclassified:,} active rows have NO filter_decision yet "
                f"(not counted above). Run `classify_existing_rawjobs` first to classify them, "
                f"then re-run this command to include them."
            ))

        if not apply:
            self.stdout.write(self.style.NOTICE(
                "\nDRY-RUN — nothing changed. Re-run with --apply to perform the purge.\n"
            ))
            return

        pks = list(qs.values_list("id", flat=True)[: (limit or None)])
        affected = 0
        for i in range(0, len(pks), batch):
            chunk = pks[i:i + batch]
            if hard:
                RawJob.objects.filter(id__in=chunk).delete()
            else:
                RawJob.objects.filter(id__in=chunk).update(is_active=False)
            affected += len(chunk)
            self.stdout.write(f"  …{affected:,}/{len(pks):,}")

        verb = "deleted" if hard else "deactivated"
        self.stdout.write(self.style.SUCCESS(f"\nDone — {verb} {affected:,} RawJob(s)."))
        if not hard:
            self.stdout.write(
                "  (Soft delete: reversible by setting is_active=True. Note the existing "
                "cleanup task may permanently remove is_active=False PENDING rows later.)"
            )

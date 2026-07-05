"""
print_scope_summary
===================
Aggregates the current RawJob scope distribution + top countries +
LocationCache totals + sync-readiness / gate breakdown. Read-only. Fast.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone


class Command(BaseCommand):
    help = "Print current RawJob scope distribution and sync-gate metrics"

    def handle(self, *args, **options):
        from datetime import timedelta

        from harvest.models import RawJob, LocationCache
        from harvest.tasks import _backfill_eligible_queryset, get_jd_backfill_lock_stale_minutes

        total = RawJob.objects.count()
        self.stdout.write(f"Total RawJobs: {total:,}")
        self.stdout.write("")

        # ── Scope status breakdown ────────────────────────────────────────────
        self.stdout.write("Scope status breakdown:")
        for row in (
            RawJob.objects.values("scope_status").annotate(c=Count("id")).order_by("-c")
        ):
            label = (row["scope_status"] or "(empty)").ljust(30)
            count = format(row["c"], ",").rjust(10)
            self.stdout.write(f"  {label}{count}")

        # ── Sync-gate summary ─────────────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write("Sync-gate eligible (is_priority=True + PRIORITY_TARGET | REVIEW_UNKNOWN_COUNTRY):")
        passable_qs = RawJob.objects.filter(
            is_priority=True,
            scope_status__in=[
                RawJob.ScopeStatus.PRIORITY_TARGET,
                RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY,
            ],
        )
        agg = passable_qs.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(sync_status="PENDING")),
            synced=Count("id", filter=Q(sync_status="SYNCED")),
            failed=Count("id", filter=Q(sync_status="FAILED")),
            missing_jd=Count("id", filter=Q(has_description=False)),
            active=Count("id", filter=Q(is_active=True)),
        )
        w = 28
        self.stdout.write(f"  {'Gate-eligible total'.ljust(w)}{format(agg['total'], ',').rjust(10)}")
        self.stdout.write(f"  {'  is_active'.ljust(w)}{format(agg['active'], ',').rjust(10)}")
        self.stdout.write(f"  {'  sync PENDING'.ljust(w)}{format(agg['pending'], ',').rjust(10)}")
        self.stdout.write(f"  {'  sync SYNCED'.ljust(w)}{format(agg['synced'], ',').rjust(10)}")
        self.stdout.write(f"  {'  sync FAILED'.ljust(w)}{format(agg['failed'], ',').rjust(10)}")
        self.stdout.write(f"  {'  missing JD'.ljust(w)}{format(agg['missing_jd'], ',').rjust(10)}")

        cold_total = RawJob.objects.filter(
            scope_status__in=[
                RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY,
                RawJob.ScopeStatus.COLD_NO_LOCATION,
            ]
        ).count()
        unscoped = RawJob.objects.filter(
            Q(scope_status="") | Q(scope_status=RawJob.ScopeStatus.UNSCOPED)
        ).count()
        unknown = RawJob.objects.filter(
            scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY
        ).count()
        self.stdout.write("")
        self.stdout.write(f"  {'Cold (gate-blocked)'.ljust(w)}{format(cold_total, ',').rjust(10)}")
        self.stdout.write(f"  {'REVIEW_UNKNOWN_COUNTRY'.ljust(w)}{format(unknown, ',').rjust(10)}")
        self.stdout.write(f"  {'Unscoped (never evaluated)'.ljust(w)}{format(unscoped, ',').rjust(10)}")

        # ── Missing JD / backfill backlog ─────────────────────────────────────
        self.stdout.write("")
        self.stdout.write("Missing JD backlog (active pending, not cold/skipped):")
        pending_active = RawJob.objects.filter(
            is_test_run=False,
            is_active=True,
            has_description=False,
        ).exclude(original_url="").exclude(
            Q(is_cold=True) | Q(jd_fetch_skipped=True) | Q(filter_decision="NO_MATCH")
        )
        strong_pending = pending_active.filter(filter_decision="STRONG")
        strong_non_priority = strong_pending.filter(is_priority=False)
        priority_only = pending_active.filter(is_priority=True)
        backfill_eligible = _backfill_eligible_queryset(None)
        now = timezone.now()
        age_buckets = [
            ("< 1 hour", now - timedelta(hours=1)),
            ("1–6 hours", now - timedelta(hours=6)),
            ("6–24 hours", now - timedelta(hours=24)),
            ("> 24 hours", None),
        ]
        self.stdout.write(f"  {'All missing JD (pending active)'.ljust(w)}{format(pending_active.count(), ',').rjust(10)}")
        self.stdout.write(f"  {'  STRONG + missing JD'.ljust(w)}{format(strong_pending.count(), ',').rjust(10)}")
        self.stdout.write(
            f"  {'  STRONG + missing JD + is_priority=False'.ljust(w)}"
            f"{format(strong_non_priority.count(), ',').rjust(10)}"
            f"  ← newly eligible for backfill"
        )
        self.stdout.write(f"  {'  is_priority=True + missing JD'.ljust(w)}{format(priority_only.count(), ',').rjust(10)}")
        self.stdout.write(f"  {'Backfill-eligible now (worker queue)'.ljust(w)}{format(backfill_eligible.count(), ',').rjust(10)}")
        self.stdout.write(f"  {'  (was priority-only before STRONG rule)'.ljust(w)}{format(priority_only.count(), ',').rjust(10)}")
        self.stdout.write("")
        self.stdout.write("STRONG missing JD by age (fetched_at):")
        for label, cutoff in age_buckets:
            qs = strong_pending
            if cutoff is None:
                qs = qs.filter(fetched_at__lt=now - timedelta(hours=24))
            elif label == "< 1 hour":
                qs = qs.filter(fetched_at__gte=cutoff)
            elif label == "1–6 hours":
                qs = qs.filter(fetched_at__gte=cutoff, fetched_at__lt=now - timedelta(hours=1))
            elif label == "6–24 hours":
                qs = qs.filter(fetched_at__gte=cutoff, fetched_at__lt=now - timedelta(hours=6))
            self.stdout.write(f"  {label.ljust(w)}{format(qs.count(), ',').rjust(10)}")
        locked = strong_pending.filter(jd_backfill_locked_at__isnull=False)
        stale_mins = get_jd_backfill_lock_stale_minutes()
        stale_before = now - timedelta(minutes=stale_mins)
        actively_fetching = locked.filter(jd_backfill_locked_at__gte=stale_before)
        self.stdout.write("")
        self.stdout.write(f"  {'STRONG locked (fetching/cooldown)'.ljust(w)}{format(locked.count(), ',').rjust(10)}")
        self.stdout.write(f"  {'  actively fetching'.ljust(w)}{format(actively_fetching.count(), ',').rjust(10)}")

        # ── Top countries ─────────────────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write("Top 15 countries (by country_code):")
        for row in (
            RawJob.objects.exclude(country_code="")
            .values("country_code")
            .annotate(c=Count("id"))
            .order_by("-c")[:15]
        ):
            self.stdout.write(
                f"  {row['country_code'].ljust(6)}{format(row['c'], ',').rjust(10)}"
            )

        # ── LocationCache ─────────────────────────────────────────────────────
        self.stdout.write("")
        cache_total = LocationCache.objects.count()
        cache_mapbox = LocationCache.objects.filter(provider="mapbox").count()
        cache_rules = LocationCache.objects.filter(source="rules").count()
        self.stdout.write(
            f"LocationCache: {cache_total:,} rows ({cache_mapbox:,} mapbox, {cache_rules:,} rules)"
        )

"""
Permanently remove RawJobs (and their vetting Jobs) that do not match intake rules.

CHENN policy: only filter_decision=STRONG rows belong in the system.
Everything else (COLD, POSSIBLE, NO_MATCH, UNKNOWN, unclassified) is deleted.

Safety:
  - dry-run by default
  - vetting Jobs with submissions / resume drafts / cover letters / saves are
    archived, not deleted, and their source RawJob is kept until manually reviewed
  - MATCHED / FILLED jobs are never deleted
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count, Q


class Command(BaseCommand):
    help = "Hard-delete non-STRONG RawJobs and remove linked vetting noise. STRONG-only intake."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform deletes. Without this flag the command is a dry-run.",
        )
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument(
            "--reclassify-first",
            action="store_true",
            help="Re-run classify_existing_rawjobs before counting/deleting.",
        )

    def handle(self, *args, **options):
        from harvest.models import RawJob
        from jobs.models import Job

        apply = bool(options["apply"])
        batch_size = max(1, int(options["batch_size"] or 2000))

        if options["reclassify_first"]:
            from django.core.management import call_command

            self.stdout.write(self.style.MIGRATE_HEADING("\nReclassifying all RawJobs…"))
            call_command("classify_existing_rawjobs", batch_size=batch_size)

        non_strong = RawJob.objects.exclude(filter_decision="STRONG")
        total_raw = non_strong.count()
        by_decision = list(
            non_strong.values("filter_decision")
            .annotate(n=Count("id"))
            .order_by("-n")
        )

        linked_jobs = Job.objects.filter(source_raw_job__in=non_strong)
        has_work = (
            Q(submissions__isnull=False)
            | Q(resume_drafts__isnull=False)
            | Q(cover_letters__isnull=False)
            | Q(saved_by__isnull=False)
        )
        inflight = [Job.Stage.MATCHED, Job.Stage.FILLED]
        protected_jobs = linked_jobs.filter(has_work | Q(stage__in=inflight)).distinct()
        deletable_jobs = linked_jobs.exclude(
            id__in=protected_jobs.values_list("id", flat=True)
        ).distinct()

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nHard purge non-STRONG — STRONG-only intake cleanup"
        ))
        self.stdout.write(f"  Non-STRONG RawJobs to delete: {total_raw:,}")
        for row in by_decision:
            label = row["filter_decision"] or "NULL"
            self.stdout.write(f"    {label:<10} {row['n']:,}")
        self.stdout.write(f"  Linked vetting Jobs to delete: {deletable_jobs.count():,}")
        self.stdout.write(self.style.WARNING(
            f"  Protected vetting Jobs (consultant work / in-flight): {protected_jobs.count():,}"
        ))
        if protected_jobs.exists():
            self.stdout.write(
                "    Their source RawJobs will NOT be deleted automatically."
            )

        if not apply:
            self.stdout.write(self.style.NOTICE(
                "\nDRY-RUN — nothing deleted. Re-run with --apply to permanently remove rows.\n"
            ))
            return

        deleted_jobs = 0
        job_pks = list(deletable_jobs.values_list("id", flat=True))
        for i in range(0, len(job_pks), batch_size):
            chunk = job_pks[i : i + batch_size]
            deleted_jobs += Job.objects.filter(id__in=chunk).delete()[0]
            self.stdout.write(f"  …deleted {deleted_jobs:,} vetting Job(s)")

        protected_raw_ids = set(
            protected_jobs.values_list("source_raw_job_id", flat=True)
        )
        raw_pks = [
            pk
            for pk in non_strong.values_list("id", flat=True)
            if pk not in protected_raw_ids
        ]
        deleted_raw = 0
        for i in range(0, len(raw_pks), batch_size):
            chunk = raw_pks[i : i + batch_size]
            deleted_raw += RawJob.objects.filter(id__in=chunk).delete()[0]
            self.stdout.write(f"  …deleted {deleted_raw:,} RawJob row(s)")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone — deleted {deleted_raw:,} RawJob(s) and {deleted_jobs:,} vetting Job(s)."
        ))
        if protected_jobs.exists():
            self.stdout.write(self.style.WARNING(
                f"{protected_jobs.count():,} vetting Job(s) with consultant work were left untouched."
            ))

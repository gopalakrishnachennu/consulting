"""
Soft-archive pooled / vetting Jobs whose source RawJob is NOT a STRONG category
match — the counterpart to `purge_non_matching_rawjobs`, for the pool/vetting
stage.

Why this is more careful than the raw purge:
  A live Job has Submission, ResumeDraft, CoverLetter and SavedJob pointed at it
  with on_delete=CASCADE. Hard-deleting a Job would silently destroy real
  consultant work. So this command:

  • DRY-RUN by default. Nothing changes unless you pass --apply.
  • SOFT archive by default (is_archived=True, stage=ARCHIVED) — reversible.
  • PROTECTS any job that has attached work (a submission / resume draft / cover
    letter / save) or is in an in-flight stage (MATCHED / FILLED). Those are
    reported as exceptions and NEVER touched automatically.
  • EXEMPTS manual jobs (no source_raw_job).
  • Also DEACTIVATES the source RawJob (is_active=False) so the sync task does
    not re-create the job — pass --keep-raw to skip that.

Match set:
  • default : source RawJob decision COLD + NO_MATCH
  • --strict: + POSSIBLE + UNKNOWN   (keep STRONG only)

Examples:
  python manage.py purge_non_matching_jobs --strict           # dry-run
  python manage.py purge_non_matching_jobs --strict --apply    # soft-archive
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q


class Command(BaseCommand):
    help = "Soft-archive pooled Jobs whose source RawJob is not a STRONG category match."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Actually perform the change. Without this it is a dry-run.")
        parser.add_argument("--strict", action="store_true",
                            help="Also archive POSSIBLE + UNKNOWN source jobs (keep STRONG only).")
        parser.add_argument("--hard-delete", action="store_true",
                            help="DELETE jobs instead of archiving. CASCADES to submissions/"
                                 "resumes/cover letters/saves — not recommended.")
        parser.add_argument("--keep-raw", action="store_true",
                            help="Do NOT deactivate the source RawJob (default deactivates it "
                                 "so the job is not re-synced).")
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--batch-size", type=int, default=1000)

    def handle(self, *args, **opts):
        from jobs.models import Job
        from harvest.models import RawJob

        apply = opts["apply"]
        strict = opts["strict"]
        hard = opts["hard_delete"]
        keep_raw = opts["keep_raw"]
        limit = opts["limit"]
        batch = max(1, opts["batch_size"])

        decisions = ["COLD", "NO_MATCH"]
        if strict:
            decisions += ["POSSIBLE", "UNKNOWN"]

        # Off-category, harvest-sourced, not already archived.
        base = Job.objects.filter(
            is_archived=False,
            source_raw_job__isnull=False,
            source_raw_job__filter_decision__in=decisions,
        )

        # Protect jobs with attached consultant work or in-flight stages.
        has_work = (
            Q(submissions__isnull=False)
            | Q(resume_drafts__isnull=False)
            | Q(cover_letters__isnull=False)
            | Q(saved_by__isnull=False)
        )
        inflight = [Job.Stage.MATCHED, Job.Stage.FILLED]
        protected_pks = set(base.filter(has_work).values_list("id", flat=True)) | set(
            base.filter(stage__in=inflight).values_list("id", flat=True)
        )

        purgeable = base.exclude(id__in=protected_pks)
        total = purgeable.count()

        mode = "STRICT (STRONG-only)" if strict else "default (COLD + NO_MATCH)"
        action = "HARD DELETE (cascades!)" if hard else "soft-archive (is_archived=True)"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nPurge non-matching pooled Jobs — mode: {mode} — action: {action}"
        ))
        self.stdout.write(f"  Purgeable (off-category, no attached work, not in-flight): {total:,}")
        self.stdout.write(self.style.WARNING(
            f"  PROTECTED (has submissions/resumes/saves or MATCHED/FILLED) — left untouched: "
            f"{len(protected_pks):,}"
        ))
        if not keep_raw:
            self.stdout.write("  Source RawJobs of purged jobs will be deactivated (stops re-sync).")
        if limit:
            self.stdout.write(f"  --limit set: will affect at most {limit:,}")

        if not apply:
            self.stdout.write(self.style.NOTICE(
                "\nDRY-RUN — nothing changed. Re-run with --apply to perform the purge.\n"
            ))
            return

        pks = list(purgeable.values_list("id", flat=True)[: (limit or None)])
        affected = 0
        for i in range(0, len(pks), batch):
            chunk = pks[i:i + batch]
            jobs = Job.objects.filter(id__in=chunk)
            raw_ids = [
                rid for rid in jobs.values_list("source_raw_job_id", flat=True) if rid
            ]
            if hard:
                jobs.delete()
            else:
                jobs.update(is_archived=True, stage=Job.Stage.ARCHIVED)
            if not keep_raw and raw_ids:
                RawJob.objects.filter(id__in=raw_ids).update(is_active=False)
            affected += len(chunk)
            self.stdout.write(f"  …{affected:,}/{len(pks):,}")

        verb = "deleted" if hard else "archived"
        self.stdout.write(self.style.SUCCESS(f"\nDone — {verb} {affected:,} Job(s)."))
        if not hard:
            self.stdout.write("  (Soft archive: reversible via is_archived=False.)")

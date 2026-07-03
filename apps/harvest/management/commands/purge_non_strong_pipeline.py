"""
One-shot cleanup for CHENN's STRONG-only harvest policy.

Runs both existing purge commands in strict mode:
  1. RawJobs   -> deactivate non-STRONG rows that are not already SYNCED
  2. Vetting   -> archive harvest-sourced non-STRONG Jobs that are safe to touch

Safety:
  - dry-run by default
  - Job purge still protects submissions / drafts / matched / filled work
  - Raw purge still never touches already-SYNCED source rows
"""
from __future__ import annotations

from django.core.management import BaseCommand, call_command


class Command(BaseCommand):
    help = "Dry-run or apply a strict STRONG-only cleanup across RawJobs and vetting Jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually perform the cleanup. Without this it is a dry-run.",
        )
        parser.add_argument(
            "--hard-delete",
            action="store_true",
            help="Pass through to both cleanup commands. Use with extreme care.",
        )
        parser.add_argument(
            "--keep-raw",
            action="store_true",
            help="Do not deactivate source RawJobs when archiving vetting Jobs.",
        )
        parser.add_argument("--raw-limit", type=int, default=0)
        parser.add_argument("--job-limit", type=int, default=0)
        parser.add_argument("--raw-batch-size", type=int, default=2000)
        parser.add_argument("--job-batch-size", type=int, default=1000)

    def handle(self, *args, **opts):
        apply = opts["apply"]
        hard = opts["hard_delete"]
        keep_raw = opts["keep_raw"]

        self.stdout.write(self.style.MIGRATE_HEADING("\nStep 1/2 — RawJobs strict cleanup"))
        raw_kwargs = {
            "strict": True,
            "apply": apply,
            "hard_delete": hard,
            "limit": int(opts["raw_limit"] or 0),
            "batch_size": int(opts["raw_batch_size"] or 2000),
        }
        call_command("purge_non_matching_rawjobs", **raw_kwargs)

        self.stdout.write(self.style.MIGRATE_HEADING("\nStep 2/2 — Vetting Jobs strict cleanup"))
        job_kwargs = {
            "strict": True,
            "apply": apply,
            "hard_delete": hard,
            "keep_raw": keep_raw,
            "limit": int(opts["job_limit"] or 0),
            "batch_size": int(opts["job_batch_size"] or 1000),
        }
        call_command("purge_non_matching_jobs", **job_kwargs)

        if not apply:
            self.stdout.write(self.style.NOTICE("\nDRY-RUN complete — re-run with --apply to enforce cleanup.\n"))
        else:
            self.stdout.write(self.style.SUCCESS("\nStrict STRONG-only cleanup complete.\n"))

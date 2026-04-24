"""
Local Harvesting Agent — management command.

Two modes:

  --mode direct  (default, recommended)
      Connect this clone directly to the production PostgreSQL database.
      The harvesters run on YOUR machine (local CPU, local IP, no server limits)
      but write results straight into prod's RawJob table.
      The prod Celery workers pick them up and run the normal pipeline.

      Usage:
        DATABASE_URL=<prod-postgres-url> \\
        python manage.py harvest_and_push \\
          --mode direct --platform workday --fetch-all --max-companies 500

  --mode push
      Harvest fully offline (no DB needed on local side).
      1. Pulls the company+label list from prod: GET /harvest/api/push/labels/
      2. Runs all harvesters locally (parallel, any speed)
      3. Enriches job dicts in-memory (skills, tech-stack, quality-score)
      4. Pushes batches to prod: POST /harvest/api/push/jobs/
      5. Prod creates RawJobs and triggers sync-to-pool pipeline

      Usage:
        python manage.py harvest_and_push \\
          --mode push \\
          --push-url https://your-prod-domain.com \\
          --push-token <HARVEST_PUSH_SECRET> \\
          --platform workday \\
          --fetch-all \\
          --workers 8
"""

import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger("harvest.agent")


# ── Lightweight company proxy used in push mode (no DB needed) ────────────────

@dataclass
class _LocalCompany:
    """Minimal stand-in for the Company ORM object used by harvesters."""
    name: str
    career_site_url: str = ""
    domain: str = ""
    id: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _chunk(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i: i + size]


def _push_batch(jobs: list[dict], push_url: str, push_token: str, dry_run: bool, stdout) -> dict:
    """POST one batch to the prod push API. Returns response dict."""
    if dry_run:
        stdout.write(f"    [dry-run] would push {len(jobs)} jobs")
        return {"received": len(jobs), "created": 0, "skipped": 0, "errors": 0}

    import urllib.request
    import urllib.error

    url = push_url.rstrip("/") + "/harvest/api/push/jobs/"
    body = json.dumps({"jobs": jobs, "trigger_pipeline": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {push_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CommandError(
            f"push API returned HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CommandError(f"push API connection error: {exc.reason}") from exc


def _fetch_labels(push_url: str, push_token: str, platform: str, limit: int) -> list[dict]:
    """GET /harvest/api/push/labels/ from prod."""
    import urllib.request
    import urllib.parse
    import urllib.error

    params = {"limit": str(limit)}
    if platform:
        params["platform"] = platform
    url = push_url.rstrip("/") + "/harvest/api/push/labels/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {push_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("labels", [])
    except urllib.error.HTTPError as exc:
        raise CommandError(
            f"labels API returned HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CommandError(f"labels API connection error: {exc.reason}") from exc


def _harvest_one_company(label: dict, since_hours: int, fetch_all: bool) -> list[dict]:
    """
    Run the platform harvester for one company label.
    Returns a list of enriched job dicts ready for pushing.
    Runs in a thread — all imports are inside the function to be thread-safe.
    """
    from harvest.platform_engine import get_harvester
    from harvest.enrichments import extract_enrichments

    platform_slug = label.get("platform_slug", "")
    tenant_id = label.get("tenant_id", "")
    company_name = label.get("company_name", "")

    if not platform_slug or not tenant_id:
        return []

    harvester = get_harvester(platform_slug)
    if harvester is None:
        return []

    local_company = _LocalCompany(
        name=company_name,
        career_site_url=label.get("career_url", ""),
        domain=label.get("domain", ""),
    )

    try:
        raw_jobs: list[dict] = harvester.fetch_jobs(
            local_company,
            tenant_id,
            since_hours=since_hours,
            fetch_all=fetch_all,
        )
    except Exception as exc:
        logger.warning("harvest_agent: %s/%s fetch error: %s", company_name, platform_slug, exc)
        return []

    enriched: list[dict] = []
    for raw in raw_jobs:
        try:
            original_url = str(raw.get("original_url", "")).strip()
            if not original_url:
                continue

            url_hash = raw.get("url_hash") or _compute_url_hash(original_url)

            # Inline enrichment (pure Python, no DB, no HTTP)
            extras = extract_enrichments({
                "title": raw.get("title", ""),
                "description": raw.get("description", ""),
                "requirements": raw.get("requirements", ""),
                "benefits": raw.get("benefits", ""),
            })

            job = {
                "url_hash": url_hash,
                "external_id": str(raw.get("external_id", ""))[:512],
                "original_url": original_url,
                "apply_url": str(raw.get("apply_url", ""))[:1024],
                "title": str(raw.get("title", ""))[:512],
                "company_name": company_name,
                "department": str(raw.get("department", ""))[:256],
                "team": str(raw.get("team", ""))[:256],
                "location_raw": str(raw.get("location_raw", ""))[:512],
                "city": str(raw.get("city", ""))[:128],
                "state": str(raw.get("state", ""))[:128],
                "country": str(raw.get("country", ""))[:128],
                "postal_code": str(raw.get("postal_code", ""))[:32],
                "location_type": raw.get("location_type", "UNKNOWN"),
                "is_remote": bool(raw.get("is_remote", False)),
                "employment_type": raw.get("employment_type", "UNKNOWN"),
                "experience_level": raw.get("experience_level", "UNKNOWN"),
                "salary_min": raw.get("salary_min"),
                "salary_max": raw.get("salary_max"),
                "salary_currency": str(raw.get("salary_currency", "USD"))[:8],
                "salary_period": str(raw.get("salary_period", ""))[:16],
                "salary_raw": str(raw.get("salary_raw", ""))[:256],
                "description": raw.get("description", ""),
                "requirements": raw.get("requirements", ""),
                "benefits": raw.get("benefits", ""),
                "posted_date": str(raw.get("posted_date", "") or ""),
                "closing_date": str(raw.get("closing_date", "") or ""),
                "platform_slug": platform_slug,
                "raw_payload": raw.get("raw_payload") or {},
                # Enrichment (computed locally)
                "skills": extras.get("skills", []),
                "tech_stack": extras.get("tech_stack", []),
                "job_category": extras.get("job_category", ""),
                "years_required": extras.get("years_required"),
                "years_required_max": extras.get("years_required_max"),
                "education_required": extras.get("education_required", ""),
                "visa_sponsorship": extras.get("visa_sponsorship"),
                "work_authorization": extras.get("work_authorization", ""),
                "clearance_required": extras.get("clearance_required", False),
                "salary_equity": extras.get("salary_equity", False),
                "signing_bonus": extras.get("signing_bonus", False),
                "relocation_assistance": extras.get("relocation_assistance", False),
                "travel_required": extras.get("travel_required", ""),
                "certifications": extras.get("certifications", []),
                "benefits_list": extras.get("benefits_list", []),
                "languages_required": extras.get("languages_required", []),
                "word_count": extras.get("word_count", 0),
                "quality_score": extras.get("quality_score"),
            }
            enriched.append(job)
        except Exception as exc:
            logger.warning("harvest_agent: enrichment error for %s: %s", original_url, exc)
            continue

    return enriched


# ── Management command ────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Local Harvesting Agent — run harvesters on this machine and write to prod.\n"
        "Two modes: 'direct' (prod DB connection) or 'push' (HTTP API push)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=["direct", "push"],
            default="direct",
            help=(
                "direct: connect local clone straight to prod PostgreSQL (recommended). "
                "push: harvest offline and push via prod HTTP API."
            ),
        )
        parser.add_argument(
            "--platform",
            type=str,
            default="",
            help="Filter to a specific platform slug (e.g. workday, greenhouse). Default: all.",
        )
        parser.add_argument(
            "--max-companies",
            type=int,
            default=500,
            help="Max number of company labels to process. Default: 500.",
        )
        parser.add_argument(
            "--since-hours",
            type=int,
            default=25,
            help="Quick Sync window: fetch jobs updated in the last N hours. Default: 25.",
        )
        parser.add_argument(
            "--fetch-all",
            action="store_true",
            help="Full Crawl: ignore since-hours and paginate every job. Slower but complete.",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=4,
            help=(
                "Parallel harvest threads (push mode only). "
                "Use 8-16 for API platforms, keep at 2-4 for HTML scrapers. Default: 4."
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Jobs per push API request (push mode only). Max 1000. Default: 500.",
        )
        # Push mode auth
        parser.add_argument(
            "--push-url",
            type=str,
            default="",
            help="Production server base URL, e.g. https://chennu.co (push mode required).",
        )
        parser.add_argument(
            "--push-token",
            type=str,
            default="",
            help="HARVEST_PUSH_SECRET token (push mode required; or set PUSH_TOKEN env var).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Harvest normally but don't write to DB or push to prod (for testing).",
        )

    # ── Mode: direct ──────────────────────────────────────────────────────────

    def _run_direct(self, options):
        """
        Direct mode: local machine connects to prod DB and runs harvest_jobs_task
        synchronously. All enrichment, RawJob creation, and pipeline triggering
        happens exactly as on the server — just on your faster local hardware.
        """
        from harvest.tasks import harvest_jobs_task

        platform = options["platform"] or None
        scope = "full crawl" if options["fetch_all"] else f"since {options['since_hours']}h"
        target = "all platforms" if not platform else platform
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"[direct] Harvesting {target} | companies={options['max_companies']} | {scope}"
        ))
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("  [dry-run] would run harvest_jobs_task — skipped"))
            return

        result = harvest_jobs_task.apply(kwargs={
            "platform_slug": platform,
            "since_hours": options["since_hours"],
            "max_companies": options["max_companies"],
            "triggered_by": "LOCAL_AGENT_DIRECT",
        })
        self.stdout.write(self.style.SUCCESS(f"[direct] Done: {result.result}"))

    # ── Mode: push ────────────────────────────────────────────────────────────

    def _run_push(self, options):
        """
        Push mode: pulls company list from prod API, harvests locally (parallel
        threads, no DB needed), enriches in-memory, pushes enriched RawJobs to prod.
        """
        push_url = (options["push_url"] or os.environ.get("PUSH_URL", "")).rstrip("/")
        push_token = options["push_token"] or os.environ.get("PUSH_TOKEN", "")

        if not push_url:
            raise CommandError(
                "Push mode requires --push-url (or PUSH_URL env var). "
                "Example: --push-url https://chennu.co"
            )
        if not push_token:
            raise CommandError(
                "Push mode requires --push-token (or PUSH_TOKEN env var). "
                "Value must match HARVEST_PUSH_SECRET on prod."
            )

        batch_size = min(max(1, options["batch_size"]), 1000)
        workers = max(1, options["workers"])
        platform = options["platform"]

        scope = "full crawl" if options["fetch_all"] else f"since {options['since_hours']}h"
        target = "all platforms" if not platform else platform
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"[push] Harvesting {target} | companies={options['max_companies']} "
            f"workers={workers} batch={batch_size} | {scope}"
        ))

        # 1. Pull label list from prod
        self.stdout.write("  Fetching company labels from prod...")
        labels = _fetch_labels(push_url, push_token, platform, options["max_companies"])
        self.stdout.write(f"  Got {len(labels)} labels")

        if not labels:
            self.stdout.write(self.style.WARNING("  No labels found — nothing to harvest."))
            return

        # 2. Harvest in parallel, push in batches
        total_harvested = 0
        total_created = 0
        total_skipped = 0
        total_errors = 0

        pending_batch: list[dict] = []

        def flush_batch():
            nonlocal total_created, total_skipped, total_errors, pending_batch
            if not pending_batch:
                return
            chunk = pending_batch[:]
            pending_batch.clear()
            result = _push_batch(chunk, push_url, push_token, options["dry_run"], self.stdout)
            total_created += result.get("created", 0)
            total_skipped += result.get("skipped", 0)
            total_errors += result.get("errors", 0)
            self.stdout.write(
                f"    pushed {len(chunk)} → "
                f"created={result.get('created', 0)} "
                f"skipped={result.get('skipped', 0)} "
                f"errors={result.get('errors', 0)}"
            )

        t0 = time.monotonic()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _harvest_one_company,
                    label,
                    options["since_hours"],
                    options["fetch_all"],
                ): label
                for label in labels
            }

            done_count = 0
            for future in as_completed(futures):
                label = futures[future]
                done_count += 1
                try:
                    jobs = future.result()
                except Exception as exc:
                    self.stderr.write(
                        f"  [{done_count}/{len(labels)}] ERROR {label.get('company_name')}: {exc}"
                    )
                    continue

                if jobs:
                    total_harvested += len(jobs)
                    pending_batch.extend(jobs)
                    self.stdout.write(
                        f"  [{done_count}/{len(labels)}] "
                        f"{label.get('company_name')} ({label.get('platform_slug')}) "
                        f"→ {len(jobs)} jobs"
                    )
                else:
                    self.stdout.write(
                        f"  [{done_count}/{len(labels)}] "
                        f"{label.get('company_name')} ({label.get('platform_slug')}) "
                        f"→ 0 jobs"
                    )

                # Push when batch is full
                if len(pending_batch) >= batch_size:
                    flush_batch()

        # Push remainder
        flush_batch()

        elapsed = time.monotonic() - t0
        self.stdout.write(self.style.SUCCESS(
            f"\n[push] Finished in {elapsed:.0f}s — "
            f"harvested={total_harvested} created={total_created} "
            f"skipped={total_skipped} errors={total_errors}"
        ))

    # ── Entrypoint ────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        mode = options["mode"]
        if mode == "direct":
            self._run_direct(options)
        else:
            self._run_push(options)

import logging
import time
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

# ─── Harvest compliance constants ────────────────────────────────────────────
# Delay between processing each company within a platform run.
# Applies on top of the per-request delay inside each harvester.
INTER_COMPANY_DELAY_API = 1.5        # seconds — API platforms (GH, Lever, Ashby, Workday)
INTER_COMPANY_DELAY_SCRAPE = 5.0     # seconds — HTML scrape platforms
HTML_SCRAPE_PLATFORMS = {"html_scrape", "icims", "taleo", "jobvite", "ultipro",
                         "applicantpro", "applytojob", "theapplicantmanager",
                         "zoho", "recruitee"}

# Circuit breaker — skip a company after this many consecutive fetch failures
MAX_CONSECUTIVE_FAILURES = 3

logger = logging.getLogger(__name__)


@shared_task(name="harvest.backfill_platform_labels_from_jobs")
def backfill_platform_labels_from_jobs_task():
    """
    Scan all job original_link URLs, detect ATS platform from URL patterns,
    and create/update CompanyPlatformLabel records — no HTTP requests needed.

    Runs after every bulk job import so new companies get labeled immediately.
    """
    from jobs.models import Job
    from companies.models import Company
    from .models import JobBoardPlatform, CompanyPlatformLabel
    from .detectors import URL_PATTERNS, TENANT_EXTRACTORS
    from .detectors import extract_tenant

    platforms = {p.slug: p for p in JobBoardPlatform.objects.filter(is_enabled=True)}
    company_best: dict = {}

    for job in Job.objects.exclude(original_link="").select_related("company_obj").iterator():
        if not job.company_obj_id or job.company_obj_id in company_best:
            continue
        raw_url = job.original_link
        url = raw_url.lower()
        for slug, patterns in URL_PATTERNS.items():
            for pattern in patterns:
                if pattern in url:
                    from .detectors import extract_tenant as _et
                    company_best[job.company_obj_id] = {
                        "slug": slug,
                        "tenant_id": _et(slug, raw_url),
                        "sample_url": raw_url,
                    }
                    break
            if job.company_obj_id in company_best:
                break

    created = updated = 0
    now = timezone.now()

    for company_id, info in company_best.items():
        platform = platforms.get(info["slug"])
        if not platform:
            continue
        try:
            company = Company.objects.get(pk=company_id)
        except Company.DoesNotExist:
            continue

        _, was_created = CompanyPlatformLabel.objects.update_or_create(
            company=company,
            defaults={
                "platform": platform,
                "confidence": "HIGH",
                "detection_method": "URL_PATTERN",
                "tenant_id": info["tenant_id"],
                "detected_at": now,
                "last_checked_at": now,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    logger.info(f"backfill_platform_labels_from_jobs: {created} created, {updated} updated")
    return {"created": created, "updated": updated}


@shared_task(bind=True, max_retries=2, name="harvest.detect_company_platforms")
def detect_company_platforms_task(self, batch_size: int = 200, force_recheck: bool = False):
    """
    Run 3-step platform detection for companies without labels (or stale ones).
    Step 1: URL Pattern → Step 2: HTTP HEAD → Step 3: HTML Parse
    """
    from companies.models import Company
    from .models import JobBoardPlatform, CompanyPlatformLabel
    from .detectors import run_detection_pipeline, extract_tenant

    stale_threshold = timezone.now() - timedelta(days=7)

    if force_recheck:
        company_ids = list(Company.objects.values_list("id", flat=True)[:batch_size])
    else:
        stale_ids = list(
            CompanyPlatformLabel.objects.filter(
                last_checked_at__lt=stale_threshold,
                detection_method__in=["UNDETECTED", "HTML_PARSE"],
            ).values_list("company_id", flat=True)
        )
        unlabeled_ids = list(
            Company.objects.exclude(platform_label__isnull=False).values_list("id", flat=True)
        )
        company_ids = list(set(stale_ids + unlabeled_ids))[:batch_size]

    companies = Company.objects.filter(id__in=company_ids)
    if not companies.exists():
        logger.info("No companies need platform detection.")
        return {"detected": 0, "total": 0}

    detected = 0
    for company in companies:
        try:
            slug, confidence, method = run_detection_pipeline(company)

            platform = None
            tenant_id = ""
            if slug:
                platform = JobBoardPlatform.objects.filter(slug=slug, is_enabled=True).first()
                url = company.career_site_url or company.website or ""
                tenant_id = extract_tenant(slug, url)

            CompanyPlatformLabel.objects.update_or_create(
                company=company,
                defaults={
                    "platform": platform,
                    "confidence": confidence,
                    "detection_method": method,
                    "detected_at": timezone.now() if slug else None,
                    "last_checked_at": timezone.now(),
                    "tenant_id": tenant_id,
                },
            )
            if slug:
                detected += 1
        except Exception as e:
            logger.error(f"Detection failed for company {company.id}: {e}")

        # Respectful delay — detection pipeline may issue HTTP requests
        time.sleep(2.0)

    logger.info(f"Detection done: {detected}/{len(company_ids)} detected.")
    return {"detected": detected, "total": len(company_ids)}


@shared_task(bind=True, max_retries=2, name="harvest.harvest_jobs")
def harvest_jobs_task(
    self,
    platform_slug: str | None = None,
    since_hours: int = 24,
    max_companies: int = 50,
):
    """Harvest jobs from all enabled platforms or a specific one."""
    from .models import JobBoardPlatform, CompanyPlatformLabel, HarvestRun, HarvestedJob
    from .harvesters import get_harvester
    from .normalizer import normalize_job_data

    qs = JobBoardPlatform.objects.filter(is_enabled=True)
    if platform_slug:
        qs = qs.filter(slug=platform_slug)

    for platform in qs:
        labels = CompanyPlatformLabel.objects.filter(
            platform=platform,
            detection_method__in=["URL_PATTERN", "HTTP_HEAD", "HTML_PARSE", "MANUAL"],
        ).select_related("company")[:max_companies]

        if not labels.exists():
            continue

        run = HarvestRun.objects.create(
            platform=platform,
            triggered_by="SCHEDULED",
            celery_task_id=self.request.id or "",
            companies_targeted=labels.count(),
        )

        harvester = get_harvester(platform.slug)
        is_scraper = platform.slug in HTML_SCRAPE_PLATFORMS
        inter_delay = INTER_COMPANY_DELAY_SCRAPE if is_scraper else INTER_COMPANY_DELAY_API

        jobs_new = jobs_dup = jobs_fail = 0
        errors: list[str] = []
        consecutive_failures = 0

        for label in labels:
            # Circuit breaker — stop hammering after repeated failures
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning(
                    "[HARVEST] Circuit breaker: %d consecutive failures on %s — stopping run",
                    consecutive_failures, platform.name,
                )
                errors.append(
                    f"Circuit breaker tripped after {consecutive_failures} consecutive failures"
                )
                break

            company = label.company
            tenant_id = label.tenant_id or ""
            try:
                raw_jobs = harvester.fetch_jobs(company, tenant_id, since_hours=since_hours)

                if not raw_jobs:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0   # reset on success

                for raw in raw_jobs:
                    try:
                        normalized = normalize_job_data(raw, platform, company, run)
                        original_url = normalized.get("original_url", "")
                        url_hash = normalized.get("url_hash", "")
                        if not original_url or not url_hash:
                            continue

                        existing = HarvestedJob.objects.filter(
                            platform=platform, url_hash=url_hash
                        ).first()

                        if existing:
                            existing.fetched_at = timezone.now()
                            existing.expires_at = timezone.now() + timedelta(hours=24)
                            existing.is_active = True
                            existing.save(update_fields=["fetched_at", "expires_at", "is_active"])
                            jobs_dup += 1
                        else:
                            HarvestedJob.objects.create(**normalized)
                            jobs_new += 1
                    except Exception as e:
                        jobs_fail += 1
                        errors.append(str(e)[:200])

            except Exception as e:
                jobs_fail += 1
                consecutive_failures += 1
                errors.append(f"Company {company.id} ({company.name}): {str(e)[:150]}")

            # Respectful inter-company delay regardless of success/failure
            time.sleep(inter_delay)

        run.finished_at = timezone.now()
        run.status = "SUCCESS" if not errors else ("PARTIAL" if jobs_new > 0 else "FAILED")
        run.jobs_fetched = jobs_new + jobs_dup + jobs_fail
        run.jobs_new = jobs_new
        run.jobs_duplicate = jobs_dup
        run.jobs_failed = jobs_fail
        run.error_log = "\n".join(errors[:50])
        run.save()

        platform.last_harvested_at = timezone.now()
        platform.save(update_fields=["last_harvested_at"])

        logger.info(f"Harvest {platform.name}: +{jobs_new} new, {jobs_dup} dup, {jobs_fail} fail")

    return {"status": "complete"}


@shared_task(name="harvest.cleanup_harvested_jobs")
def cleanup_harvested_jobs_task():
    """Delete expired HarvestedJobs and old HarvestRun records."""
    from .models import HarvestedJob, HarvestRun

    now = timezone.now()

    expired, _ = HarvestedJob.objects.filter(
        expires_at__lt=now,
        sync_status__in=["PENDING", "SKIPPED"],
    ).delete()

    old_cutoff = now - timedelta(days=30)
    old_runs, _ = HarvestRun.objects.filter(
        started_at__lt=old_cutoff,
        status__in=["SUCCESS", "FAILED", "PARTIAL"],
    ).delete()

    logger.info(f"Cleanup: {expired} expired jobs, {old_runs} old runs deleted.")
    return {"expired_jobs": expired, "old_runs": old_runs}


@shared_task(name="harvest.sync_harvested_to_pool")
def sync_harvested_to_pool_task(max_jobs: int = 100):
    """Promote pending HarvestedJobs to internal Job model (status=POOL)."""
    from .models import HarvestedJob
    from jobs.models import Job
    from django.contrib.auth import get_user_model

    User = get_user_model()
    system_user = User.objects.filter(is_superuser=True).first()
    if not system_user:
        logger.error("No superuser found for sync task.")
        return {"synced": 0}

    pending = HarvestedJob.objects.filter(
        sync_status="PENDING",
        is_active=True,
        company__isnull=False,
    ).exclude(original_url="").select_related("company", "platform")[:max_jobs]

    synced = skipped = failed = 0

    for hj in pending:
        existing = Job.objects.filter(original_link=hj.original_url).first()
        if existing:
            hj.synced_to_job = existing
            hj.sync_status = "SKIPPED"
            hj.save(update_fields=["synced_to_job", "sync_status"])
            skipped += 1
            continue

        try:
            with transaction.atomic():
                job = Job.objects.create(
                    title=hj.title,
                    company=hj.company_name,
                    company_obj=hj.company,
                    location=hj.location or "",
                    description=hj.description_text or hj.title,
                    original_link=hj.original_url,
                    salary_range=hj.salary_raw or "",
                    job_type=hj.job_type if hj.job_type != "UNKNOWN" else "FULL_TIME",
                    status="POOL",
                    job_source=f"HARVESTED_{hj.platform.slug.upper()}",
                    posted_by=system_user,
                )
                hj.synced_to_job = job
                hj.sync_status = "SYNCED"
                hj.save(update_fields=["synced_to_job", "sync_status"])
                synced += 1
        except Exception as e:
            hj.sync_status = "FAILED"
            hj.save(update_fields=["sync_status"])
            logger.error(f"Sync failed for HarvestedJob {hj.id}: {e}")
            failed += 1

    logger.info(f"Sync: {synced} synced, {skipped} skipped, {failed} failed.")
    return {"synced": synced, "skipped": skipped, "failed": failed}

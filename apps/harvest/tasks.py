import logging
import time
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from core.task_progress import update_task_progress

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


@shared_task(bind=True, name="harvest.backfill_platform_labels_from_jobs")
def backfill_platform_labels_from_jobs_task(self):
    """
    Scan all job original_link URLs, detect ATS platform from URL patterns,
    and create/update CompanyPlatformLabel records — no HTTP requests needed.

    Runs after every bulk job import so new companies get labeled immediately.
    """
    from jobs.models import Job
    from companies.models import Company
    from .models import JobBoardPlatform, CompanyPlatformLabel
    from .detectors import URL_PATTERNS, extract_tenant

    update_task_progress(self, current=0, total=0, message="Loading job URLs…")

    platforms = {p.slug: p for p in JobBoardPlatform.objects.filter(is_enabled=True)}
    company_best: dict = {}

    all_jobs = list(
        Job.objects.exclude(original_link="")
        .filter(company_obj__isnull=False)
        .values("company_obj_id", "original_link")
    )
    total_jobs = len(all_jobs)

    update_task_progress(self, current=0, total=total_jobs, message=f"Scanning {total_jobs} job URLs…")

    for idx, job in enumerate(all_jobs, start=1):
        cid = job["company_obj_id"]
        if cid in company_best:
            continue
        raw_url = job["original_link"]
        url = raw_url.lower()
        for slug, patterns in URL_PATTERNS.items():
            for pattern in patterns:
                if pattern in url:
                    company_best[cid] = {
                        "slug": slug,
                        "tenant_id": extract_tenant(slug, raw_url),
                    }
                    break
            if cid in company_best:
                break

        if idx % 200 == 0:
            update_task_progress(
                self,
                current=idx,
                total=total_jobs,
                message=f"Scanned {idx}/{total_jobs} URLs · {len(company_best)} platforms found…",
            )

    matches = len(company_best)
    update_task_progress(self, current=total_jobs, total=total_jobs,
                         message=f"URL scan done — labeling {matches} companies…")

    created = updated = 0
    now = timezone.now()
    items = list(company_best.items())

    for i, (company_id, info) in enumerate(items, start=1):
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

        if i % 50 == 0:
            update_task_progress(
                self,
                current=i,
                total=matches,
                message=f"Labeled {i}/{matches} companies ({created} new, {updated} updated)…",
            )

    logger.info(f"backfill_platform_labels_from_jobs: {created} created, {updated} updated")
    return {"created": created, "updated": updated}


@shared_task(bind=True, max_retries=2, name="harvest.detect_company_platforms")
def detect_company_platforms_task(
    self,
    batch_size: int = 200,
    force_recheck: bool = False,
    triggered_user_id: int | None = None,
):
    """
    Run 3-step platform detection for companies without labels (or stale ones).
    Step 1: URL Pattern → Step 2: HTTP HEAD → Step 3: HTML Parse

    Persists a HarvestRun (run_type=DETECTION) so Run Monitor shows progress and results.
    """
    from django.contrib.auth import get_user_model

    from companies.models import Company
    from .models import HarvestRun, JobBoardPlatform, CompanyPlatformLabel
    from .detectors import run_detection_pipeline, extract_tenant

    User = get_user_model()
    triggered_user = None
    if triggered_user_id:
        triggered_user = User.objects.filter(pk=triggered_user_id).first()

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

    run = HarvestRun.objects.create(
        run_type=HarvestRun.RunType.DETECTION,
        platform=None,
        triggered_by=HarvestRun.TriggerType.MANUAL,
        triggered_user=triggered_user,
        celery_task_id=self.request.id or "",
        companies_targeted=len(company_ids),
        detection_total=0,
        detection_detected=0,
        status=HarvestRun.Status.RUNNING,
    )

    if not company_ids:
        now = timezone.now()
        run.finished_at = now
        run.status = HarvestRun.Status.SUCCESS
        run.save(update_fields=["finished_at", "status"])
        logger.info("No companies need platform detection.")
        return {"detected": 0, "total": 0, "harvest_run_id": run.pk}

    companies = Company.objects.filter(id__in=company_ids).order_by("id")
    company_list = list(companies)
    total_n = len(company_list)
    detected = 0
    errors: list[str] = []

    update_task_progress(
        self,
        current=0,
        total=total_n,
        message="Starting platform detection…",
    )

    try:
        for idx, company in enumerate(company_list, start=1):
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
                msg = f"Company {company.id}: {e}"
                logger.error("Detection failed: %s", msg)
                errors.append(msg[:300])

            time.sleep(2.0)
            update_task_progress(
                self,
                current=idx,
                total=total_n,
                message=f"{idx}/{total_n} · {(company.name or str(company.pk))[:60]}",
            )

        now = timezone.now()
        run.finished_at = now
        run.detection_detected = detected
        run.detection_total = len(company_ids)
        run.companies_targeted = len(company_ids)
        if errors:
            run.status = HarvestRun.Status.PARTIAL if detected else HarvestRun.Status.FAILED
            run.error_log = "\n".join(errors[:50])
        else:
            run.status = HarvestRun.Status.SUCCESS
        run.save(
            update_fields=[
                "finished_at",
                "status",
                "detection_detected",
                "detection_total",
                "companies_targeted",
                "error_log",
            ]
        )
        logger.info("Detection done: %s/%s detected.", detected, len(company_ids))
        return {"detected": detected, "total": len(company_ids), "harvest_run_id": run.pk}

    except Exception as e:
        logger.exception("detect_company_platforms_task failed: %s", e)
        run.finished_at = timezone.now()
        run.status = HarvestRun.Status.FAILED
        run.detection_detected = detected
        run.detection_total = len(company_ids)
        run.error_log = (str(e)[:1500] + ("\n" + "\n".join(errors[:20]) if errors else ""))[:4000]
        run.save(
            update_fields=[
                "finished_at",
                "status",
                "detection_detected",
                "detection_total",
                "error_log",
            ]
        )
        raise


@shared_task(bind=True, max_retries=2, name="harvest.harvest_jobs")
def harvest_jobs_task(
    self,
    platform_slug: str | None = None,
    since_hours: int = 24,
    max_companies: int = 50,
    triggered_by: str = "SCHEDULED",
    triggered_user_id: int | None = None,
):
    """Harvest jobs from all enabled platforms or a specific one."""
    from django.contrib.auth import get_user_model

    from .models import JobBoardPlatform, CompanyPlatformLabel, HarvestRun, HarvestedJob
    from .harvesters import get_harvester
    from .normalizer import normalize_job_data

    tb = triggered_by if triggered_by in ("SCHEDULED", "MANUAL") else "SCHEDULED"
    triggered_user = None
    if triggered_user_id:
        User = get_user_model()
        triggered_user = User.objects.filter(pk=triggered_user_id).first()

    qs = JobBoardPlatform.objects.filter(is_enabled=True)
    if platform_slug:
        qs = qs.filter(slug=platform_slug)

    for platform in qs:
        labels_qs = CompanyPlatformLabel.objects.filter(
            platform=platform,
            detection_method__in=["URL_PATTERN", "HTTP_HEAD", "HTML_PARSE", "MANUAL"],
        ).select_related("company")[:max_companies]

        labels_list = list(labels_qs)
        if not labels_list:
            continue

        run = HarvestRun.objects.create(
            run_type=HarvestRun.RunType.HARVEST,
            platform=platform,
            triggered_by=tb,
            triggered_user=triggered_user,
            celery_task_id=self.request.id or "",
            companies_targeted=len(labels_list),
        )

        harvester = get_harvester(platform.slug)
        is_scraper = platform.slug in HTML_SCRAPE_PLATFORMS
        inter_delay = INTER_COMPANY_DELAY_SCRAPE if is_scraper else INTER_COMPANY_DELAY_API

        jobs_new = jobs_dup = jobs_fail = 0
        errors: list[str] = []
        consecutive_failures = 0

        total_l = len(labels_list)
        update_task_progress(
            self,
            current=0,
            total=total_l,
            message=f"Harvest {platform.name}: starting…",
        )

        for i, label in enumerate(labels_list, start=1):
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
            update_task_progress(
                self,
                current=i,
                total=total_l,
                message=f"{platform.name}: {i}/{total_l}",
            )

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


@shared_task(bind=True, name="harvest.check_portal_health")
def check_portal_health_task(self, label_pk: int):
    """
    HTTP-check a single career portal URL and update portal_alive + portal_last_verified.
    Called individually per label — queue many at once via verify_all_portals_task.
    """
    import requests
    from .models import CompanyPlatformLabel

    try:
        label = CompanyPlatformLabel.objects.select_related("platform").get(pk=label_pk)
    except CompanyPlatformLabel.DoesNotExist:
        return

    from .career_url import build_career_url
    url = build_career_url(
        label.platform.slug if label.platform else "",
        label.tenant_id or "",
    )
    if not url:
        return

    alive = False
    try:
        resp = requests.head(
            url,
            timeout=12,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; GoCareers-PortalBot/1.0; "
                    "+https://chennu.co)"
                )
            },
        )
        # Treat 2xx and 3xx (after redirect) as alive; 4xx/5xx as down
        if resp.status_code >= 400:
            # Some ATS block HEAD — retry with GET (just first bytes)
            resp = requests.get(
                url,
                timeout=15,
                stream=True,
                allow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; GoCareers-PortalBot/1.0)"
                    )
                },
            )
            resp.close()
        alive = resp.status_code < 400
    except Exception:
        alive = False

    label.portal_alive = alive
    label.portal_last_verified = timezone.now()
    label.save(update_fields=["portal_alive", "portal_last_verified"])


@shared_task(bind=True, name="harvest.verify_all_portals")
def verify_all_portals_task(self):
    """
    Queue HTTP health checks for all CompanyPlatformLabels that have a career URL.
    Each check runs asynchronously via check_portal_health_task.
    """
    from .models import CompanyPlatformLabel

    update_task_progress(self, current=0, total=0, message="Queuing portal health checks…")

    label_pks = list(
        CompanyPlatformLabel.objects.filter(
            platform__isnull=False,
        ).exclude(tenant_id="").exclude(tenant_id__isnull=True)
        .values_list("pk", flat=True)
    )

    total = len(label_pks)
    update_task_progress(self, current=0, total=total, message=f"Queuing {total} checks…")

    for i, pk in enumerate(label_pks, start=1):
        check_portal_health_task.apply_async(
            args=[pk],
            countdown=i * 0.3,   # stagger by 0.3s each to avoid hammering
        )
        if i % 50 == 0:
            update_task_progress(
                self, current=i, total=total,
                message=f"Queued {i}/{total} checks…",
            )

    update_task_progress(self, current=total, total=total,
                         message=f"✅ All {total} portal checks queued!")
    return {"queued": total}


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


@shared_task(bind=True, name="harvest.sync_harvested_to_pool")
def sync_harvested_to_pool_task(self, max_jobs: int = 100):
    """Promote pending HarvestedJobs to internal Job model (status=POOL)."""
    from .models import HarvestedJob
    from jobs.models import Job
    from django.contrib.auth import get_user_model

    User = get_user_model()
    system_user = User.objects.filter(is_superuser=True).first()
    if not system_user:
        logger.error("No superuser found for sync task.")
        return {"synced": 0}

    pending = list(
        HarvestedJob.objects.filter(
            sync_status="PENDING",
            is_active=True,
            company__isnull=False,
        )
        .exclude(original_url="")
        .select_related("company", "platform")[:max_jobs]
    )

    synced = skipped = failed = 0
    total_n = len(pending)
    if total_n:
        update_task_progress(self, current=0, total=total_n, message="Sync to job pool…")

    for idx, hj in enumerate(pending, start=1):
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

        if total_n:
            update_task_progress(
                self,
                current=idx,
                total=total_n,
                message=f"Sync {idx}/{total_n}",
            )

    logger.info(f"Sync: {synced} synced, {skipped} skipped, {failed} failed.")
    return {"synced": synced, "skipped": skipped, "failed": failed}

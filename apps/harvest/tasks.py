import logging
import time
from datetime import timedelta

from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from core.task_progress import update_task_progress

# ─── Harvest compliance constants ────────────────────────────────────────────
# Delay between processing each company within a platform run.
# Applies on top of the per-request delay inside each harvester.
INTER_COMPANY_DELAY_API = 1.5        # seconds — API platforms (GH, Lever, Ashby, Workday)
INTER_COMPANY_DELAY_SCRAPE = 5.0     # seconds — HTML scrape platforms
HTML_SCRAPE_PLATFORMS = {"html_scrape", "icims", "taleo", "jobvite", "ultipro",
                         "applicantpro", "applytojob", "theapplicantmanager",
                         "zoho", "recruitee", "breezy", "teamtailor"}

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


@shared_task(bind=True, name="harvest.fetch_raw_jobs_for_company", max_retries=2, default_retry_delay=60)
def fetch_raw_jobs_for_company_task(
    self,
    label_pk: int,
    batch_id: int = None,
    triggered_by: str = "MANUAL",
    max_jobs: int | None = None,
    since_hours: int | None = None,
    fetch_all: bool = False,
):
    """
    Fetch ALL jobs for a single CompanyPlatformLabel and upsert into RawJob.
    Creates a CompanyFetchRun audit record. Updates FetchBatch counters if batch_id given.
    """
    import hashlib
    import requests
    from datetime import date

    from .models import CompanyPlatformLabel, CompanyFetchRun, FetchBatch, RawJob
    from .harvesters import get_harvester

    # ── Load label ────────────────────────────────────────────────────────────
    try:
        label = CompanyPlatformLabel.objects.select_related("platform", "company").get(pk=label_pk)
    except CompanyPlatformLabel.DoesNotExist:
        logger.warning("fetch_raw_jobs_for_company_task: label %s not found", label_pk)
        return

    batch = None
    if batch_id:
        batch = FetchBatch.objects.filter(pk=batch_id).first()

    # ── Create run record ─────────────────────────────────────────────────────
    run = CompanyFetchRun.objects.create(
        label=label,
        batch=batch,
        status=CompanyFetchRun.Status.RUNNING,
        task_id=self.request.id or "",
        started_at=timezone.now(),
        triggered_by=triggered_by,
    )

    # ── Guard: no tenant or no platform ──────────────────────────────────────
    if not label.platform or not label.tenant_id:
        run.status = CompanyFetchRun.Status.SKIPPED
        run.error_type = CompanyFetchRun.ErrorType.NO_TENANT
        run.error_message = "No platform or tenant_id configured."
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return

    harvester = get_harvester(label.platform.slug)
    if harvester is None:
        run.status = CompanyFetchRun.Status.SKIPPED
        run.error_type = CompanyFetchRun.ErrorType.PLATFORM_ERROR
        run.error_message = f"No harvester for platform slug: {label.platform.slug}"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return

    # ── Fetch ─────────────────────────────────────────────────────────────────
    # Scraper platforms (HTML-based) can't filter by date — always fetch all.
    # API platforms support since_hours for incremental fetches (default: 25h window).
    # test_mode passes max_jobs — skip full pagination to stay fast + respectful.
    # adp uses HTMLScrapeHarvester (not a JSON API) — include in scraper set
    # for proper rate-limiting and fetch_all behavior
    SCRAPER_SLUGS = {"jobvite", "icims", "taleo", "applicantpro", "applytojob",
                     "theapplicantmanager", "zoho", "breezy", "teamtailor", "adp"}
    platform_slug_val = label.platform.slug if label.platform else ""
    is_scraper_platform = platform_slug_val in SCRAPER_SLUGS

    # fetch_all logic:
    #   fetch_all=True  → always paginate through ALL pages, ignore since_hours filter
    #                     (used by FETCH ALL button for initial/full crawl)
    #   fetch_all=False → use since_hours window (fast daily incremental)
    #   scrapers        → always fetch_all (no date filter exists in HTML)
    #   test mode       → fetch_all so we get real data, but capped by max_jobs
    use_fetch_all = fetch_all or is_scraper_platform or (max_jobs is not None)
    effective_since_hours = since_hours if since_hours is not None else 25

    try:
        if is_scraper_platform:
            # HTML scrapers have no date filter — always fetch everything
            raw_jobs = harvester.fetch_jobs(
                label.company,
                label.tenant_id,
                fetch_all=True,
            )
        elif use_fetch_all:
            # Full crawl: get ALL jobs from this company, all pages, ignore time filter
            raw_jobs = harvester.fetch_jobs(
                label.company,
                label.tenant_id,
                fetch_all=True,
            )
        else:
            # Incremental: only jobs updated in the last N hours (fast daily run)
            raw_jobs = harvester.fetch_jobs(
                label.company,
                label.tenant_id,
                since_hours=effective_since_hours,
                fetch_all=False,
            )
        # Capture API-reported total (even when we only fetched a subset)
        run.jobs_total_available = getattr(harvester, "last_total_available", 0) or len(raw_jobs)
    except requests.exceptions.Timeout as exc:
        run.status = CompanyFetchRun.Status.FAILED
        run.error_type = CompanyFetchRun.ErrorType.TIMEOUT
        run.error_message = str(exc)[:500]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return
    except requests.exceptions.HTTPError as exc:
        run.status = CompanyFetchRun.Status.FAILED
        run.error_type = CompanyFetchRun.ErrorType.HTTP_ERROR
        run.error_message = str(exc)[:500]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        return
    except Exception as exc:
        run.status = CompanyFetchRun.Status.FAILED
        run.error_type = CompanyFetchRun.ErrorType.PARSE_ERROR
        run.error_message = str(exc)[:500]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_type", "error_message", "completed_at"])
        if batch:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1
            )
        logger.exception("fetch_raw_jobs_for_company_task failed for label %s: %s", label_pk, exc)
        return

    # ── Upsert jobs ───────────────────────────────────────────────────────────
    jobs_new = jobs_updated = jobs_duplicate = jobs_failed = 0
    upsert_errors: list[str] = []

    # In test mode, cap to max_jobs so we don't write hundreds of rows
    if max_jobs and len(raw_jobs) > max_jobs:
        raw_jobs = raw_jobs[:max_jobs]

    for job_dict in raw_jobs:
        try:
            original_url = (job_dict.get("original_url") or "").strip()
            if not original_url:
                jobs_failed += 1
                continue

            url_hash = hashlib.sha256(original_url.encode()).hexdigest()

            # Parse posted_date
            posted_date = None
            posted_raw = job_dict.get("posted_date_raw", "")
            if posted_raw:
                try:
                    # Handle ISO format: 2024-01-15T00:00:00Z or 2024-01-15
                    posted_date = date.fromisoformat(
                        posted_raw[:10].replace("Z", "")
                    )
                except Exception:
                    pass

            # Parse closing_date
            closing_date = None
            closing_raw = job_dict.get("closing_date", "")
            if closing_raw:
                try:
                    closing_date = date.fromisoformat(closing_raw[:10])
                except Exception:
                    pass

            defaults = {
                "company": label.company,
                "platform_label": label,
                "job_platform": label.platform,
                "external_id": (job_dict.get("external_id") or "")[:512],
                "original_url": original_url[:1024],
                "apply_url": (job_dict.get("apply_url") or "")[:1024],
                "title": (job_dict.get("title") or "")[:512],
                "company_name": (job_dict.get("company_name") or label.company.name)[:256],
                "department": (job_dict.get("department") or "")[:256],
                "team": (job_dict.get("team") or "")[:256],
                "location_raw": (job_dict.get("location_raw") or "")[:512],
                "city": (job_dict.get("city") or "")[:128],
                "state": (job_dict.get("state") or "")[:128],
                "country": (job_dict.get("country") or "")[:128],
                "location_type": job_dict.get("location_type", "UNKNOWN"),
                "is_remote": bool(job_dict.get("is_remote", False)),
                "employment_type": job_dict.get("employment_type", "UNKNOWN"),
                "experience_level": job_dict.get("experience_level", "UNKNOWN"),
                "salary_min": job_dict.get("salary_min"),
                "salary_max": job_dict.get("salary_max"),
                "salary_currency": (job_dict.get("salary_currency") or "USD")[:8],
                "salary_period": (job_dict.get("salary_period") or "")[:16],
                "salary_raw": (job_dict.get("salary_raw") or "")[:256],
                "description": job_dict.get("description") or "",
                "requirements": job_dict.get("requirements") or "",
                "benefits": job_dict.get("benefits") or "",
                "posted_date": posted_date,
                "closing_date": closing_date,
                "platform_slug": (label.platform.slug if label.platform else "")[:64],
                "raw_payload": job_dict.get("raw_payload") or {},
                "is_active": True,
            }

            obj, created = RawJob.objects.update_or_create(
                url_hash=url_hash,
                defaults=defaults,
            )
            if created:
                jobs_new += 1
            else:
                jobs_updated += 1

        except Exception as exc:
            jobs_failed += 1
            err_str = f"{type(exc).__name__}: {exc}"
            logger.error("RawJob upsert failed for label %s: %s", label_pk, err_str)
            if len(upsert_errors) < 5:
                upsert_errors.append(err_str[:300])

    # ── Update run record ─────────────────────────────────────────────────────
    run.status = (
        CompanyFetchRun.Status.SUCCESS
        if jobs_failed == 0
        else (CompanyFetchRun.Status.PARTIAL if (jobs_new + jobs_updated) > 0 else CompanyFetchRun.Status.FAILED)
    )
    run.jobs_found = len(raw_jobs)
    run.jobs_new = jobs_new
    run.jobs_updated = jobs_updated
    run.jobs_duplicate = jobs_duplicate
    run.jobs_failed = jobs_failed
    run.completed_at = timezone.now()
    if upsert_errors and not run.error_message:
        run.error_message = "Upsert errors: " + " | ".join(upsert_errors)
        run.error_type = CompanyFetchRun.ErrorType.PARSE_ERROR
    run.save(update_fields=[
        "status", "jobs_found", "jobs_total_available", "jobs_new", "jobs_updated",
        "jobs_duplicate", "jobs_failed", "completed_at", "error_message", "error_type",
    ])

    # ── Update batch counters + auto-complete ────────────────────────────────
    if batch:
        if run.status in (CompanyFetchRun.Status.SUCCESS, CompanyFetchRun.Status.PARTIAL):
            FetchBatch.objects.filter(pk=batch.pk).update(
                completed_companies=models.F("completed_companies") + 1,
                total_jobs_found=models.F("total_jobs_found") + len(raw_jobs),
                total_jobs_new=models.F("total_jobs_new") + jobs_new,
            )
        else:
            FetchBatch.objects.filter(pk=batch.pk).update(
                failed_companies=models.F("failed_companies") + 1,
            )

        # Auto-complete the batch when every child task has reported back
        refreshed = FetchBatch.objects.filter(pk=batch.pk).values(
            "total_companies", "completed_companies", "failed_companies"
        ).first()
        if refreshed:
            done = refreshed["completed_companies"] + refreshed["failed_companies"]
            total_co = refreshed["total_companies"]
            if total_co > 0 and done >= total_co:
                final_status = (
                    FetchBatch.Status.COMPLETED
                    if refreshed["failed_companies"] == 0
                    else FetchBatch.Status.PARTIAL
                )
                FetchBatch.objects.filter(
                    pk=batch.pk, status=FetchBatch.Status.RUNNING
                ).update(status=final_status, completed_at=timezone.now())

    logger.info(
        "fetch_raw_jobs: label=%s new=%d updated=%d failed=%d",
        label_pk, jobs_new, jobs_updated, jobs_failed,
    )
    return {
        "label_pk": label_pk,
        "jobs_found": len(raw_jobs),
        "jobs_new": jobs_new,
        "jobs_updated": jobs_updated,
        "jobs_failed": jobs_failed,
    }


@shared_task(bind=True, name="harvest.fetch_raw_jobs_batch", max_retries=0)
def fetch_raw_jobs_batch_task(
    self,
    platform_slug: str = None,
    label_pks: list = None,
    batch_name: str = None,
    triggered_user_id: int = None,
    test_mode: bool = False,
    test_max_jobs: int = 10,
    companies_per_platform: int = 1,
    skip_platforms: list = None,
    min_hours_since_fetch: int = 6,
    fetch_all: bool = False,
):
    """
    Create a FetchBatch and dispatch fetch_raw_jobs_for_company_task for every matching label.

    test_mode=True — picks up to `companies_per_platform` companies per platform,
    passes max_jobs=test_max_jobs (no full pagination). Useful for smoke-testing.
    skip_platforms — list of platform slugs to exclude (e.g. ["greenhouse","lever"]).
    min_hours_since_fetch — skip labels that were successfully fetched within this many
    hours (default 6). Prevents re-hammering the same API on repeated daily runs.
    Pass 0 to disable (force re-fetch everything).
    """
    from django.contrib.auth import get_user_model
    from .models import CompanyPlatformLabel, CompanyFetchRun, FetchBatch

    User = get_user_model()
    triggered_user = None
    if triggered_user_id:
        triggered_user = User.objects.filter(pk=triggered_user_id).first()

    # Build batch name
    if not batch_name:
        ts = timezone.now().strftime("%Y-%m-%d %H:%M")
        skipped = ", ".join(skip_platforms or [])
        if test_mode:
            skip_str = f" | skip: {skipped}" if skipped else ""
            batch_name = f"PLATFORM CHECK — {companies_per_platform} co/platform, {test_max_jobs} jobs{skip_str} — {ts}"
        elif platform_slug:
            batch_name = f"{platform_slug.title()} batch — {ts}"
        else:
            batch_name = f"Full batch — {ts}"

    batch = FetchBatch.objects.create(
        created_by=triggered_user,
        name=batch_name,
        status=FetchBatch.Status.RUNNING,
        platform_filter=platform_slug or "",
        task_id=self.request.id or "",
        started_at=timezone.now(),
    )

    # ── Build label queryset ──────────────────────────────────────────────────
    # Include portal_alive=True (confirmed up) AND portal_alive=None (never checked).
    # Exclude portal_alive=False (confirmed down — no point hammering dead portals).
    # Only include companies whose platform is enabled — respect is_enabled flag.
    qs = CompanyPlatformLabel.objects.filter(
        portal_alive__in=[True, None],
        platform__isnull=False,
        platform__is_enabled=True,
    ).exclude(tenant_id="").select_related("platform", "company").order_by("company__name")

    if platform_slug:
        qs = qs.filter(platform__slug=platform_slug)

    if label_pks:
        qs = qs.filter(pk__in=label_pks)

    if skip_platforms:
        qs = qs.exclude(platform__slug__in=skip_platforms)

    # ── Build skip-if-fresh set ───────────────────────────────────────────────
    # Labels with a successful/partial run completed within min_hours_since_fetch
    # are skipped — no point re-fetching the same jobs minutes/hours later.
    fresh_label_pks: set[int] = set()
    if min_hours_since_fetch > 0 and not test_mode:
        fresh_cutoff = timezone.now() - timedelta(hours=min_hours_since_fetch)
        fresh_label_pks = set(
            CompanyFetchRun.objects.filter(
                status__in=[CompanyFetchRun.Status.SUCCESS, CompanyFetchRun.Status.PARTIAL],
                completed_at__gte=fresh_cutoff,
            ).values_list("label_id", flat=True)
        )
        if fresh_label_pks:
            logger.info(
                "fetch_raw_jobs_batch: skipping %d labels fetched within last %dh",
                len(fresh_label_pks), min_hours_since_fetch,
            )

    if test_mode:
        # Pick up to `companies_per_platform` companies per platform slug
        per_plat = max(1, companies_per_platform)
        seen_platforms: dict[str, int] = {}  # slug -> count
        label_list = []
        for label in qs.iterator():
            slug = label.platform.slug if label.platform else ""
            if not slug:
                continue
            count = seen_platforms.get(slug, 0)
            if count < per_plat:
                seen_platforms[slug] = count + 1
                label_list.append(label.pk)
        logger.info(
            "fetch_raw_jobs_batch TEST MODE: %d platforms, %d companies selected (%d per platform)",
            len(seen_platforms), len(label_list), per_plat,
        )
    else:
        all_pks = list(qs.values_list("pk", flat=True))
        label_list = [pk for pk in all_pks if pk not in fresh_label_pks]
        skipped_fresh = len(all_pks) - len(label_list)
        if skipped_fresh:
            logger.info(
                "fetch_raw_jobs_batch: %d/%d labels skipped (fresh <%dh), %d queued",
                skipped_fresh, len(all_pks), min_hours_since_fetch, len(label_list),
            )

    total = len(label_list)

    batch.total_companies = total
    batch.save(update_fields=["total_companies"])

    update_task_progress(self, current=0, total=total, message=f"Dispatching {total} company fetches…")

    # Stagger by platform type: API platforms get a tighter stagger (0.1s),
    # HTML scrapers get a wider one (1.0s) to avoid hammering slow targets.
    # adp uses HTMLScrapeHarvester → needs the slower 1.5s scraper stagger
    SCRAPER_SLUGS = {"jobvite", "icims", "taleo", "ultipro", "applicantpro",
                     "applytojob", "theapplicantmanager", "zoho", "breezy", "teamtailor", "adp"}

    # Fetch label→platform slug mapping once to decide stagger
    label_platform_map: dict[int, str] = {}
    if label_list:
        for row in CompanyPlatformLabel.objects.filter(pk__in=label_list).values("pk", "platform__slug"):
            label_platform_map[row["pk"]] = row["platform__slug"] or ""

    api_offset = 0
    scraper_offset = 0
    for label_pk in label_list:
        slug = label_platform_map.get(label_pk, "")
        is_scraper = slug in SCRAPER_SLUGS
        if is_scraper:
            countdown = scraper_offset
            scraper_offset += 1.5   # 1.5s between scraper tasks
        else:
            countdown = api_offset
            api_offset += 0.1       # 0.1s between API tasks (they throttle internally)

        kwargs = {"max_jobs": test_max_jobs} if test_mode else {}
        if fetch_all and not test_mode:
            kwargs["fetch_all"] = True   # pass full-crawl flag to child tasks
        fetch_raw_jobs_for_company_task.apply_async(
            args=[label_pk, batch.pk, "BATCH"],
            kwargs=kwargs,
            countdown=countdown,
        )

    if label_list and label_list[0] % 50 == 0:
        pass  # progress update already at end
    update_task_progress(self, current=total, total=total,
                         message=f"All {total} fetches queued for batch #{batch.pk}")

    logger.info("fetch_raw_jobs_batch: queued %d companies (batch #%d, test=%s)", total, batch.pk, test_mode)
    return {"batch_id": batch.pk, "total_companies": total, "test_mode": test_mode}


@shared_task(bind=True, name="harvest.retry_failed_raw_jobs")
def retry_failed_raw_jobs_task(self):
    """Re-queue fetch_raw_jobs_for_company_task for all FAILED runs in the last 7 days."""
    from .models import CompanyFetchRun

    cutoff = timezone.now() - timedelta(days=7)
    failed_runs = CompanyFetchRun.objects.filter(
        status=CompanyFetchRun.Status.FAILED,
        started_at__gte=cutoff,
    ).select_related("label")

    queued = 0
    for run in failed_runs:
        fetch_raw_jobs_for_company_task.delay(
            run.label_id,
            run.batch_id,
            "SCHEDULED",
        )
        queued += 1

    logger.info("retry_failed_raw_jobs: re-queued %d tasks", queued)
    return {"queued": queued}


@shared_task(bind=True, name="harvest.validate_raw_job_urls")
def validate_raw_job_urls_task(
    self,
    platform_slug: str | None = None,
    batch_size: int = 200,
    concurrency: int = 20,
    max_jobs: int | None = None,
):
    """
    HEAD-check raw job URLs and mark is_active=False for ones that return 4xx/5xx.

    Runs after every FETCH ALL batch (or on a schedule) to surface broken links
    before a human ever sees them. Results are visible in the Jobs Browser
    (SYNC column stays PENDING; is_active=False jobs are hidden from candidates).

    Uses a thread pool for concurrency — HEAD requests are I/O bound so
    parallelism is safe and fast.

    platform_slug — limit to one platform (e.g. "workday")
    batch_size    — DB fetch chunk size (memory control)
    concurrency   — parallel HTTP threads
    max_jobs      — cap total checked (for quick spot-checks)
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .models import RawJob

    qs = RawJob.objects.filter(is_active=True).exclude(original_url="")
    if platform_slug:
        qs = qs.filter(platform_slug=platform_slug)
    if max_jobs:
        qs = qs[:max_jobs]

    total = qs.count()
    update_task_progress(self, current=0, total=total, message=f"Checking {total:,} URLs…")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; GoCareers-UrlValidator/1.0; +https://chennu.co)"
        )
    }

    checked = alive = dead = errors = 0

    def check_url(job_id: int, url: str) -> tuple[int, bool]:
        """Returns (job_id, is_alive)."""
        try:
            r = requests.head(url, timeout=10, allow_redirects=True, headers=headers)
            if r.status_code == 405:
                # HEAD blocked — try GET streaming (just headers)
                r = requests.get(url, timeout=12, stream=True, allow_redirects=True, headers=headers)
                r.close()
            return job_id, r.status_code < 400
        except Exception:
            return job_id, False  # treat network errors as dead

    offset = 0
    while True:
        chunk = list(qs.values("id", "original_url")[offset: offset + batch_size])
        if not chunk:
            break
        if max_jobs and offset >= max_jobs:
            break

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(check_url, row["id"], row["original_url"]): row["id"]
                for row in chunk
            }
            dead_ids = []
            for future in as_completed(futures):
                job_id, is_alive = future.result()
                checked += 1
                if is_alive:
                    alive += 1
                else:
                    dead += 1
                    dead_ids.append(job_id)

            # Mark dead jobs inactive in one batch update
            if dead_ids:
                RawJob.objects.filter(pk__in=dead_ids).update(is_active=False)

        offset += batch_size
        update_task_progress(
            self, current=checked, total=total,
            message=f"Checked {checked:,}/{total:,} — {alive:,} alive, {dead:,} dead",
        )

    logger.info(
        "validate_raw_job_urls: checked=%d alive=%d dead=%d errors=%d",
        checked, alive, dead, errors,
    )
    return {"checked": checked, "alive": alive, "dead": dead}


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

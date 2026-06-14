from celery import shared_task

from core.task_progress import update_task_progress
from urllib.parse import urlparse
import logging

from django.utils import timezone

from .models import Job
from harvest.url_health import check_job_posting_live

logger = logging.getLogger(__name__)


_JOB_DEPARTMENT_SYNC_ALIASES: dict[str, str] = {
    "information technology": Job.Department.IT_MANAGEMENT,
    "it": Job.Department.IT_MANAGEMENT,
    "non it business": Job.Department.MANAGEMENT,
    "non-it business": Job.Department.MANAGEMENT,
    "business": Job.Department.MANAGEMENT,
    "engineering": Job.Department.SOFTWARE_DEV,
    "engineering non it": Job.Department.CIVIL_ENG,
    "engineering construction": Job.Department.CIVIL_ENG,
    "devops sre": Job.Department.DEVOPS_CLOUD,
    "hr people": Job.Department.HR,
    "people": Job.Department.HR,
    "healthcare clinical": Job.Department.HEALTHCARE,
}


def _department_sync_value(raw_department: str | None) -> str:
    """
    Convert RawJob.department_normalized/freeform category labels to Job.department
    enum codes before syncing. Job.department is a varchar(20) choice field; raw
    labels like "Information Technology" must never be written directly.
    """
    raw = (raw_department or "").strip()
    if not raw:
        return ""

    valid_values = {value for value, _label in Job.Department.choices}
    if raw in valid_values:
        return raw

    lowered = raw.lower().strip()
    label_map = {label.lower(): value for value, label in Job.Department.choices}
    if lowered in label_map:
        return label_map[lowered]

    compact = lowered.replace("&", " ").replace("/", " ").replace("_", " ").replace("-", " ")
    compact = " ".join(compact.split())
    return _JOB_DEPARTMENT_SYNC_ALIASES.get(compact, "")


def _department_sync_sql_case(source_column: str = "department_normalized") -> str:
    """
    SQL equivalent of _department_sync_value for the production bulk UPDATE path.
    The returned expression only emits valid Job.department enum codes or ''.
    """
    value_pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    for value, label in Job.Department.choices:
        for raw in (value, label):
            key = str(raw).lower().replace("&", " ").replace("/", " ").replace("_", " ").replace("-", " ")
            key = " ".join(key.split())
            if key and key not in seen:
                seen.add(key)
                value_pairs.append((key, value))

    for key, value in _JOB_DEPARTMENT_SYNC_ALIASES.items():
        key = " ".join(key.lower().replace("&", " ").replace("/", " ").replace("_", " ").replace("-", " ").split())
        if key and key not in seen:
            seen.add(key)
            value_pairs.append((key, value))

    normalized_expr = (
        f"btrim(lower(regexp_replace(replace(replace(replace(replace({source_column}, '&', ' '), "
        f"'/', ' '), '_', ' '), '-', ' '), "
        r"'\s+', ' ', 'g')))"
    )
    cases = "\n".join(
        f"            WHEN {normalized_expr} = '{key}' THEN '{value}'"
        for key, value in value_pairs
    )
    return f"""CASE
{cases}
            ELSE ''
        END"""


@shared_task
def generate_job_matches_task(job_id: int, notify: bool = True):
    """
    Embed job + compute cosine similarity against all consultant embeddings.
    Optionally notify top matches via in-app notification.
    """
    from .matching import embed_job, compute_matches_for_job, notify_top_matches_for_job
    try:
        job = Job.objects.get(pk=job_id)
    except Job.DoesNotExist:
        return {"error": f"Job {job_id} not found"}

    embed_job(job)
    results = compute_matches_for_job(job, top_n=20)
    if notify and results:
        notify_top_matches_for_job(job, top_n=5)

    return {"job_id": job_id, "matches_computed": len(results)}


@shared_task
def refresh_consultant_embeddings_task():
    """Regenerate embeddings for all active consultant profiles. Run weekly."""
    from users.models import ConsultantProfile
    from .matching import embed_consultant

    profiles = ConsultantProfile.objects.select_related('user').prefetch_related(
        'marketing_roles', 'experience'
    )
    updated = 0
    for profile in profiles:
        if embed_consultant(profile):
            updated += 1
    return {"updated": updated}


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
    return url


def _check_job_url(url: str) -> bool:
    if not url:
        return False
    url = _normalize_url(url)
    try:
        result = check_job_posting_live(url)
        return bool(result.is_live)
    except Exception:
        return False


@shared_task
def run_job_validation(job_id: int):
    """
    Run quality validation on a single job and persist the score.
    Auto-promotes to OPEN if the score meets PlatformConfig.auto_approve_pool_threshold.
    Called async when a job enters POOL status.
    """
    from .services import validate_job_quality, ensure_parsed_jd
    from .gating import apply_gate_result_to_job, evaluate_job_gate

    try:
        job = Job.objects.get(pk=job_id)
    except Job.DoesNotExist:
        return {"error": f"Job {job_id} not found"}

    # Ensure JD is parsed first so skills check is meaningful
    ensure_parsed_jd(job)
    job.refresh_from_db()

    result = validate_job_quality(job)
    job.validation_score = result["score"]
    job.validation_result = result
    job.validation_run_at = timezone.now()
    gate = evaluate_job_gate(job)
    apply_gate_result_to_job(job, gate)
    job.gate_checked_at = timezone.now()
    job.save(
        update_fields=[
            "validation_score", "validation_result", "validation_run_at",
            "hard_gate_passed", "gate_status", "vet_lane",
            "pipeline_reason_code", "pipeline_reason_detail",
            "hard_gate_failures", "hard_gate_checks",
            "data_quality_score", "trust_score", "candidate_fit_score",
            "vet_priority_score", "gate_checked_at",
        ]
    )

    # Auto-approve if threshold met
    if result.get("auto_approved") and job.status == Job.Status.POOL and gate.passed:
        job.status = Job.Status.OPEN
        job.stage = Job.Stage.LIVE
        job.vet_approved_at = timezone.now()
        job.save(update_fields=["status", "stage", "vet_approved_at", "updated_at"])
        try:
            from .notify import notify_new_open_job_to_consultants, notify_job_pool_status
            notify_new_open_job_to_consultants(job)
            notify_job_pool_status(job, approved=True, auto=True)
        except Exception:
            logger.exception("Auto-approve notification failed for job %s", job_id)

    # Notify pool review recipients if the job is still in pool (not auto-approved)
    if job.status == Job.Status.POOL:
        _notify_pool_review_emails(job, result)

    logger.info("Job %s validation complete — score=%s auto_approved=%s", job_id, result["score"], result.get("auto_approved"))
    return {"job_id": job_id, "score": result["score"], "auto_approved": result.get("auto_approved")}


def _notify_pool_review_emails(job: Job, validation_result: dict):
    """
    Send a plain-text email to pool_review_notify_emails when a job needs manual review.
    Uses Django's send_mail; silently skips if not configured.
    """
    try:
        from core.models import PlatformConfig
        cfg = PlatformConfig.load()
        raw = (getattr(cfg, 'pool_review_notify_emails', '') or '').strip()
        if not raw:
            return
        recipients = [e.strip() for e in raw.split(',') if e.strip() and '@' in e]
        if not recipients:
            return

        from django.core.mail import send_mail
        from django.conf import settings
        from django.urls import reverse

        score = validation_result.get('score', '?')
        issues = validation_result.get('issues', [])
        issue_lines = '\n'.join(
            f"  [{i['severity'].upper()}] {i['message']}" for i in issues
        ) or '  None'

        try:
            pool_url = settings.SITE_URL.rstrip('/') + reverse('job-pool')
        except Exception:
            pool_url = reverse('job-pool')

        subject = f"[Job Pool] New job needs review: {job.title} at {job.company}"
        body = (
            f"A new job has been added to the vetting pool and needs your review.\n\n"
            f"Job: {job.title}\n"
            f"Company: {job.company}\n"
            f"Location: {job.location or 'Not specified'}\n"
            f"Validation Score: {score}/100\n\n"
            f"Issues:\n{issue_lines}\n\n"
            f"Review the job pool here:\n{pool_url}\n"
        )

        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com'),
            recipient_list=recipients,
            fail_silently=True,
        )
        logger.info("Pool review notification sent to %s for job %s", recipients, job.pk)
    except Exception:
        logger.exception("Failed to send pool review notification for job %s", job.pk)


@shared_task(bind=True)
def validate_job_urls_task(self, batch_size: int = 50):
    """
    Re-check original job URLs and flag jobs as 'possibly_filled' when their source goes away.
    Runs daily via Celery beat (see core.signals).
    """
    now = timezone.now()
    cutoff = now - timezone.timedelta(hours=24)

    # Only check Jobs without a source_raw_job — those linked to a RawJob are
    # already handled by validate_raw_job_urls_task which propagates status here.
    qs = Job.objects.filter(status__in=[Job.Status.OPEN, Job.Status.POOL], is_archived=False)
    qs = qs.filter(original_link__isnull=False).exclude(original_link="")
    qs = qs.filter(source_raw_job__isnull=True)
    qs = qs.filter(
        original_link_last_checked_at__lt=cutoff
    ) | qs.filter(
        original_link_last_checked_at__isnull=True
    )

    jobs = list(qs[:batch_size])
    total_n = len(jobs)
    if total_n:
        update_task_progress(self, current=0, total=total_n, message="Checking job posting URLs…")

    processed = 0
    for i, job in enumerate(jobs, start=1):
        was_pf = job.possibly_filled
        is_live = _check_job_url(job.original_link)
        job.original_link_is_live = is_live
        job.original_link_last_checked_at = now
        # If URL is not live and job is still marked OPEN, flag as possibly filled.
        job.possibly_filled = not is_live and job.status == Job.Status.OPEN
        job.save(update_fields=["original_link_is_live", "original_link_last_checked_at", "possibly_filled"])
        processed += 1
        if job.possibly_filled and not was_pf:
            try:
                from jobs.notify import notify_job_posting_link_unhealthy

                notify_job_posting_link_unhealthy(job)
            except Exception:
                pass

        if total_n:
            update_task_progress(
                self,
                current=i,
                total=total_n,
                message=f"URL check {i}/{total_n}",
            )

    result = {"processed": processed}
    try:
        from core.models import PipelineRunLog
        PipelineRunLog.objects.update_or_create(
            task_name="validate_job_urls",
            defaults={"last_run_at": timezone.now(), "last_run_result": result},
        )
    except Exception:
        pass
    return result


@shared_task
def auto_close_jobs_task():
    """
    Close stale OPEN jobs per PlatformConfig:
    - Optional: age in days (job_auto_close_after_days)
    - Optional: dead original link (job_auto_close_when_link_dead)
    """
    from core.models import PlatformConfig, PipelineRunLog

    config = PlatformConfig.load()
    now = timezone.now()
    closed_age = 0
    closed_dead = 0

    days = getattr(config, "job_auto_close_after_days", None)
    if days and days > 0:
        cutoff = now - timezone.timedelta(days=days)
        qs = Job.objects.filter(status=Job.Status.OPEN, created_at__lt=cutoff)
        for job in qs.iterator():
            job.status = Job.Status.CLOSED
            job.save(update_fields=["status", "updated_at"])
            closed_age += 1
            try:
                from jobs.notify import notify_job_auto_closed_for_owner

                notify_job_auto_closed_for_owner(job)
            except Exception:
                pass

    if getattr(config, "job_auto_close_when_link_dead", False):
        qs = Job.objects.filter(
            status=Job.Status.OPEN,
            original_link_is_live=False,
        )
        for job in qs.iterator():
            job.status = Job.Status.CLOSED
            job.save(update_fields=["status", "updated_at"])
            closed_dead += 1
            try:
                from jobs.notify import notify_job_auto_closed_for_owner

                notify_job_auto_closed_for_owner(job)
            except Exception:
                pass

    result = {"closed_stale_days": closed_age, "closed_dead_link": closed_dead}
    try:
        PipelineRunLog.objects.update_or_create(
            task_name="auto_close_jobs",
            defaults={"last_run_at": timezone.now(), "last_run_result": result},
        )
    except Exception:
        pass
    return result


# ── Job Classification Engine ─────────────────────────────────────────────────

CLASSIFY_LOCK_KEY = "jobs:classify_all:lock"
CLASSIFY_ACTIVE_TASK_KEY = "jobs:classify_all:active_task"
CLASSIFY_LOCK_TTL = 60 * 180  # fallback — actual TTL read from HarvestEngineConfig


def _classify_lock_ttl() -> int:
    """Return lock TTL in seconds from HarvestEngineConfig (configurable from GUI)."""
    try:
        from harvest.models import HarvestEngineConfig
        return int(HarvestEngineConfig.get().classify_lock_ttl_minutes) * 60
    except Exception:
        return CLASSIFY_LOCK_TTL


@shared_task(bind=True, name="jobs.classify_all", max_retries=0, soft_time_limit=10800, time_limit=11100)
def classify_jobs_task(self, force_reclassify: bool = False, active_only: bool = False):
    """
    Full taxonomy backfill for harvested jobs.
    Updates RawJob taxonomy fields and then backfills Job.marketing_roles for
    synced jobs so the UI can route consultants immediately after completion.

    Lock TTL is configurable via HarvestEngineConfig.classify_lock_ttl_minutes.
    Staff can force-clear the lock via the Force Unlock button in Raw Controls.
    """
    from django.core.cache import cache

    from harvest.models import HarvestOpsRun
    from harvest.ops_audit import begin_ops_run, finish_ops_run

    task_id = getattr(self.request, "id", "") or "running"
    lock_ttl = _classify_lock_ttl()
    acquired = cache.add(CLASSIFY_LOCK_KEY, task_id, lock_ttl)
    if not acquired:
        logger.warning("classify_jobs_task: already running, aborting duplicate.")
        dup_op = begin_ops_run(
            HarvestOpsRun.Operation.CLASSIFY,
            task_id,
            queue={"force_reclassify": force_reclassify, "active_only": active_only, "duplicate_lock": True},
        )
        finish_ops_run(dup_op, HarvestOpsRun.Status.SKIPPED, {"reason": "lock_held"})
        return {"status": "skipped", "reason": "lock_held"}
    cache.set(CLASSIFY_ACTIVE_TASK_KEY, task_id, lock_ttl)

    ops_run = begin_ops_run(
        HarvestOpsRun.Operation.CLASSIFY,
        task_id,
        queue={"force_reclassify": force_reclassify, "active_only": active_only, "mode": "taxonomy"},
    )
    try:
        result = _run_classify_raw(self, force_reclassify=force_reclassify, active_only=active_only)
        finish_ops_run(ops_run, HarvestOpsRun.Status.SUCCESS, result)
        return result
    except Exception as e:
        logger.exception("classify_jobs_task failed: %s", e)
        finish_ops_run(ops_run, HarvestOpsRun.Status.FAILED, {"error": str(e)[:500]})
        raise
    finally:
        cache.delete(CLASSIFY_LOCK_KEY)
        cache.delete(CLASSIFY_ACTIVE_TASK_KEY)


def _run_classify_raw(task_self, *, force_reclassify: bool, active_only: bool = False) -> dict:
    from django.db import transaction
    from harvest.models import HarvestEngineConfig, RawJob
    from harvest.enrichments import (
        CURRENT_DOMAIN_VERSION,
        detect_job_category,
        detect_job_domains,
    )
    from jobs.marketing_role_routing import assign_marketing_roles_to_job, infer_marketing_role_slugs
    from .models import Job

    # Configurable chunk limit prevents task timeouts on very large backlogs.
    # 0 = unlimited (original behaviour).
    try:
        chunk_limit = int(HarvestEngineConfig.get().classify_chunk_limit)
    except Exception:
        chunk_limit = 0

    raw_qs = RawJob.objects.exclude(title="").order_by("pk")
    if active_only:
        raw_qs = raw_qs.filter(is_test_run=False, is_active=True)
    if not force_reclassify:
        raw_qs = raw_qs.exclude(domain_version=CURRENT_DOMAIN_VERSION)
    if chunk_limit > 0:
        raw_qs = raw_qs[:chunk_limit]

    synced_qs = RawJob.objects.filter(sync_status=RawJob.SyncStatus.SYNCED).order_by("pk")
    if active_only:
        synced_qs = synced_qs.filter(is_test_run=False, is_active=True)
    if chunk_limit > 0:
        # Proportional split: spend half the limit on synced roles
        synced_qs = synced_qs[:chunk_limit // 2]

    raw_total = raw_qs.count()
    synced_total = synced_qs.count()
    total = raw_total + synced_total
    if total == 0:
        return {"status": "done", "total": 0, "classified": 0, "roles_assigned": 0}

    update_task_progress(task_self, current=0, total=total, message="Starting taxonomy backfill…")

    processed = 0
    chunk: list[RawJob] = []
    CHUNK = 1000
    classified = 0
    categorized = 0
    fallback_routed = 0
    unclassified = 0

    for rj in raw_qs.iterator(chunk_size=CHUNK):
        description = rj.description_clean or rj.description or ""
        domains = detect_job_domains(
            rj.title or "",
            description,
            rj.job_category or "",
            rj.department_normalized or "",
            max_matches=3,
        )
        domain = domains[0] if domains else ""
        category, _title_match, _desc_match = detect_job_category(
            rj.title or "",
            description,
            department_normalized=rj.department_normalized or "",
            domain_slug=domain,
        )

        if not domains:
            domains = infer_marketing_role_slugs(
                title=rj.title or "",
                description=description,
                job_category=category or rj.job_category or "",
                department_normalized=rj.department_normalized or "",
                primary_domain="",
                max_roles=3,
            )
            domain = domains[0] if domains else ""
            if domain:
                fallback_routed += 1

        rj.job_category = category or rj.job_category or ""
        rj.job_domain = domain
        rj.job_domain_candidates = list(domains[:3])
        rj.domain_version = CURRENT_DOMAIN_VERSION

        chunk.append(rj)
        processed += 1
        if rj.job_category:
            categorized += 1
        if domain:
            classified += 1
        else:
            unclassified += 1

        if len(chunk) >= CHUNK:
            with transaction.atomic():
                RawJob.objects.bulk_update(
                    chunk,
                    ["job_category", "job_domain", "job_domain_candidates", "domain_version"],
                )
            chunk.clear()
            update_task_progress(
                task_self,
                current=processed,
                total=total,
                message=f"Taxonomy {processed:,} / {raw_total:,} raw jobs…",
                detail={
                    "phase": "raw_taxonomy",
                    "classified": classified,
                    "categorized": categorized,
                    "fallback_routed": fallback_routed,
                    "unclassified": unclassified,
                },
            )

    if chunk:
        with transaction.atomic():
            RawJob.objects.bulk_update(
                chunk,
                ["job_category", "job_domain", "job_domain_candidates", "domain_version"],
            )

    _sync_classifications_to_jobs(force=force_reclassify, active_only=active_only)

    role_assigned = 0
    role_skipped = 0
    role_missing_job = 0
    synced_processed = 0

    last_pk = None
    while True:
        page_qs = synced_qs if last_pk is None else synced_qs.filter(pk__gt=last_pk)
        batch = list(page_qs.select_related("company")[:CHUNK])
        if not batch:
            break
        last_pk = batch[-1].pk

        for rj in batch:
            job = (
                Job.objects.filter(source_raw_job=rj).first()
                or (Job.objects.filter(url_hash=rj.url_hash).first() if rj.url_hash else None)
            )
            if not job:
                role_missing_job += 1
                synced_processed += 1
                continue

            if active_only and (job.is_archived or job.status not in {Job.Status.POOL, Job.Status.OPEN}):
                role_skipped += 1
                synced_processed += 1
                continue

            if not force_reclassify and job.marketing_roles.exists():
                role_skipped += 1
                synced_processed += 1
                continue

            if assign_marketing_roles_to_job(job, raw_job=rj):
                role_assigned += 1
            synced_processed += 1

        update_task_progress(
            task_self,
            current=raw_total + synced_processed,
            total=total,
            message=f"Roles {synced_processed:,} / {synced_total:,} synced jobs…",
            detail={
                "phase": "job_roles",
                "classified": classified,
                "categorized": categorized,
                "fallback_routed": fallback_routed,
                "roles_assigned": role_assigned,
                "role_skipped": role_skipped,
                "role_missing_job": role_missing_job,
            },
        )

    update_task_progress(
        task_self,
        current=total,
        total=total,
        message=f"Done — taxonomy applied to {processed:,} raw jobs and roles backfilled on {role_assigned:,} jobs.",
        detail={
            "phase": "done",
            "classified": classified,
            "categorized": categorized,
            "fallback_routed": fallback_routed,
            "unclassified": unclassified,
            "roles_assigned": role_assigned,
            "role_skipped": role_skipped,
            "role_missing_job": role_missing_job,
        },
    )

    return {
        "status": "done",
        "total": raw_total,
        "classified": processed,
        "categorized": categorized,
        "fallback_routed": fallback_routed,
        "unclassified": unclassified,
        "roles_assigned": role_assigned,
        "role_skipped": role_skipped,
        "role_missing_job": role_missing_job,
        "domain_version": CURRENT_DOMAIN_VERSION,
        "active_only": active_only,
    }


def _sync_classifications_to_jobs(*, force: bool = False, active_only: bool = False):
    """Copy country + department from RawJob → Job for all linked records.

    force=True skips the "only update empty fields" filter so existing values
    are overwritten (respects department_source='manual' regardless).
    """
    from django.db import connection
    from django.utils import timezone as tz

    if connection.vendor == "sqlite":
        qs = Job.objects.select_related("source_raw_job").filter(
            source_raw_job__isnull=False,
            source_raw_job__is_active=True,
        )
        if active_only:
            qs = qs.filter(is_archived=False, status__in=[Job.Status.POOL, Job.Status.OPEN])
        for job in qs.iterator(chunk_size=500):
            raw = job.source_raw_job
            changed: list[str] = []

            if raw and raw.country and (force or not job.country):
                job.country = raw.country
                changed.append("country")

            mapped_department = _department_sync_value(raw.department_normalized if raw else "")
            if (
                raw
                and mapped_department
                and job.department_source != "manual"
                and (force or not job.department)
            ):
                job.department = mapped_department
                job.department_source = "raw_job"
                changed.extend(["department", "department_source"])

            if changed:
                job.classified_at = tz.now()
                changed.extend(["classified_at", "updated_at"])
                job.save(update_fields=changed)
        return

    # When force=False we only touch Jobs where the field is still blank.
    # When force=True we overwrite whatever was there (except manual dept).
    where_filter = "" if force else """
              AND (
                  (r.country <> '' AND (j.country IS NULL OR j.country = ''))
                  OR (r.mapped_department <> '' AND j.department_source <> 'manual'
                      AND (j.department IS NULL OR j.department = ''))
              )"""
    job_scope_filter = ""
    if active_only:
        job_scope_filter = "AND j.is_archived = false AND j.status IN ('POOL', 'OPEN')"

    department_case = _department_sync_sql_case("department_normalized")
    with connection.cursor() as cur:
        cur.execute(f"""
            WITH raw_mapped AS (
                SELECT
                    id,
                    country,
                    {department_case} AS mapped_department
                FROM harvest_rawjob
                WHERE is_active = true
            )
            UPDATE jobs_job j
            SET
                country            = COALESCE(NULLIF(r.country, ''), j.country),
                department         = CASE
                                       WHEN j.department_source = 'manual' THEN j.department
                                       WHEN r.mapped_department <> ''      THEN r.mapped_department
                                       ELSE j.department
                                     END,
                department_source  = CASE
                                       WHEN j.department_source = 'manual'   THEN j.department_source
                                       WHEN r.mapped_department <> ''        THEN 'raw_job'
                                       ELSE j.department_source
                                     END,
                classified_at      = NOW()
            FROM raw_mapped r
            WHERE r.id = j.source_raw_job_id
              {job_scope_filter}
              {where_filter}
        """)


# ── Legacy: classify manually-posted Jobs that have no RawJob source ──────────

CLASSIFY_JOB_ONLY_LOCK_KEY = "jobs:classify_jobs_only:lock"


@shared_task(bind=True, name="jobs.classify_manual_jobs", max_retries=0, soft_time_limit=3600, time_limit=3900)
def classify_manual_jobs_task(self, force_reclassify: bool = False):
    """Classify Job records that have no raw_job_id (manually-posted jobs)."""
    from django.core.cache import cache
    from django.utils import timezone as tz
    from django.db.models import Q

    from .classifier.country import detect_country
    from .classifier.department import classify_department

    acquired = cache.add(CLASSIFY_JOB_ONLY_LOCK_KEY, self.request.id or "running", 3600)
    if not acquired:
        return {"status": "skipped", "reason": "lock_held"}

    try:
        qs = Job.objects.select_related("company_obj").filter(source_raw_job__isnull=True).order_by("pk")
        if not force_reclassify:
            qs = qs.filter(
                Q(classified_at__isnull=True) | Q(needs_reclassification=True)
            ).exclude(department_source="manual")

        total = qs.count()
        if total == 0:
            return {"status": "done", "total": 0, "classified": 0}

        stats: dict[str, int] = {"rules": 0, "role_domain": 0, "embedding": 0, "llm": 0}
        country_found = 0
        processed = 0
        chunk: list[Job] = []
        CHUNK = 500

        for job in qs.iterator(chunk_size=CHUNK):
            if not job.country or force_reclassify:
                country, region = detect_country(
                    location=job.location or "",
                    title=job.title or "",
                    description=job.description or "",
                )
                job.country = country
                job.region = region
                if country:
                    country_found += 1

            if not job.department or force_reclassify:
                company_industry = (job.company_obj.industry or "") if job.company_obj else ""
                dept, conf, source = classify_department(
                    title=job.title or "",
                    description=job.description or "",
                    role_domain=(job.parsed_jd or {}).get("role_domain", ""),
                    company_industry=company_industry,
                    use_llm=True,
                    llm_threshold=0.45,
                )
                job.department = dept
                job.department_confidence = round(conf, 4)
                job.department_source = source
                stats[source] = stats.get(source, 0) + 1

            job.classified_at = tz.now()
            job.needs_reclassification = False
            chunk.append(job)
            processed += 1

            if len(chunk) >= CHUNK:
                Job.objects.bulk_update(
                    chunk,
                    ["country", "region", "department", "department_confidence",
                     "department_source", "classified_at", "needs_reclassification"],
                )
                chunk.clear()

        if chunk:
            Job.objects.bulk_update(
                chunk,
                ["country", "region", "department", "department_confidence",
                 "department_source", "classified_at", "needs_reclassification"],
            )

        return {"status": "done", "total": total, "classified": processed,
                "country_found": country_found, **stats}
    finally:
        cache.delete(CLASSIFY_JOB_ONLY_LOCK_KEY)

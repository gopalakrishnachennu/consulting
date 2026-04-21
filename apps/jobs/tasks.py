from celery import shared_task
from urllib.request import Request, urlopen

from core.task_progress import update_task_progress
from urllib.parse import urlparse
import ssl
import logging

from django.utils import timezone

from .models import Job

logger = logging.getLogger(__name__)


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
        ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "GoCareers-job-url-checker/1.0"})
        req.get_method = lambda: "HEAD"
        try:
            resp = urlopen(req, context=ctx, timeout=5)
        except Exception:
            # Fallback to GET
            req.get_method = lambda: "GET"
            resp = urlopen(req, context=ctx, timeout=5)
        status = getattr(resp, "status", None) or getattr(resp, "code", None)
        if status is None:
            return True
        status = int(status)
        # 4xx/5xx are dead links.
        if status >= 400:
            return False
        # 3xx redirect chain accepted as live.
        if 300 <= status < 400:
            return True
        # For 2xx responses, detect "soft 404" pages that still return HTTP 200.
        try:
            req_get = Request(url, headers={"User-Agent": "GoCareers-job-url-checker/1.0"})
            req_get.get_method = lambda: "GET"
            resp_get = urlopen(req_get, context=ctx, timeout=7)
            body = (resp_get.read(8192) or b"").decode("utf-8", errors="ignore").lower()
            soft_404_markers = (
                "page you are looking for doesn't exist",
                "page you are looking for does not exist",
                "job not found",
                "this job is no longer available",
                "position no longer available",
                "404",
            )
            if any(m in body for m in soft_404_markers):
                return False
        except Exception:
            # If body check fails but status was 2xx, keep as live to avoid false negatives.
            pass
        return True
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
    job.save(update_fields=["validation_score", "validation_result", "validation_run_at"])

    # Auto-approve if threshold met
    if result.get("auto_approved") and job.status == Job.Status.POOL:
        job.status = Job.Status.OPEN
        job.save(update_fields=["status"])
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

    # Check both OPEN and POOL jobs so pool UI status stays accurate too.
    qs = Job.objects.filter(status__in=[Job.Status.OPEN, Job.Status.POOL], is_archived=False)
    qs = qs.filter(original_link__isnull=False).exclude(original_link="")
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


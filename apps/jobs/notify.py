"""In-app (and optional email) notifications for job lifecycle events."""

import logging
from django.urls import reverse

from core.models import Notification
from core.notification_utils import create_notification, notify_job_email_optional

from .models import Job
from .services import match_consultants_for_job

logger = logging.getLogger(__name__)


def notify_new_open_job_to_consultants(job: Job) -> int:
    """
    Notify matched consultants when a new OPEN job is posted (marketing fit / score).
    Returns number of in-app notifications created (excludes skipped due to prefs).
    """
    if job.status != Job.Status.OPEN:
        return 0
    consultants = match_consultants_for_job(job, limit=50)
    if not consultants:
        return 0
    path = reverse('job-detail', kwargs={'pk': job.pk})
    created = 0
    for c in consultants:
        u = c.user
        n = create_notification(
            u,
            kind=Notification.Kind.JOB,
            title=f"New job: {job.title}",
            body=f"{job.company} · {job.location or 'Location TBD'}",
            link=path,
            dedupe_key=f"job_new:{job.pk}:{u.pk}",
        )
        if n is not None:
            created += 1
            notify_job_email_optional(
                u,
                subject=f"New job: {job.title}",
                body=f"{job.company}\n{job.location or ''}",
                link_path=path,
            )
    return created


def notify_job_closed_to_applicants(job: Job) -> int:
    """Notify consultants who applied when a job moves to CLOSED."""
    if job.status != Job.Status.CLOSED:
        return 0
    from submissions.models import ApplicationSubmission

    path = reverse('job-detail', kwargs={'pk': job.pk})
    subs = ApplicationSubmission.objects.filter(job=job).select_related('consultant__user')
    n_created = 0
    for sub in subs:
        u = sub.consultant.user
        n = create_notification(
            u,
            kind=Notification.Kind.JOB,
            title=f"Job closed: {job.title}",
            body=f"{job.company}",
            link=path,
            dedupe_key=f"job_closed:{job.pk}:{u.pk}",
        )
        if n is not None:
            n_created += 1
            notify_job_email_optional(
                u,
                subject=f"Job closed: {job.title}",
                body=f"{job.company}",
                link_path=path,
            )
    return n_created


def notify_job_auto_closed_for_owner(job: Job) -> None:
    """Notify the job owner when automation closes a job."""
    if not job.posted_by_id:
        return
    path = reverse('job-detail', kwargs={'pk': job.pk})
    create_notification(
        job.posted_by,
        kind=Notification.Kind.JOB,
        title=f"Job auto-closed: {job.title}",
        body=f"{job.company} — status set to Closed by system rules.",
        link=path,
        dedupe_key=f"job_autoclose:{job.pk}",
    )
    notify_job_email_optional(
        job.posted_by,
        subject=f"Job auto-closed: {job.title}",
        body=f"{job.company}",
        link_path=path,
    )


def notify_job_pool_status(job: Job, approved: bool, auto: bool = False, actor=None) -> None:
    """Notify the job poster when their pool job is approved or rejected."""
    if not job.posted_by_id:
        return
    path = reverse('job-detail', kwargs={'pk': job.pk})
    if approved:
        verb = "Auto-approved" if auto else "Approved"
        title = f"{verb}: {job.title}"
        body = f"{job.company} has been moved to Open and is now visible to consultants."
    else:
        title = f"Job not approved: {job.title}"
        reason = (job.rejection_reason or "").strip()
        body = f"{job.company} was not approved.{(' Reason: ' + reason) if reason else ''}"

    create_notification(
        job.posted_by,
        kind=Notification.Kind.JOB,
        title=title,
        body=body,
        link=path,
        dedupe_key=f"job_pool_{'approved' if approved else 'rejected'}:{job.pk}",
    )
    notify_job_email_optional(
        job.posted_by,
        subject=title,
        body=body,
        link_path=path,
    )


def notify_job_posting_link_unhealthy(job: Job) -> None:
    """Notify owner when the original posting URL appears dead (possibly filled)."""
    if not job.posted_by_id:
        return
    path = reverse('job-detail', kwargs={'pk': job.pk})
    create_notification(
        job.posted_by,
        kind=Notification.Kind.JOB,
        title=f"Job link may be dead: {job.title}",
        body=f"Original URL for {job.company} returned an error. Review whether the role is still open.",
        link=path,
        dedupe_key=f"job_deadlink:{job.pk}",
    )
    notify_job_email_optional(
        job.posted_by,
        subject=f"Job link check: {job.title}",
        body=f"The original posting URL may no longer be live ({job.company}).",
        link_path=path,
    )

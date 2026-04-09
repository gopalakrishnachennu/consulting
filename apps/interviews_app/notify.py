"""In-app notifications for interview scheduling and feedback."""

import hashlib

from django.urls import reverse

from core.models import Notification
from core.notification_utils import create_notification
from users.models import User


def notify_interview_scheduled(interview) -> None:
    """
    Consultant scheduled an interview — notify recruiting staff (submitter + job owner) except the consultant.
    """
    sub = interview.submission
    consultant_uid = interview.consultant.user_id
    recipient_ids = set()
    if sub.submitted_by_id:
        recipient_ids.add(sub.submitted_by_id)
    if sub.job.posted_by_id:
        recipient_ids.add(sub.job.posted_by_id)
    recipient_ids.discard(consultant_uid)

    path = reverse('interview-detail', kwargs={'pk': interview.pk})
    when = interview.scheduled_at.strftime('%b %d, %Y %H:%M UTC') if interview.scheduled_at else ''
    title = f"Interview scheduled: {interview.job_title}"
    body = f"{interview.company} · {interview.get_round_display()}" + (f" · {when}" if when else '')

    for uid in recipient_ids:
        u = User.objects.get(pk=uid)
        create_notification(
            u,
            kind=Notification.Kind.INTERVIEW,
            title=title,
            body=body,
            link=path,
            dedupe_key=f"interview_sched:{interview.pk}",
        )


def notify_interview_updated(interview, *, old_status: str, old_scheduled_at) -> None:
    """Reschedule or status change — notify same staff audience."""
    if old_status == interview.status and old_scheduled_at == interview.scheduled_at:
        return
    sub = interview.submission
    consultant_uid = interview.consultant.user_id
    recipient_ids = set()
    if sub.submitted_by_id:
        recipient_ids.add(sub.submitted_by_id)
    if sub.job.posted_by_id:
        recipient_ids.add(sub.job.posted_by_id)
    recipient_ids.discard(consultant_uid)

    path = reverse('interview-detail', kwargs={'pk': interview.pk})
    title = f"Interview updated: {interview.job_title}"
    body = f"{interview.company} — now {interview.get_status_display()}"

    sig = hashlib.md5(
        f"{interview.pk}:{interview.status}:{interview.scheduled_at}".encode()
    ).hexdigest()[:12]

    for uid in recipient_ids:
        u = User.objects.get(pk=uid)
        create_notification(
            u,
            kind=Notification.Kind.INTERVIEW,
            title=title,
            body=body,
            link=path,
            dedupe_key=f"interview_upd:{interview.pk}:{sig}",
        )


def notify_interview_feedback_submitted(feedback) -> None:
    """Staff submitted scorecard — notify the consultant."""
    interview = feedback.interview
    consultant = interview.consultant.user
    path = reverse('interview-detail', kwargs={'pk': interview.pk})
    create_notification(
        consultant,
        kind=Notification.Kind.INTERVIEW,
        title=f"Interview feedback: {interview.job_title}",
        body=f"New feedback recorded for {interview.company}.",
        link=path,
        dedupe_key=f"interview_fb:{feedback.pk}",
    )

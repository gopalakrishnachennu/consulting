"""Phase 4: Celery tasks for follow-up reminders and stale submission alerts."""
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from datetime import timedelta


@shared_task
def send_followup_reminders():
    """
    Check for pending follow-up reminders that are due and send notifications.
    Runs periodically via Celery Beat.
    """
    from .models import FollowUpReminder
    from core.notification_utils import create_notification
    from core.models import Notification

    now = timezone.now()
    due_reminders = FollowUpReminder.objects.filter(
        status=FollowUpReminder.ReminderStatus.PENDING,
        remind_at__lte=now,
    ).select_related("submission__job", "submission__consultant__user", "created_by")

    sent = 0
    for reminder in due_reminders:
        with transaction.atomic():
            locked = (
                FollowUpReminder.objects.select_for_update()
                .filter(
                    pk=reminder.pk,
                    status=FollowUpReminder.ReminderStatus.PENDING,
                )
                .first()
            )
            if not locked:
                continue
            sub = locked.submission
            notify_user = locked.created_by or sub.submitted_by
            if notify_user:
                create_notification(
                    notify_user,
                    kind=Notification.Kind.SUBMISSION,
                    title=f"Follow-up: {sub.consultant.user.get_full_name()} → {sub.job.title}",
                    body=locked.message or f"Submission #{sub.pk} needs follow-up.",
                    link=f"/submissions/{sub.pk}/",
                    dedupe_key=f"followup_reminder:{locked.pk}",
                )
            locked.status = FollowUpReminder.ReminderStatus.SENT
            locked.sent_at = now
            locked.save(update_fields=["status", "sent_at"])
            sent += 1

    return {"sent": sent}


@shared_task
def detect_stale_submissions():
    """
    Find submissions that haven't been updated in 14+ days and create
    notifications for the submitting employee.
    """
    from .models import ApplicationSubmission
    from core.notification_utils import create_notification
    from core.models import Notification

    cutoff = timezone.now() - timedelta(days=14)
    stale = ApplicationSubmission.objects.filter(
        updated_at__lt=cutoff,
        status__in=[
            ApplicationSubmission.Status.APPLIED,
            ApplicationSubmission.Status.IN_PROGRESS,
            ApplicationSubmission.Status.INTERVIEW,
        ],
        is_archived=False,
    ).select_related("job", "consultant__user", "submitted_by")

    created = 0
    for sub in stale:
        if sub.submitted_by:
            # Avoid duplicate notifications — check if one was sent in last 7 days
            recent = Notification.objects.filter(
                user=sub.submitted_by,
                kind=Notification.Kind.SUBMISSION,
                title__startswith="Stale:",
                link=f"/submissions/{sub.pk}/",
                created_at__gte=timezone.now() - timedelta(days=7),
            ).exists()
            if not recent:
                create_notification(
                    sub.submitted_by,
                    kind=Notification.Kind.SUBMISSION,
                    title=f"Stale: {sub.consultant.user.get_full_name()} → {sub.job.title}",
                    body=f"No updates in {(timezone.now() - sub.updated_at).days} days. Consider following up.",
                    link=f"/submissions/{sub.pk}/",
                )
                created += 1

    return {"notifications_created": created}

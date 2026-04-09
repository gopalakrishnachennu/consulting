"""In-app + optional email when a new message is posted."""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from core.models import Notification
from core.notification_utils import create_notification, _settings_allowed_origin
from users.models import UserEmailNotificationPreferences

from .utils import invalidate_messaging_unread_cache

logger = logging.getLogger(__name__)


def notify_recipients_new_message(message):
    """Notify other participants (bell + optional email)."""
    from users.models import User

    thread = message.thread
    link = reverse("thread-detail", kwargs={"pk": thread.pk})
    sender = message.sender
    preview = (message.content or "").strip()[:200]
    if message.attachment and not preview:
        preview = "Attachment"

    for recipient in thread.participants.exclude(pk=sender.pk):
        if not isinstance(recipient, User):
            continue
        create_notification(
            recipient,
            kind=Notification.Kind.MESSAGE,
            title=f"New message from {sender.get_full_name() or sender.username}",
            body=preview,
            link=link,
            dedupe_key=f"msg-{message.pk}",
        )
        invalidate_messaging_unread_cache(recipient.pk)

        prefs, _ = UserEmailNotificationPreferences.objects.get_or_create(user=recipient)
        if not prefs.email_system:
            continue
        if not getattr(recipient, "email", None):
            continue
        try:
            send_mail(
                subject=f"New message from {sender.get_full_name() or sender.username}",
                message=f"{preview}\n\nOpen: {_settings_allowed_origin()}{link}",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@localhost",
                recipient_list=[recipient.email],
                fail_silently=True,
            )
        except Exception as exc:
            logger.debug("notify_recipients_new_message email failed: %s", exc)

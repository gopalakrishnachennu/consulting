"""Create in-app notifications, optional email, and enterprise safeguards (Phase 3+)."""

import logging
import re
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import IntegrityError
from django.urls import reverse

from .models import Notification

logger = logging.getLogger(__name__)

# Short-lived cache for nav unread badge (invalidated on read/create).
UNREAD_CACHE_SECONDS = 45
UNREAD_CACHE_KEY = "notif_unread:{user_id}"


def invalidate_notification_unread_cache(user_id: int) -> None:
    cache.delete(UNREAD_CACHE_KEY.format(user_id=user_id))


def get_cached_unread_count(user_id: int) -> int:
    key = UNREAD_CACHE_KEY.format(user_id=user_id)
    n = cache.get(key)
    if n is not None:
        return int(n)
    n = Notification.objects.filter(user_id=user_id, read_at__isnull=True).count()
    cache.set(key, n, UNREAD_CACHE_SECONDS)
    return n


def _inapp_allowed_for_kind(user, kind: str) -> bool:
    from users.models import UserEmailNotificationPreferences

    prefs, _ = UserEmailNotificationPreferences.objects.get_or_create(user=user)
    mapping = {
        Notification.Kind.SUBMISSION: prefs.inapp_submissions,
        Notification.Kind.INTERVIEW: prefs.inapp_interviews,
        Notification.Kind.JOB: prefs.inapp_jobs,
        Notification.Kind.SYSTEM: prefs.inapp_system,
        Notification.Kind.MESSAGE: prefs.inapp_system,
    }
    return mapping.get(kind, True)


def sanitize_internal_link(link: str) -> str:
    """
    Only allow same-origin relative paths. Blocks open redirects and javascript: URLs.
    """
    if not link:
        return ""
    link = (link or "").strip()
    if not link.startswith("/"):
        return ""
    if link.startswith("//"):
        return ""
    lowered = link.lower()
    if lowered.startswith("/\\") or "://" in link[:20]:
        return ""
    if re.match(r"^/[^\s]*javascript:", lowered):
        return ""
    return link[:500]


def create_notification(
    user,
    *,
    kind: str,
    title: str,
    body: str = "",
    link: str = "",
    dedupe_key: Optional[str] = None,
) -> Optional[Notification]:
    """
    Persist an in-app notification. Respects per-user in-app category toggles.
    If dedupe_key is set and a row already exists for (user, dedupe_key), returns that row (no duplicate).
    """
    if not _inapp_allowed_for_kind(user, kind):
        return None

    safe_link = sanitize_internal_link(link)
    title = (title or "")[:200]
    body = body or ""
    dk = (dedupe_key or "").strip()[:64] or None

    if dk:
        existing = Notification.objects.filter(user=user, dedupe_key=dk).first()
        if existing:
            return existing

    try:
        n = Notification.objects.create(
            user=user,
            kind=kind,
            title=title,
            body=body,
            link=safe_link,
            dedupe_key=dk,
        )
    except IntegrityError:
        if dk:
            return Notification.objects.filter(user=user, dedupe_key=dk).first()
        raise

    invalidate_notification_unread_cache(user.pk)
    return n


def notify_submission_pipeline_event(
    submission,
    *,
    actor,
    old_status: str,
    new_status: str,
):
    """
    Notify the consultant when staff moves their application on the pipeline.
    Skips if the actor is the consultant themselves.
    """
    consultant_user = submission.consultant.user
    if actor and consultant_user.pk == actor.pk:
        return

    path = reverse("submission-detail", kwargs={"pk": submission.pk})
    title = f"Application updated: {submission.get_status_display()}"
    body = f"{submission.job.title} · {submission.job.company}"
    create_notification(
        consultant_user,
        kind=Notification.Kind.SUBMISSION,
        title=title,
        body=body,
        link=path,
    )

    from users.models import UserEmailNotificationPreferences

    prefs, _ = UserEmailNotificationPreferences.objects.get_or_create(user=consultant_user)
    if not prefs.email_submissions:
        return
    if not getattr(consultant_user, "email", None):
        return
    try:
        send_mail(
            subject=title,
            message=f"{body}\n\nView: {_settings_allowed_origin()}{path}",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@localhost",
            recipient_list=[consultant_user.email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.debug("notify_submission_pipeline_event email failed: %s", exc)


def _settings_allowed_origin():
    """Best-effort base URL for emails (no request context)."""
    return getattr(settings, "SITE_URL", "") or ""


def notify_job_email_optional(user, *, subject: str, body: str, link_path: str) -> None:
    """Respects UserEmailNotificationPreferences.email_jobs."""
    from users.models import UserEmailNotificationPreferences

    prefs, _ = UserEmailNotificationPreferences.objects.get_or_create(user=user)
    if not prefs.email_jobs:
        return
    if not getattr(user, "email", None):
        return
    base = _settings_allowed_origin()
    try:
        send_mail(
            subject=(subject or "")[:200],
            message=f"{body}\n\nView: {base}{link_path}",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None) or "noreply@localhost",
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.debug("notify_job_email_optional failed: %s", exc)

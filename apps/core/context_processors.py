"""Platform singleton for templates + unread notification count for nav bell."""

from core.notification_utils import get_cached_unread_count


def platform_settings(request):
    """Expose singleton PlatformConfig as PLATFORM_CONFIG (base.html, etc.)."""
    from core.models import PlatformConfig

    return {'PLATFORM_CONFIG': PlatformConfig.load()}


def unread_notifications_count(request):
    if not request.user.is_authenticated:
        return {'unread_notification_count': 0}
    n = get_cached_unread_count(request.user.pk)
    return {'unread_notification_count': n}


def pending_pool_count(request):
    """
    Inject pending_pool_count for admin/employee nav badge.
    Only runs the DB query for logged-in staff users.
    """
    if not request.user.is_authenticated:
        return {'pending_pool_count': 0}
    role = getattr(request.user, 'role', None)
    if not (request.user.is_superuser or role in ('ADMIN', 'EMPLOYEE')):
        return {'pending_pool_count': 0}
    try:
        from jobs.models import Job
        count = Job.objects.filter(status=Job.Status.POOL, is_archived=False).count()
    except Exception:
        count = 0
    return {'pending_pool_count': count}

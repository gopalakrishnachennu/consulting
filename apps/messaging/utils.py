"""Caching, access control, and rate limiting for messaging."""

from django.core.cache import cache
from django.db.models import Count, Exists, OuterRef, Q, Subquery

from users.models import User

from .models import Message, Thread

MESSAGING_UNREAD_CACHE_KEY = "messaging_unread:{user_id}"
MESSAGING_UNREAD_CACHE_SECONDS = 45
SEARCH_RATE_PREFIX = "messaging_search_rl"


def invalidate_messaging_unread_cache(user_id: int) -> None:
    cache.delete(MESSAGING_UNREAD_CACHE_KEY.format(user_id=user_id))


def get_cached_messaging_unread_count(user_id: int) -> int:
    key = MESSAGING_UNREAD_CACHE_KEY.format(user_id=user_id)
    n = cache.get(key)
    if n is not None:
        return int(n)
    n = (
        Message.objects.filter(
            thread__participants__pk=user_id,
            is_read=False,
        )
        .exclude(sender_id=user_id)
        .count()
    )
    cache.set(key, n, MESSAGING_UNREAD_CACHE_SECONDS)
    return n


def is_staff_user(user: User) -> bool:
    return user.is_superuser or user.role in (User.Role.ADMIN, User.Role.EMPLOYEE)


def users_may_message_each_other(a: User, b: User) -> bool:
    if a.pk == b.pk:
        return False
    if a.organisation_id and b.organisation_id and a.organisation_id != b.organisation_id:
        return False
    return True


def user_can_access_thread(user: User, thread: Thread) -> bool:
    if thread.participants.filter(pk=user.pk).exists():
        return True
    if thread.thread_type == Thread.ThreadType.ORG_SHARED and thread.organisation_id:
        if user.is_superuser:
            return True
        if is_staff_user(user) and user.organisation_id == thread.organisation_id:
            return True
    return False


def ensure_thread_participant(thread: Thread, user: User) -> None:
    if not thread.participants.filter(pk=user.pk).exists():
        thread.participants.add(user)


def inbox_threads_base_queryset(user: User):
    """Threads visible in the inbox for this user."""
    last_msg = (
        Message.objects.filter(thread=OuterRef("pk"), deleted_at__isnull=True)
        .order_by("-created_at")
    )
    uid = user.id
    if is_staff_user(user):
        q = Q(participants=user)
        if user.organisation_id:
            q |= Q(
                thread_type=Thread.ThreadType.ORG_SHARED,
                organisation_id=user.organisation_id,
            )
        qs = Thread.objects.filter(q).distinct()
    else:
        qs = user.threads.all()
    return (
        qs.annotate(
            unread_count=Count(
                "messages",
                filter=Q(messages__is_read=False) & ~Q(messages__sender_id=uid),
            ),
            last_snippet=Subquery(last_msg.values("content")[:1]),
            last_msg_at=Subquery(last_msg.values("created_at")[:1]),
        )
        .order_by("-updated_at")
        .prefetch_related("participants")
    )


def filter_inbox_queryset(qs, list_q: str):
    if not (list_q or "").strip():
        return qs
    q = list_q.strip()
    return qs.filter(
        Q(participants__username__icontains=q)
        | Q(participants__first_name__icontains=q)
        | Q(participants__last_name__icontains=q)
        | Q(participants__email__icontains=q)
        | Exists(
            Message.objects.filter(
                thread_id=OuterRef("pk"),
                deleted_at__isnull=True,
                content__icontains=q,
            )
        )
    ).distinct()


def messaging_search_rate_ok(user_id: int, ip: str) -> bool:
    """Max ~90 searches per minute per user+IP (sliding window via cache incr)."""
    key = f"{SEARCH_RATE_PREFIX}:{user_id}:{ip}"
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 60)
        return True
    return n <= 90

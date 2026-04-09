"""Unread inbound message count for nav badge (cached)."""

from messaging.utils import get_cached_messaging_unread_count


def unread_messages_count(request):
    if not request.user.is_authenticated:
        return {"unread_message_count": 0}
    n = get_cached_messaging_unread_count(request.user.pk)
    return {"unread_message_count": n}

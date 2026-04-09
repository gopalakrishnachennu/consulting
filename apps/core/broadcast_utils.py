"""Fan-out admin broadcasts and record per-user audit rows."""

import logging
from django.db import transaction, IntegrityError
from django.db.models import Q

from users.models import User

from .models import BroadcastDelivery, BroadcastMessage, Notification
from .notification_utils import create_notification, sanitize_internal_link

logger = logging.getLogger(__name__)


def _recipient_queryset(message: BroadcastMessage):
    qs = User.objects.filter(is_active=True)
    if message.organisation_id:
        qs = qs.filter(organisation_id=message.organisation_id)

    aud = message.audience
    if aud == BroadcastMessage.Audience.EMPLOYEES_ONLY:
        qs = qs.filter(role=User.Role.EMPLOYEE)
    elif aud == BroadcastMessage.Audience.CONSULTANTS:
        qs = qs.filter(role=User.Role.CONSULTANT)
    elif aud == BroadcastMessage.Audience.EMPLOYEES_AND_CONSULTANTS:
        qs = qs.filter(role__in=[User.Role.EMPLOYEE, User.Role.CONSULTANT])
    elif aud == BroadcastMessage.Audience.STAFF:
        qs = qs.filter(Q(role__in=[User.Role.ADMIN, User.Role.EMPLOYEE]) | Q(is_superuser=True))
    elif aud == BroadcastMessage.Audience.ADMINS:
        qs = qs.filter(Q(role=User.Role.ADMIN) | Q(is_superuser=True))
    # ALL_ACTIVE: no extra filter
    return qs.order_by('id')


@transaction.atomic
def deliver_broadcast(message: BroadcastMessage) -> dict:
    """
    Create in-app notifications for all matching users and BroadcastDelivery rows.
    Returns counts: delivered, skipped_inapp.
    """
    link = sanitize_internal_link(message.link or '')
    delivered = 0
    skipped = 0

    for user in _recipient_queryset(message).iterator(chunk_size=500):
        n = create_notification(
            user,
            kind=message.kind,
            title=message.title,
            body=message.body or '',
            link=link,
            dedupe_key=f"broadcast:{message.pk}:{user.pk}",
        )
        try:
            if n is None:
                BroadcastDelivery.objects.create(
                    broadcast=message,
                    user=user,
                    notification=None,
                    status=BroadcastDelivery.Status.SKIPPED_INAPP,
                )
                skipped += 1
            else:
                BroadcastDelivery.objects.create(
                    broadcast=message,
                    user=user,
                    notification=n,
                    status=BroadcastDelivery.Status.DELIVERED,
                )
                delivered += 1
        except IntegrityError:
            logger.debug("broadcast delivery duplicate skipped user=%s broadcast=%s", user.pk, message.pk)

    return {'delivered': delivered, 'skipped_inapp': skipped}

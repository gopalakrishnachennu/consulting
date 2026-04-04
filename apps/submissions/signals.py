"""
Phase 1 signals: auto-actions on submission status changes.

- When a submission changes to PLACED → update consultant status to PLACED
- When a placement status changes to COMPLETED/TERMINATED → revert consultant to ACTIVE
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ApplicationSubmission, Placement
from users.models import ConsultantProfile


@receiver(post_save, sender=ApplicationSubmission)
def on_submission_status_change(sender, instance, **kwargs):
    """Auto-update consultant profile status based on submission status."""
    if instance.status == ApplicationSubmission.Status.PLACED:
        # Set consultant status to PLACED
        consultant = instance.consultant
        if consultant.status != ConsultantProfile.Status.PLACED:
            consultant.status = ConsultantProfile.Status.PLACED
            consultant.save(update_fields=['status'])


@receiver(post_save, sender=Placement)
def on_placement_status_change(sender, instance, **kwargs):
    """When a placement is completed or terminated, revert consultant to ACTIVE."""
    if instance.status in (
        Placement.PlacementStatus.COMPLETED,
        Placement.PlacementStatus.TERMINATED,
    ):
        consultant = instance.submission.consultant
        # Only revert if they don't have another active placement
        has_other_active = Placement.objects.filter(
            submission__consultant=consultant,
            status=Placement.PlacementStatus.ACTIVE,
        ).exclude(pk=instance.pk).exists()

        if not has_other_active and consultant.status == ConsultantProfile.Status.PLACED:
            consultant.status = ConsultantProfile.Status.ACTIVE
            consultant.save(update_fields=['status'])

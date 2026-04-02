from django.db import models
from django.conf import settings
from django.core.validators import FileExtensionValidator
from jobs.models import Job
from resumes.models import Resume
from users.models import ConsultantProfile
from django.utils.translation import gettext_lazy as _
from config.constants.limits import ALLOWED_UPLOAD_EXTENSIONS


class ApplicationSubmission(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = 'IN_PROGRESS', _('In Progress')
        APPLIED = 'APPLIED', _('Applied')
        INTERVIEW = 'INTERVIEW', _('Interview Scheduled')
        OFFER = 'OFFER', _('Offer Received')
        REJECTED = 'REJECTED', _('Rejected')
        WITHDRAWN = 'WITHDRAWN', _('Withdrawn')

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='submissions')
    consultant = models.ForeignKey(ConsultantProfile, on_delete=models.CASCADE, related_name='submissions')
    resume = models.ForeignKey(Resume, on_delete=models.SET_NULL, null=True, blank=True, related_name='submissions')
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.APPLIED
    )
    
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='submitted_applications'
    )
    
    proof_file = models.FileField(
        upload_to='submission_proofs/',
        blank=True,
        null=True,
        help_text="Upload screenshot, image, or PDF confirmation",
        validators=[FileExtensionValidator(allowed_extensions=ALLOWED_UPLOAD_EXTENSIONS)],
    )
    notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(blank=True, null=True, help_text="When proof of submission was uploaded")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.consultant.user.username} applied to {self.job.title}"
    
    class Meta:
        ordering = ['-created_at']
        unique_together = ('job', 'consultant')  # Prevent double applications? Maybe not if re-applying later.


class SubmissionStatusHistory(models.Model):
    """One record per status change for application timeline."""
    submission = models.ForeignKey(
        ApplicationSubmission, on_delete=models.CASCADE, related_name='status_history'
    )
    from_status = models.CharField(max_length=20, blank=True, null=True)
    to_status = models.CharField(max_length=20)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.submission_id}: {self.from_status or '?'} → {self.to_status}"


def record_submission_status_change(submission, to_status, from_status=None, note=''):
    """Record a status change for the application timeline."""
    SubmissionStatusHistory.objects.create(
        submission=submission,
        from_status=from_status or '',
        to_status=to_status,
        note=note[:500] if note else '',
    )


class Offer(models.Model):
    """Offer and negotiation tracking for a submission (when status=OFFER)."""
    submission = models.OneToOneField(
        ApplicationSubmission, on_delete=models.CASCADE, related_name='offer_detail'
    )
    initial_salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    initial_currency = models.CharField(max_length=10, default='USD', blank=True)
    initial_notes = models.TextField(blank=True)
    final_salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    final_currency = models.CharField(max_length=10, default='USD', blank=True)
    final_terms = models.TextField(blank=True, help_text="Final accepted terms summary")
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Offer for {self.submission}"


class OfferRound(models.Model):
    """One negotiation round (proposed salary/terms at a point in time)."""
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name='rounds')
    round_number = models.PositiveSmallIntegerField(default=1)
    salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=10, default='USD', blank=True)
    bonus_notes = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    proposed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['round_number']

    def __str__(self):
        return f"Round {self.round_number} for {self.offer}"


class SubmissionResponse(models.Model):
    class ResponseType(models.TextChoices):
        EMAIL = 'EMAIL', _('Email')
        CALL = 'CALL', _('Call')
        PORTAL = 'PORTAL', _('Portal')
        OTHER = 'OTHER', _('Other')

    class Status(models.TextChoices):
        RECEIVED = 'RECEIVED', _('Received')
        FOLLOW_UP = 'FOLLOW_UP', _('Follow Up')
        CLOSED = 'CLOSED', _('Closed')

    submission = models.ForeignKey(
        ApplicationSubmission, on_delete=models.CASCADE, related_name='responses'
    )
    response_type = models.CharField(max_length=20, choices=ResponseType.choices, default=ResponseType.EMAIL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    notes = models.TextField(blank=True)
    responded_at = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='submission_responses'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-responded_at']


class EmailEvent(models.Model):
    """
    Normalized log of an inbound email that may relate to a submission.
    Used for IMAP email parsing & auto-status updates.
    """

    class DetectedStatus(models.TextChoices):
        UNKNOWN = 'UNKNOWN', _('Unknown')
        IN_PROGRESS = ApplicationSubmission.Status.IN_PROGRESS
        APPLIED = ApplicationSubmission.Status.APPLIED
        INTERVIEW = ApplicationSubmission.Status.INTERVIEW
        OFFER = ApplicationSubmission.Status.OFFER
        REJECTED = ApplicationSubmission.Status.REJECTED

    class AppliedAction(models.TextChoices):
        NONE = 'none', _('No Action')
        AUTO_UPDATED = 'auto_updated', _('Auto Updated')
        NEEDS_REVIEW = 'needs_review', _('Needs Review')
        MANUAL_UPDATED = 'manual_updated', _('Manually Updated')

    received_at = models.DateTimeField()
    from_address = models.EmailField(max_length=255)
    to_address = models.EmailField(max_length=255)
    subject = models.CharField(max_length=500)
    body_snippet = models.TextField(blank=True)
    raw_message_id = models.CharField(max_length=255, blank=True, db_index=True)

    detected_status = models.CharField(
        max_length=20,
        choices=DetectedStatus.choices,
        default=DetectedStatus.UNKNOWN,
    )
    detected_candidate_name = models.CharField(max_length=255, blank=True)
    detected_company = models.CharField(max_length=255, blank=True)
    detected_job_title = models.CharField(max_length=255, blank=True)
    confidence = models.PositiveSmallIntegerField(default=0)

    matched_submission = models.ForeignKey(
        ApplicationSubmission,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='email_events',
    )
    applied_action = models.CharField(
        max_length=20,
        choices=AppliedAction.choices,
        default=AppliedAction.NONE,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at', '-created_at']

    def __str__(self):
        return f"[{self.received_at}] {self.subject}"

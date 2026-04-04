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
        PLACED = 'PLACED', _('Placed')
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


class Placement(models.Model):
    """
    Created when a submission reaches PLACED status.
    Tracks placement details, billing, and revenue.
    """

    class PlacementType(models.TextChoices):
        PERMANENT = 'PERMANENT', _('Permanent')
        CONTRACT = 'CONTRACT', _('Contract')
        CONTRACT_TO_HIRE = 'CONTRACT_TO_HIRE', _('Contract-to-Hire')
        TEMPORARY = 'TEMPORARY', _('Temporary')

    class PlacementStatus(models.TextChoices):
        ACTIVE = 'ACTIVE', _('Active')
        COMPLETED = 'COMPLETED', _('Completed')
        TERMINATED = 'TERMINATED', _('Terminated Early')
        ON_HOLD = 'ON_HOLD', _('On Hold')

    submission = models.OneToOneField(
        ApplicationSubmission,
        on_delete=models.CASCADE,
        related_name='placement',
    )
    placement_type = models.CharField(
        max_length=20,
        choices=PlacementType.choices,
        default=PlacementType.PERMANENT,
    )
    status = models.CharField(
        max_length=20,
        choices=PlacementStatus.choices,
        default=PlacementStatus.ACTIVE,
    )

    start_date = models.DateField(help_text=_("Placement start date"))
    end_date = models.DateField(null=True, blank=True, help_text=_("Placement end date (for contracts)"))

    # Billing rates
    bill_rate = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_("Hourly bill rate to client"),
    )
    pay_rate = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_("Hourly pay rate to consultant"),
    )
    currency = models.CharField(max_length=10, default='USD')

    # Revenue (for permanent placements)
    fee_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text=_("Placement fee as % of annual salary (permanent placements)"),
    )
    fee_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("Flat placement fee amount"),
    )
    annual_salary = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("Consultant's annual salary (for permanent placements)"),
    )

    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_placements',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Placement: {self.submission.consultant.user.get_full_name()} at {self.submission.job.company}"

    @property
    def spread(self):
        """Hourly margin = bill_rate - pay_rate."""
        if self.bill_rate and self.pay_rate:
            return self.bill_rate - self.pay_rate
        return None

    @property
    def calculated_revenue(self):
        """
        For permanent: fee_amount or (fee_percentage * annual_salary / 100).
        For contract: spread * total approved timesheet hours.
        """
        if self.placement_type == self.PlacementType.PERMANENT:
            if self.fee_amount:
                return self.fee_amount
            if self.fee_percentage and self.annual_salary:
                return (self.fee_percentage * self.annual_salary) / 100
        else:
            # Contract: sum of approved timesheet revenue
            spread = self.spread
            if spread:
                total_hours = sum(
                    ts.hours_worked for ts in self.timesheets.filter(
                        status=Timesheet.TimesheetStatus.APPROVED
                    )
                )
                return spread * total_hours
        return None


class Timesheet(models.Model):
    """
    Weekly timesheet for contract placements.
    """

    class TimesheetStatus(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft')
        SUBMITTED = 'SUBMITTED', _('Submitted')
        APPROVED = 'APPROVED', _('Approved')
        REJECTED = 'REJECTED', _('Rejected')

    placement = models.ForeignKey(
        Placement,
        on_delete=models.CASCADE,
        related_name='timesheets',
    )
    week_ending = models.DateField(help_text=_("Saturday of the work week"))
    hours_worked = models.DecimalField(
        max_digits=5, decimal_places=2,
        help_text=_("Total hours worked this week"),
    )
    status = models.CharField(
        max_length=20,
        choices=TimesheetStatus.choices,
        default=TimesheetStatus.DRAFT,
    )
    overtime_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text=_("Overtime hours (included in hours_worked)"),
    )
    notes = models.TextField(blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_timesheets',
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_timesheets',
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-week_ending']
        unique_together = ('placement', 'week_ending')

    def __str__(self):
        return f"Timesheet: {self.placement.submission.consultant.user.get_full_name()} — {self.week_ending}"

    @property
    def bill_amount(self):
        """Amount billed to client for this week."""
        if self.placement.bill_rate:
            return self.hours_worked * self.placement.bill_rate
        return None

    @property
    def pay_amount(self):
        """Amount paid to consultant for this week."""
        if self.placement.pay_rate:
            return self.hours_worked * self.placement.pay_rate
        return None

    @property
    def margin(self):
        """Margin (profit) for this week."""
        bill = self.bill_amount
        pay = self.pay_amount
        if bill is not None and pay is not None:
            return bill - pay
        return None


class Commission(models.Model):
    """
    Commission tracking for employees who facilitated placements.
    """

    class CommissionStatus(models.TextChoices):
        PENDING = 'PENDING', _('Pending')
        APPROVED = 'APPROVED', _('Approved')
        PAID = 'PAID', _('Paid')
        CANCELLED = 'CANCELLED', _('Cancelled')

    placement = models.ForeignKey(
        Placement,
        on_delete=models.CASCADE,
        related_name='commissions',
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='commissions',
        help_text=_("Employee who earned the commission"),
    )
    commission_rate = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text=_("Commission rate as % of placement revenue"),
    )
    commission_amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text=_("Calculated or overridden commission amount"),
    )
    currency = models.CharField(max_length=10, default='USD')
    status = models.CharField(
        max_length=20,
        choices=CommissionStatus.choices,
        default=CommissionStatus.PENDING,
    )
    paid_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Commission: {self.employee.get_full_name()} — ${self.commission_amount}"


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

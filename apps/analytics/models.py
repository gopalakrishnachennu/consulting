from django.db import models
from django.conf import settings
from django.utils import timezone


class DailySnapshot(models.Model):
    """One row per calendar day — point-in-time platform health metrics."""
    date = models.DateField(unique=True)

    # Jobs pipeline
    jobs_harvested_total = models.PositiveIntegerField(default=0)
    jobs_in_pool = models.PositiveIntegerField(default=0)
    jobs_live = models.PositiveIntegerField(default=0)
    jobs_closed_today = models.PositiveIntegerField(default=0)

    # Submissions funnel
    submissions_total = models.PositiveIntegerField(default=0)
    submissions_applied = models.PositiveIntegerField(default=0)
    submissions_interview = models.PositiveIntegerField(default=0)
    submissions_offer = models.PositiveIntegerField(default=0)
    submissions_placed = models.PositiveIntegerField(default=0)
    submissions_rejected = models.PositiveIntegerField(default=0)

    # Revenue (from active placements)
    active_placements = models.PositiveIntegerField(default=0)
    revenue_mtd = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    avg_bill_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    avg_pay_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    avg_margin_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # Consultants
    consultants_active = models.PositiveIntegerField(default=0)
    consultants_bench = models.PositiveIntegerField(default=0)
    consultants_placed = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']
        verbose_name = "Daily Snapshot"

    def __str__(self):
        return f"Snapshot {self.date}"


class FunnelEvent(models.Model):
    """One event per status transition — feeds conversion rate analytics."""

    class Stage(models.TextChoices):
        RESUME_GENERATED = 'RESUME_GENERATED', 'Resume Generated'
        SUBMITTED = 'SUBMITTED', 'Submitted'
        INTERVIEW = 'INTERVIEW', 'Interview'
        OFFER = 'OFFER', 'Offer'
        PLACED = 'PLACED', 'Placed'
        REJECTED = 'REJECTED', 'Rejected'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'

    stage = models.CharField(max_length=30, choices=Stage.choices)
    consultant = models.ForeignKey(
        'users.ConsultantProfile', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='funnel_events'
    )
    job = models.ForeignKey(
        'jobs.Job', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='funnel_events'
    )
    submission = models.ForeignKey(
        'submissions.ApplicationSubmission', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='funnel_events'
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='funnel_events_acted'
    )
    source = models.CharField(max_length=50, blank=True, help_text="e.g. email_parser, manual, auto_approve")
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-occurred_at']
        verbose_name = "Funnel Event"
        indexes = [
            models.Index(fields=['stage', 'occurred_at']),
            models.Index(fields=['consultant', 'stage']),
        ]

    def __str__(self):
        return f"{self.stage} at {self.occurred_at:%Y-%m-%d}"

    @classmethod
    def record(cls, stage, submission=None, job=None, consultant=None, actor=None, source=''):
        """Convenience method to record a funnel transition."""
        try:
            cls.objects.create(
                stage=stage,
                submission=submission,
                job=job or (submission.job if submission else None),
                consultant=consultant or (submission.consultant if submission else None),
                actor=actor,
                source=source,
                occurred_at=timezone.now(),
            )
        except Exception:
            pass


class RevenueRecord(models.Model):
    """Weekly revenue record per active placement."""
    placement = models.ForeignKey(
        'submissions.Placement', on_delete=models.CASCADE,
        related_name='revenue_records'
    )
    period_start = models.DateField()
    period_end = models.DateField()
    hours_billed = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    bill_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    pay_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    gross_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    margin = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    margin_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_start']
        unique_together = ('placement', 'period_start')
        verbose_name = "Revenue Record"

    def __str__(self):
        return f"Revenue {self.period_start} – {self.period_end}: ${self.gross_revenue}"

    def save(self, *args, **kwargs):
        if self.bill_rate and self.pay_rate and self.hours_billed:
            self.gross_revenue = self.bill_rate * self.hours_billed
            self.margin = (self.bill_rate - self.pay_rate) * self.hours_billed
            if self.gross_revenue:
                self.margin_pct = round((self.margin / self.gross_revenue) * 100, 2)
        super().save(*args, **kwargs)

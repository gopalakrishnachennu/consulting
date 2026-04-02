from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from users.models import MarketingRole
from companies.models import Company

class Job(models.Model):
    class JobType(models.TextChoices):
        FULL_TIME = 'FULL_TIME', _('Full Time')
        PART_TIME = 'PART_TIME', _('Part Time')
        CONTRACT = 'CONTRACT', _('Contract')
        INTERNSHIP = 'INTERNSHIP', _('Internship')

    class Status(models.TextChoices):
        OPEN = 'OPEN', _('Open')
        CLOSED = 'CLOSED', _('Closed')
        DRAFT = 'DRAFT', _('Draft')

    title = models.CharField(max_length=200)
    company = models.CharField(max_length=200, help_text="Legacy company name (will be kept for compatibility).")
    company_obj = models.ForeignKey(
        Company,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
        help_text="Structured company profile backing this job.",
    )
    location = models.CharField(max_length=200, blank=True)
    description = models.TextField()
    original_link = models.URLField(max_length=500, help_text="Link to the original job posting")
    original_link_last_checked_at = models.DateTimeField(null=True, blank=True)
    original_link_is_live = models.BooleanField(
        default=True,
        help_text="Set via background checker. False when the original job URL appears to be gone.",
    )
    possibly_filled = models.BooleanField(
        default=False,
        help_text="Flag set when the job URL starts returning 4xx/410; suggests the role might be filled or closed externally.",
    )
    
    salary_range = models.CharField(max_length=100, blank=True)
    job_type = models.CharField(
        max_length=20,
        choices=JobType.choices,
        default=JobType.FULL_TIME
    )
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN
    )

    marketing_roles = models.ManyToManyField(
        MarketingRole,
        blank=True,
        related_name='jobs'
    )
    
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='posted_jobs'
    )
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='edited_jobs'
    )
    last_edited_at = models.DateTimeField(null=True, blank=True)
    parsed_jd = models.JSONField(default=dict, blank=True)
    parsed_jd_status = models.CharField(max_length=20, blank=True)
    parsed_jd_error = models.TextField(blank=True)
    parsed_jd_updated_at = models.DateTimeField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title
    
    class Meta:
        ordering = ['-created_at']

class JobTemplate(models.Model):
    title = models.CharField(max_length=200, help_text="Template Name")
    description = models.TextField()
    default_marketing_roles = models.ManyToManyField(MarketingRole, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title

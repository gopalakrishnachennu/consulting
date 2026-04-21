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
        POOL = 'POOL', _('In Pool')
        OPEN = 'OPEN', _('Open')
        CLOSED = 'CLOSED', _('Closed')
        DRAFT = 'DRAFT', _('Draft')

    class Stage(models.TextChoices):
        DISCOVERED = 'DISCOVERED', _('Discovered')
        FETCHED    = 'FETCHED',    _('Fetched')
        ENRICHED   = 'ENRICHED',   _('Enriched')
        SCORED     = 'SCORED',     _('Scored')
        VETTED     = 'VETTED',     _('Vetted')
        LIVE       = 'LIVE',       _('Live')
        MATCHED    = 'MATCHED',    _('Matched')
        FILLED     = 'FILLED',     _('Filled')
        ARCHIVED   = 'ARCHIVED',   _('Archived')

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
        default=Status.POOL
    )

    stage = models.CharField(
        max_length=20,
        choices=Stage.choices,
        default=Stage.DISCOVERED,
        db_index=True,
        help_text=_("Unified pipeline stage — supersedes status over time."),
    )
    stage_changed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    url_hash = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text=_("SHA256 of original_link for cross-platform dedupe."),
    )
    quality_score = models.FloatField(
        null=True, blank=True,
        help_text=_("0.0–1.0 fraction of key fields populated."),
    )
    stage_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='owned_jobs',
        help_text=_("Staffer responsible at current stage."),
    )

    # ─── Validation pipeline ───────────────────────────────────────────
    validation_score = models.IntegerField(
        null=True, blank=True,
        help_text="Quality score 0–100 computed by validate_job_quality()"
    )
    validation_result = models.JSONField(
        null=True, blank=True,
        help_text="Full breakdown: issues[], passed[], auto_approved"
    )
    validation_run_at = models.DateTimeField(null=True, blank=True)
    validated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='validated_jobs',
    )
    rejection_reason = models.TextField(blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='rejected_jobs',
    )
    rejected_at = models.DateTimeField(null=True, blank=True)

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
    
    # Phase 5: Job source tracking
    job_source = models.CharField(
        max_length=100, blank=True,
        help_text=_("Where this job was found (e.g. LinkedIn, Indeed, Referral, Website)"),
    )

    # Phase 5: Soft-delete
    is_archived = models.BooleanField(default=False, help_text=_("Soft-deleted / archived"))
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='archived_jobs',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # Auto-sync legacy company name from company_obj FK
        if self.company_obj_id and self.company_obj:
            self.company = self.company_obj.name
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    class Meta:
        ordering = ['-created_at']

class JobTemplate(models.Model):
    title = models.CharField(max_length=200, help_text="Template Name")
    description = models.TextField()
    default_marketing_roles = models.ManyToManyField(MarketingRole, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class JobEmbedding(models.Model):
    """Stores the OpenAI embedding vector for a job (for semantic matching)."""
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name='embedding')
    vector = models.JSONField(help_text="Embedding float list from text-embedding-3-small")
    model = models.CharField(max_length=80, default='text-embedding-3-small')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Job Embedding"

    def __str__(self):
        return f"Embedding for {self.job_id}"


class MatchScore(models.Model):
    """Pre-computed cosine similarity between a job and a consultant profile."""
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='match_scores')
    consultant = models.ForeignKey(
        'users.ConsultantProfile', on_delete=models.CASCADE, related_name='match_scores'
    )
    score = models.FloatField(help_text="Cosine similarity 0.0–1.0")
    rank = models.PositiveSmallIntegerField(default=0, help_text="Rank among all consultants for this job (1 = best)")
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('job', 'consultant')
        ordering = ['rank']
        verbose_name = "Match Score"

    def __str__(self):
        return f"Job {self.job_id} ↔ Consultant {self.consultant_id}: {self.score:.3f}"

    @property
    def score_pct(self):
        return int(self.score * 100)

    def __str__(self):
        return self.title


class PipelineEvent(models.Model):
    """Single source of truth for job lifecycle transitions.

    Replaces HarvestRun + FetchBatch + CompanyFetchRun. Every stage transition,
    every task run, every failure — one row each. Gives full lineage per job
    in one query: Job.pipeline_events.all().
    """

    class Status(models.TextChoices):
        SUCCESS = 'SUCCESS', _('Success')
        FAILED  = 'FAILED',  _('Failed')
        SKIPPED = 'SKIPPED', _('Skipped')
        RUNNING = 'RUNNING', _('Running')

    job = models.ForeignKey(
        Job, on_delete=models.CASCADE, related_name='pipeline_events',
        null=True, blank=True,
        help_text=_("Nullable: pre-FETCHED events (discovery) may not have a Job yet."),
    )
    url_hash = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text=_("Allows event logging before Job row exists."),
    )
    from_stage = models.CharField(max_length=20, blank=True)
    to_stage   = models.CharField(max_length=20, blank=True)
    task_name  = models.CharField(max_length=120, blank=True)
    celery_id  = models.CharField(max_length=80, blank=True, db_index=True)
    status     = models.CharField(max_length=10, choices=Status.choices, default=Status.SUCCESS)
    error      = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True, help_text=_("Task-specific payload."))
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['job', '-occurred_at']),
            models.Index(fields=['to_stage', '-occurred_at']),
            models.Index(fields=['status', '-occurred_at']),
        ]
        verbose_name = "Pipeline Event"

    def __str__(self):
        return f"{self.task_name or 'event'} {self.from_stage}→{self.to_stage} [{self.status}]"

    @classmethod
    def record(cls, *, job=None, url_hash='', from_stage='', to_stage='',
               task_name='', celery_id='', status='SUCCESS', error='',
               duration_ms=None, meta=None):
        return cls.objects.create(
            job=job,
            url_hash=url_hash or (getattr(job, 'url_hash', '') if job else ''),
            from_stage=from_stage,
            to_stage=to_stage,
            task_name=task_name,
            celery_id=celery_id,
            status=status,
            error=error,
            duration_ms=duration_ms,
            meta=meta or {},
        )

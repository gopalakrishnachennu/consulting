from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from users.models import MarketingRole
from companies.models import Company

class Job(models.Model):
    class LinkHealthState(models.TextChoices):
        LIVE = "LIVE", _("Live")
        INCONCLUSIVE = "INCONCLUSIVE", _("Inconclusive")
        DEAD = "DEAD", _("Dead")

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

    class GateStatus(models.TextChoices):
        ELIGIBLE = 'ELIGIBLE', _('Eligible')
        REVIEW = 'REVIEW', _('Needs Review')
        BLOCKED = 'BLOCKED', _('Blocked')

    class VetLane(models.TextChoices):
        AUTO = 'AUTO', _('Auto-Approve Lane')
        HUMAN = 'HUMAN', _('Human Review Lane')
        BLOCKED = 'BLOCKED', _('Blocked Lane')

    class RoutingStatus(models.TextChoices):
        PENDING = 'PENDING', _('Pending')
        READY = 'READY', _('Ready')
        REVIEW = 'REVIEW', _('Needs Review')
        FAILED = 'FAILED', _('Failed')
        OVERRIDDEN = 'OVERRIDDEN', _('Overridden')

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
    original_link_health = models.CharField(
        max_length=16,
        choices=LinkHealthState.choices,
        default=LinkHealthState.LIVE,
        db_index=True,
        help_text="Tri-state source link health: LIVE, INCONCLUSIVE, or DEAD.",
    )
    original_link_reason = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="Last link-health reason code from the checker.",
    )
    original_link_status_code = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Last HTTP status code observed during the link-health check.",
    )
    original_link_final_url = models.URLField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Resolved URL after redirects during the last link-health check.",
    )
    possibly_filled = models.BooleanField(
        default=False,
        help_text="Flag set when the job URL starts returning 4xx/410; suggests the role might be filled or closed externally.",
    )
    
    salary_range = models.CharField(max_length=100, blank=True)
    job_type = models.CharField(
        max_length=20,
        choices=JobType.choices,
        default=JobType.FULL_TIME,
        db_index=True,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.POOL,
        db_index=True,
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
        max_length=64, blank=True, default='', db_index=True,
        help_text=_("SHA256 of original_link for cross-platform dedupe. Unique when non-empty."),
    )
    quality_score = models.FloatField(
        null=True, blank=True,
        help_text=_("0.0–1.0 fraction of key fields populated."),
    )
    # Vet gate + triage signals (Harvest -> Vet workflow).
    source_raw_job = models.ForeignKey(
        "harvest.RawJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="synced_jobs",
        help_text=_("Raw harvest source row used to create this pooled job."),
    )
    hard_gate_passed = models.BooleanField(default=False, db_index=True)
    gate_status = models.CharField(
        max_length=12,
        choices=GateStatus.choices,
        default=GateStatus.REVIEW,
        db_index=True,
    )
    vet_lane = models.CharField(
        max_length=10,
        choices=VetLane.choices,
        default=VetLane.HUMAN,
        db_index=True,
    )
    pipeline_reason_code = models.CharField(max_length=64, blank=True, db_index=True)
    pipeline_reason_detail = models.TextField(blank=True)
    hard_gate_failures = models.JSONField(default=list, blank=True)
    hard_gate_checks = models.JSONField(default=dict, blank=True)
    data_quality_score = models.FloatField(null=True, blank=True)
    trust_score = models.FloatField(null=True, blank=True)
    candidate_fit_score = models.FloatField(null=True, blank=True)
    vet_priority_score = models.FloatField(null=True, blank=True, db_index=True)
    gate_checked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    queue_entered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    vet_approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
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
    primary_marketing_role = models.ForeignKey(
        MarketingRole,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='primary_jobs',
        help_text=_("Single downstream marketing role used for consultant routing and resume targeting."),
    )
    primary_marketing_role_source = models.CharField(
        max_length=24,
        blank=True,
        default="",
        help_text=_("How the primary role was chosen: auto, approved_snapshot, manual_override."),
    )
    primary_marketing_role_locked = models.BooleanField(
        default=False,
        help_text=_("When enabled, automatic refreshes cannot overwrite the primary marketing role."),
    )
    primary_marketing_role_updated_at = models.DateTimeField(null=True, blank=True)
    auto_marketing_role_slugs = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Auto-assigned MarketingRole slugs from harvested domain routing. Manual role edits stay in the M2M."),
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
    # JD Extraction Engine (V4) cache/version metadata — content-hash + versions so a
    # cached parse is reused only when the JD text and extractor versions all match.
    parsed_jd_hash = models.CharField(max_length=64, blank=True, db_index=True)
    parsed_jd_model = models.CharField(max_length=100, blank=True)
    parsed_jd_prompt_version = models.CharField(max_length=40, blank=True)
    parsed_jd_schema_version = models.CharField(max_length=40, blank=True)
    routing_profile = models.JSONField(default=dict, blank=True)
    routing_status = models.CharField(
        max_length=12,
        choices=RoutingStatus.choices,
        default=RoutingStatus.PENDING,
        db_index=True,
    )
    routing_confidence = models.FloatField(null=True, blank=True)
    routing_source = models.CharField(max_length=32, blank=True)
    routing_role_family = models.CharField(max_length=80, blank=True, db_index=True)
    routing_seniority = models.CharField(max_length=20, blank=True, db_index=True)
    routing_years_min = models.PositiveSmallIntegerField(null=True, blank=True)
    routing_years_max = models.PositiveSmallIntegerField(null=True, blank=True)
    routing_country_mode = models.CharField(max_length=20, blank=True, db_index=True)
    routing_country_codes = models.JSONField(default=list, blank=True)
    routing_work_mode = models.CharField(max_length=20, blank=True)
    routing_visa_sponsorship = models.BooleanField(null=True, blank=True)
    routing_work_authorization = models.CharField(max_length=160, blank=True)
    routing_work_auth_category = models.CharField(max_length=40, blank=True, db_index=True)
    routing_employment_terms = models.JSONField(default=list, blank=True)
    routing_clearance_required = models.BooleanField(default=False)
    routing_warnings = models.JSONField(default=list, blank=True)
    routing_hash = models.CharField(max_length=64, blank=True, db_index=True)
    routing_model = models.CharField(max_length=100, blank=True)
    routing_prompt_version = models.CharField(max_length=40, blank=True)
    routing_schema_version = models.CharField(max_length=40, blank=True)
    routing_extracted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    routing_override = models.JSONField(default=dict, blank=True)
    routing_override_updated_at = models.DateTimeField(null=True, blank=True)

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

    # ── Classification (country + department engine) ──────────────────────────
    class Department(models.TextChoices):
        SOFTWARE_DEV     = "software_dev",     "Software Development"
        DATA_ANALYTICS   = "data_analytics",   "Data & Analytics"
        DEVOPS_CLOUD     = "devops_cloud",      "DevOps & Cloud"
        SECURITY         = "security",          "Security & Cybersecurity"
        IT_SUPPORT       = "it_support",        "IT Support & Help Desk"
        QA_TESTING       = "qa_testing",        "QA & Testing"
        SYSTEMS_NETWORK  = "systems_network",   "Systems & Network"
        IT_MANAGEMENT    = "it_management",     "IT Management & Architecture"
        HEALTHCARE_IT    = "healthcare_it",     "Healthcare IT"
        MANAGEMENT       = "management",        "Management & C-Suite"
        SALES            = "sales",             "Sales & Business Development"
        MARKETING        = "marketing",         "Marketing & Communications"
        HR               = "hr",               "Human Resources"
        FINANCE          = "finance",           "Finance & Accounting"
        OPERATIONS       = "operations",        "Operations & Logistics"
        LEGAL            = "legal",             "Legal & Compliance"
        CUSTOMER_SUCCESS = "customer_success",  "Customer Success"
        DESIGN           = "design",            "Design & Creative"
        ADMIN            = "admin",             "Administrative"
        CIVIL_ENG        = "civil_eng",         "Civil & Construction"
        HEALTHCARE       = "healthcare",        "Healthcare & Clinical"
        OTHER            = "other",             "Other"

    country                = models.CharField(max_length=100, blank=True, db_index=True)
    region                 = models.CharField(max_length=50, blank=True,
                               help_text="APAC/EMEA/LATAM/Worldwide for multi-country roles")
    department             = models.CharField(max_length=20, choices=Department.choices,
                               blank=True, db_index=True)
    department_confidence  = models.FloatField(default=0.0)
    department_source      = models.CharField(max_length=20, blank=True,
                               help_text="rules|embedding|llm|synced|manual")
    classified_at          = models.DateTimeField(null=True, blank=True, db_index=True)
    needs_reclassification = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        # Auto-sync legacy company name from company_obj FK
        if self.company_obj_id and self.company_obj:
            self.company = self.company_obj.name
        # Flag stale classification when title or location changes
        if self.pk:
            changed = Job.objects.filter(pk=self.pk).values("title", "location", "description").first()
            if changed and (
                changed["title"] != self.title
                or changed["location"] != self.location
                or changed["description"] != self.description
            ):
                self.needs_reclassification = True
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    @property
    def is_recent(self):
        """True if created within the last 3 days."""
        from django.utils import timezone
        from datetime import timedelta
        if not self.created_at:
            return False
        return self.created_at >= timezone.now() - timedelta(days=3)

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


class RawJobClassifierRun(models.Model):
    """Immutable execution record for one classifier provider against one RawJob."""

    class Provider(models.TextChoices):
        BACKEND_RULES = "backend_rules", _("Backend Rules")
        CODEX = "codex", _("Codex")
        CLAUDE = "claude", _("Claude")
        SECONDARY_STUB = "secondary_stub", _("Secondary Stub")

    class ProviderRole(models.TextChoices):
        PRIMARY = "PRIMARY", _("Primary")
        SECONDARY = "SECONDARY", _("Secondary")

    class Status(models.TextChoices):
        QUEUED = "QUEUED", _("Queued")
        RUNNING = "RUNNING", _("Running")
        COMPLETED = "COMPLETED", _("Completed")
        FAILED = "FAILED", _("Failed")
        SKIPPED = "SKIPPED", _("Skipped")

    raw_job = models.ForeignKey(
        "harvest.RawJob",
        on_delete=models.CASCADE,
        related_name="classifier_runs",
    )
    provider = models.CharField(max_length=32, choices=Provider.choices, db_index=True)
    provider_role = models.CharField(
        max_length=12,
        choices=ProviderRole.choices,
        default=ProviderRole.PRIMARY,
        db_index=True,
    )
    input_hash = models.CharField(max_length=64, db_index=True)
    prompt_version = models.CharField(max_length=40, blank=True)
    provider_version = models.CharField(max_length=40, blank=True)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    confidence = models.FloatField(null=True, blank=True)
    raw_output = models.JSONField(default=dict, blank=True)
    normalized_output = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["raw_job", "provider", "-created_at"]),
            models.Index(fields=["raw_job", "provider_role", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["input_hash", "provider", "-created_at"]),
        ]

    def __str__(self):
        return f"RawJob {self.raw_job_id} · {self.provider} · {self.status}"


class RawJobClassificationSnapshot(models.Model):
    """Current merged classification state for a RawJob in shadow mode."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        PARTIAL = "PARTIAL", _("Partial")
        MERGED = "MERGED", _("Merged")
        NEEDS_REVIEW = "NEEDS_REVIEW", _("Needs Review")
        FAILED = "FAILED", _("Failed")

    class ApprovalState(models.TextChoices):
        UNREVIEWED = "UNREVIEWED", _("Unreviewed")
        APPROVED = "APPROVED", _("Approved")
        OVERRIDDEN = "OVERRIDDEN", _("Overridden")

    raw_job = models.OneToOneField(
        "harvest.RawJob",
        on_delete=models.CASCADE,
        related_name="classification_snapshot",
    )
    current_input_hash = models.CharField(max_length=64, blank=True, db_index=True)
    backend_run = models.ForeignKey(
        RawJobClassifierRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    secondary_run = models.ForeignKey(
        RawJobClassifierRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    merged_output = models.JSONField(default=dict, blank=True)
    verifier_summary = models.JSONField(default=dict, blank=True)
    final_confidence = models.FloatField(default=0.0)
    needs_review = models.BooleanField(default=False, db_index=True)
    review_reason = models.CharField(max_length=120, blank=True)
    approval_state = models.CharField(
        max_length=16,
        choices=ApprovalState.choices,
        default=ApprovalState.UNREVIEWED,
        db_index=True,
    )
    approval_input_hash = models.CharField(max_length=64, blank=True, db_index=True)
    approval_is_stale = models.BooleanField(default=False, db_index=True)
    approval_stale_at = models.DateTimeField(null=True, blank=True, db_index=True)
    approved_output = models.JSONField(default=dict, blank=True)
    approved_source = models.CharField(max_length=16, blank=True)
    approved_primary_role_slug = models.CharField(max_length=120, blank=True, db_index=True)
    primary_role_source = models.CharField(max_length=24, blank=True, default="")
    primary_role_locked = models.BooleanField(default=False)
    primary_role_override_reason = models.TextField(blank=True)
    primary_role_overridden_at = models.DateTimeField(null=True, blank=True)
    primary_role_overridden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="overridden_rawjob_primary_roles",
    )
    approval_note = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_rawjob_classifications",
    )
    pushed_job = models.ForeignKey(
        "Job",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rawjob_classification_pushes",
    )
    pushed_to_vetting_at = models.DateTimeField(null=True, blank=True, db_index=True)
    pushed_to_vetting_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pushed_rawjob_classifications",
    )
    pushed_to_vetting_note = models.TextField(blank=True)
    pushed_to_vetting_with_warnings = models.BooleanField(default=False)
    pushed_warning_codes = models.JSONField(default=list, blank=True)
    ready_for_vetting = models.BooleanField(default=False, db_index=True)
    last_merged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"RawJob {self.raw_job_id} merged classification"


class RawJobClassificationConflict(models.Model):
    """Field-level disagreement record between backend and secondary classifiers."""

    class Resolution(models.TextChoices):
        AGREED = "AGREED", _("Agreed")
        BACKEND = "BACKEND", _("Backend Wins")
        SECONDARY = "SECONDARY", _("Secondary Wins")
        MANUAL = "MANUAL", _("Manual Override")
        REVIEW = "REVIEW", _("Needs Review")

    class Severity(models.TextChoices):
        INFO = "INFO", _("Info")
        WARN = "WARN", _("Warn")
        CRITICAL = "CRITICAL", _("Critical")

    raw_job = models.ForeignKey(
        "harvest.RawJob",
        on_delete=models.CASCADE,
        related_name="classification_conflicts",
    )
    snapshot = models.ForeignKey(
        RawJobClassificationSnapshot,
        on_delete=models.CASCADE,
        related_name="field_conflicts",
    )
    field_path = models.CharField(max_length=120, db_index=True)
    backend_value = models.JSONField(null=True, blank=True)
    secondary_value = models.JSONField(null=True, blank=True)
    resolved_value = models.JSONField(null=True, blank=True)
    resolution = models.CharField(
        max_length=16,
        choices=Resolution.choices,
        default=Resolution.REVIEW,
        db_index=True,
    )
    severity = models.CharField(
        max_length=12,
        choices=Severity.choices,
        default=Severity.WARN,
        db_index=True,
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["field_path", "id"]
        unique_together = [("snapshot", "field_path")]
        indexes = [
            models.Index(fields=["raw_job", "field_path"]),
            models.Index(fields=["resolution", "severity"]),
        ]

    def __str__(self):
        return f"RawJob {self.raw_job_id} conflict {self.field_path}"

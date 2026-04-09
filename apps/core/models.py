from django.db import models
from django.core.cache import cache
from django.conf import settings

from users.models import User, ConsultantProfile
from jobs.models import Job


class Organisation(models.Model):
    """
    Lightweight organisation/tenant for future white-label support.
    Currently optional and single-instance; safe to ignore when unused.
    """
    name = models.CharField(max_length=150, unique=True)
    slug = models.SlugField(max_length=160, unique=True)
    logo_url = models.URLField(blank=True)
    primary_color = models.CharField(max_length=20, blank=True, help_text="Tailwind color token or hex (e.g. 'blue-600' or '#0f172a').")
    accent_color = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Organisation"
        verbose_name_plural = "Organisations"

    def __str__(self):
        return self.name


class PlatformConfig(models.Model):
    """
    Singleton model to store global platform configuration.
    For future white-label, this can be extended to be per-organisation.
    """
    # Branding
    site_name = models.CharField(max_length=100, default="EduConsult")
    site_tagline = models.CharField(max_length=200, default="Connecting Experts with Opportunities", blank=True)
    logo_url = models.URLField(blank=True, help_text="External URL to logo image")

    # SEO
    meta_description = models.TextField(blank=True, help_text="Default meta description for SEO")
    meta_keywords = models.CharField(max_length=255, blank=True, help_text="Comma-separated keywords")

    # Contact
    contact_email = models.EmailField(default="support@educonsult.com")
    support_phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)

    # Feature Flags
    enable_consultant_registration = models.BooleanField(default=True, help_text="Allow new consultants to register")
    enable_job_applications = models.BooleanField(default=True, help_text="Allow consultants to apply for jobs")
    enable_public_consultant_view = models.BooleanField(default=True, help_text="Allow guests to view consultant profiles")
    match_jd_title_default = models.BooleanField(
        default=False,
        help_text="If enabled, the most recent resume role title is replaced with the JD title by default."
    )
    enable_consultant_global_interview_calendar = models.BooleanField(
        default=False,
        help_text="If enabled, consultants can view the full interview calendar (all candidates) instead of only their own."
    )

    # System
    maintenance_mode = models.BooleanField(default=False)
    maintenance_message = models.TextField(default="We are currently performing scheduled maintenance. Please check back later.")
    session_timeout_minutes = models.IntegerField(default=60, help_text="Session expiry time in minutes")
    max_upload_size_mb = models.IntegerField(default=5, help_text="Max file upload size in MB")

    # Email ingestion (IMAP) – configuration only (future feature)
    email_ingest_enabled = models.BooleanField(
        default=False,
        help_text="Enable IMAP email ingestion for status updates (requires IMAP credentials).",
    )
    email_imap_host = models.CharField(
        max_length=255,
        blank=True,
        default="imap.gmail.com",
        help_text="IMAP server hostname (e.g. imap.gmail.com).",
    )
    email_imap_port = models.IntegerField(
        default=993,
        help_text="IMAP port (993 for SSL).",
    )
    email_imap_use_ssl = models.BooleanField(
        default=True,
        help_text="Use SSL/TLS for IMAP connection (recommended).",
    )
    email_imap_username = models.CharField(
        max_length=255,
        blank=True,
        help_text="IMAP username (usually the email address of the inbox).",
    )
    email_imap_encrypted_password = models.TextField(
        blank=True,
        help_text="Encrypted IMAP password or app password (stored encrypted).",
    )
    email_poll_interval_seconds = models.IntegerField(
        default=60,
        help_text="How often to poll the IMAP inbox for new messages (in seconds).",
    )
    email_auto_poll_enabled = models.BooleanField(
        default=False,
        help_text="If enabled, a background worker (Celery Beat) will poll IMAP automatically.",
    )
    email_ai_fallback_enabled = models.BooleanField(
        default=False,
        help_text="If enabled, use AI as a fallback when rules are unsure (token usage).",
    )
    email_ai_confidence_threshold = models.PositiveSmallIntegerField(
        default=80,
        help_text="AI confidence threshold (0–100). Only apply AI results at or above this number.",
    )
    email_notify_employee_on_auto_update = models.BooleanField(
        default=False,
        help_text="Send an email to the employee who submitted the application when an auto-update happens.",
    )
    email_notify_consultant_on_auto_update = models.BooleanField(
        default=False,
        help_text="Send an email to the consultant when an auto-update happens.",
    )

    # Data pipeline / enrichment (Phase 4)
    google_kg_api_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="Google Knowledge Graph API key for company enrichment (optional).",
    )
    apollo_api_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="Apollo.io API key for organization enrichment (optional).",
    )
    hunter_api_key = models.CharField(
        max_length=255,
        blank=True,
        help_text="Hunter.io API key for company enrichment (optional).",
    )
    auto_enrich_on_create = models.BooleanField(
        default=True,
        help_text="When enabled, new companies are automatically queued for enrichment (Clearbit, OG, optional APIs).",
    )

    # Jobs pipeline (Phase 3)
    job_auto_close_after_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="If set, OPEN jobs older than this many days are closed automatically (daily task). Leave empty to disable.",
    )
    job_auto_close_when_link_dead = models.BooleanField(
        default=False,
        help_text="If enabled, OPEN jobs whose original posting URL is no longer live are closed automatically.",
    )

    # Job Pool / Vetting pipeline
    require_pool_staging = models.BooleanField(
        default=True,
        help_text=(
            "When enabled (recommended), all new jobs land in the Pool for review before going live. "
            "Disable to make new jobs OPEN immediately (legacy behaviour)."
        ),
    )
    auto_approve_pool_threshold = models.PositiveSmallIntegerField(
        default=0,
        help_text=(
            "Validation score threshold (0–100). Jobs that score at or above this number are "
            "automatically promoted from Pool to Open without manual review. "
            "Set to 0 to disable auto-approval entirely."
        ),
    )
    pool_review_notify_emails = models.TextField(
        blank=True,
        help_text=(
            "Comma-separated list of email addresses to notify whenever a new job enters the Pool. "
            "Leave blank to send no external emails. "
            "Example: admin@company.com, recruiter@company.com"
        ),
    )

    # Social Media
    twitter_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    github_url = models.URLField(blank=True)

    # Legal
    tos_url = models.URLField(blank=True, verbose_name="Terms of Service URL")
    privacy_policy_url = models.URLField(blank=True, verbose_name="Privacy Policy URL")

    def __str__(self):
        return "Platform Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1  # Singleton: always ID 1
        super(PlatformConfig, self).save(*args, **kwargs)
        cache.delete('platform_config')  # Invalidate cache on save

    def delete(self, *args, **kwargs):
        pass  # Prevent deletion

    @classmethod
    def load(cls):
        """
        Load the singleton instance. Create if not exists.
        """
        if cache.get('platform_config') is None:
            obj, created = cls.objects.get_or_create(pk=1)
            cache.set('platform_config', obj)
        return cache.get('platform_config')


class PipelineRunLog(models.Model):
    """
    Tracks last run of pipeline tasks for Settings UI.
    """
    task_name = models.CharField(max_length=100, unique=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_result = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Pipeline run log"
        verbose_name_plural = "Pipeline run logs"

    def __str__(self):
        return f"{self.task_name} @ {self.last_run_at}"


class Notification(models.Model):
    """In-app notification (Phase 3 notification center)."""

    class Kind(models.TextChoices):
        SUBMISSION = 'SUBMISSION', 'Submission'
        INTERVIEW = 'INTERVIEW', 'Interview'
        JOB = 'JOB', 'Job'
        SYSTEM = 'SYSTEM', 'System'
        MESSAGE = 'MESSAGE', 'Message'

    user = models.ForeignKey('users.User', on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SYSTEM)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=500, blank=True, help_text="Relative path, e.g. /submissions/12/")
    dedupe_key = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Optional stable id for idempotent creates (tasks, automations).",
    )
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['user', 'read_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'dedupe_key'],
                name='notification_user_dedupe_key_uniq',
                condition=models.Q(dedupe_key__isnull=False),
            ),
        ]

    def __str__(self):
        return f"{self.title} → {self.user_id}"


class BroadcastMessage(models.Model):
    """
    Admin/org broadcast: one message fan-out to many users with per-recipient audit (BroadcastDelivery).
    """

    class Audience(models.TextChoices):
        # Primary options (recruiting / workforce comms)
        EMPLOYEES_ONLY = 'EMPLOYEES_ONLY', 'Employees only'
        CONSULTANTS = 'CONSULTANTS', 'Consultants only'
        EMPLOYEES_AND_CONSULTANTS = (
            'EMPLOYEES_AND_CONSULTANTS',
            'Employees and consultants (both)',
        )
        # Other scopes
        ALL_ACTIVE = 'ALL_ACTIVE', 'Everyone (all active users)'
        STAFF = 'STAFF', 'Staff (admins + employees)'
        ADMINS = 'ADMINS', 'Admins only'

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=500, blank=True)
    kind = models.CharField(
        max_length=20,
        choices=Notification.Kind.choices,
        default=Notification.Kind.SYSTEM,
    )
    audience = models.CharField(
        max_length=32,
        choices=Audience.choices,
        default=Audience.EMPLOYEES_AND_CONSULTANTS,
        help_text='Who receives this broadcast (respects optional organisation filter below).',
    )
    organisation = models.ForeignKey(
        'core.Organisation',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text='Optional: limit recipients to this tenant.',
    )
    created_by = models.ForeignKey(
        'users.User',
        null=True,
        on_delete=models.SET_NULL,
        related_name='broadcasts_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class BroadcastDelivery(models.Model):
    """Audit row: who received (or skipped) a broadcast."""

    class Status(models.TextChoices):
        DELIVERED = 'DELIVERED', 'Delivered'
        SKIPPED_INAPP = 'SKIPPED_INAPP', 'Skipped (in-app category off)'

    broadcast = models.ForeignKey(BroadcastMessage, on_delete=models.CASCADE, related_name='deliveries')
    user = models.ForeignKey('users.User', on_delete=models.CASCADE, related_name='broadcast_deliveries')
    notification = models.ForeignKey(Notification, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DELIVERED,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['broadcast', 'user'], name='broadcast_delivery_user_uniq'),
        ]
        indexes = [
            models.Index(fields=['broadcast', 'status']),
        ]

    def __str__(self):
        return f"{self.broadcast_id} → user {self.user_id} ({self.status})"


class AuditLog(models.Model):
    """
    Logs critical actions performed by users for compliance and tracking.
    """
    actor = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, related_name='audit_logs')
    action = models.CharField(max_length=255)
    target_model = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.actor} - {self.action} at {self.timestamp}"


class LLMConfig(models.Model):
    """
    Singleton model to store LLM configuration and API credentials.
    """
    encrypted_api_key = models.TextField(blank=True, help_text="Encrypted OpenAI API key")
    active_model = models.CharField(max_length=100, default="gpt-4o-mini")
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.70)
    max_output_tokens = models.PositiveIntegerField(default=2000)

    monthly_token_cap = models.PositiveIntegerField(default=0, help_text="0 means no cap")
    generation_enabled = models.BooleanField(default=True)
    auto_disable_on_cap = models.BooleanField(default=True)

    data_pipelines_connected = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "LLM Configuration"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        self.pk = 1  # Singleton: always ID 1
        if not is_new:
            LLMConfigVersion.objects.create(
                config=self,
                active_model=self.active_model,
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
            )
        super().save(*args, **kwargs)
        cache.delete('llm_config')

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        if cache.get('llm_config') is None:
            obj, _ = cls.objects.get_or_create(pk=1)
            cache.set('llm_config', obj)
        return cache.get('llm_config')


class LLMConfigVersion(models.Model):
    config = models.ForeignKey(LLMConfig, on_delete=models.CASCADE, related_name='versions')
    active_model = models.CharField(max_length=100)
    temperature = models.DecimalField(max_digits=3, decimal_places=2)
    max_output_tokens = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class LLMUsageLog(models.Model):
    request_type = models.CharField(max_length=50, default='resume_generation')
    model_name = models.CharField(max_length=100)
    system_prompt = models.TextField(blank=True)
    user_prompt = models.TextField(blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    response_text = models.TextField(blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    cost_input = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    cost_output = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    cost_total = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    success = models.BooleanField(default=True)
    error_message = models.TextField(blank=True)

    job = models.ForeignKey(Job, on_delete=models.SET_NULL, null=True, blank=True)
    consultant = models.ForeignKey(ConsultantProfile, on_delete=models.SET_NULL, null=True, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

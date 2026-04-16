import hashlib
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class JobBoardPlatform(models.Model):
    """Registry of known ATS / job board platforms."""

    class ApiType(models.TextChoices):
        WORKDAY_API = "workday_api", "Workday REST API"
        GREENHOUSE_API = "greenhouse_api", "Greenhouse REST API"
        LEVER_API = "lever_api", "Lever REST API"
        ASHBY_GRAPHQL = "ashby_graphql", "Ashby GraphQL"
        HTML_SCRAPE = "html_scrape", "HTML Scrape"
        UNKNOWN = "unknown", "Unknown"

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    url_patterns = models.JSONField(
        default=list,
        help_text="List of URL substrings to match (e.g. ['myworkdayjobs.com']).",
    )
    api_type = models.CharField(
        max_length=20, choices=ApiType.choices, default=ApiType.UNKNOWN
    )
    fetch_endpoint_tmpl = models.TextField(
        blank=True,
        help_text="URL template. Use {tenant} as placeholder.",
    )
    headers_json = models.JSONField(
        default=dict, blank=True, help_text="Default request headers."
    )
    rate_limit_per_min = models.PositiveSmallIntegerField(default=10)
    requires_auth = models.BooleanField(default=False)
    is_enabled = models.BooleanField(default=True)
    logo_url = models.URLField(blank=True)
    color_hex = models.CharField(
        max_length=7, blank=True, default="#6B7280",
        help_text="Badge colour hex e.g. #4A90D9",
    )
    notes = models.TextField(blank=True)
    last_harvested_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Job Board Platform"
        verbose_name_plural = "Job Board Platforms"

    def __str__(self):
        return self.name

    @property
    def api_type_label(self):
        return dict(self.ApiType.choices).get(self.api_type, "Unknown")

    @property
    def api_badge_color(self):
        mapping = {
            "workday_api": "blue",
            "greenhouse_api": "green",
            "lever_api": "purple",
            "ashby_graphql": "indigo",
            "html_scrape": "yellow",
            "unknown": "gray",
        }
        return mapping.get(self.api_type, "gray")


class CompanyPlatformLabel(models.Model):
    """Maps a Company to its detected ATS / job board platform."""

    class Confidence(models.TextChoices):
        HIGH = "HIGH", "High"
        MEDIUM = "MEDIUM", "Medium"
        LOW = "LOW", "Low"
        UNKNOWN = "UNKNOWN", "Unknown"

    class DetectionMethod(models.TextChoices):
        URL_PATTERN = "URL_PATTERN", "URL Pattern Match"
        HTTP_HEAD = "HTTP_HEAD", "HTTP Redirect Follow"
        HTML_PARSE = "HTML_PARSE", "HTML Content Parse"
        MANUAL = "MANUAL", "Manually Set"
        UNDETECTED = "UNDETECTED", "Could Not Detect"

    company = models.OneToOneField(
        "companies.Company",
        on_delete=models.CASCADE,
        related_name="platform_label",
    )
    platform = models.ForeignKey(
        JobBoardPlatform,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="labels",
    )
    custom_career_url = models.URLField(
        blank=True, help_text="Custom/own career page if no standard ATS."
    )
    tenant_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="ATS tenant / token extracted from career URL.",
    )
    confidence = models.CharField(
        max_length=10, choices=Confidence.choices, default=Confidence.UNKNOWN
    )
    detection_method = models.CharField(
        max_length=15,
        choices=DetectionMethod.choices,
        default=DetectionMethod.UNDETECTED,
    )
    detected_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(
        default=False, help_text="Manually verified by superuser."
    )
    verified_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_platform_labels",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    # ── Portal health (set by check_portal_health_task) ──────────────────────
    portal_alive = models.BooleanField(
        null=True, blank=True,
        help_text="True=HTTP 2xx/3xx, False=4xx/5xx/timeout, None=not yet checked.",
    )
    portal_last_verified = models.DateTimeField(
        null=True, blank=True,
        help_text="When the portal URL was last HTTP-checked.",
    )

    class Meta:
        ordering = ["company__name"]
        verbose_name = "Company Platform Label"
        verbose_name_plural = "Company Platform Labels"

    def __str__(self):
        plat = self.platform.name if self.platform else "Undetected"
        return f"{self.company.name} → {plat}"

    @property
    def confidence_color(self):
        return {"HIGH": "green", "MEDIUM": "yellow", "LOW": "orange", "UNKNOWN": "gray"}.get(
            self.confidence, "gray"
        )

    @property
    def career_page_url(self) -> str:
        """Constructed public job board URL for this company."""
        from .career_url import build_career_url
        if not self.platform:
            return ""
        return build_career_url(self.platform.slug, self.tenant_id)

    @property
    def scrape_status(self) -> str:
        """
        Returns one of:
          'verified'      — HTTP check confirmed portal is alive (2xx/3xx)
          'down'          — HTTP check confirmed portal is unreachable (4xx/5xx/timeout)
          'ready'         — platform + clean tenant, URL built but not yet HTTP-checked
          'needs_backfill'— tenant has https:// prefix (old bug), backfill will fix
          'no_tenant'     — platform detected but no tenant extracted yet
          'no_ats'        — explicitly detected as having no ATS
          'unknown'       — not scanned yet
        """
        if self.detection_method == self.DetectionMethod.UNDETECTED:
            return "no_ats"
        if not self.platform:
            return "unknown"
        if self.tenant_id and not self.tenant_id.startswith("https://"):
            # Has valid tenant — use HTTP health result if available
            if self.portal_alive is True:
                return "verified"
            if self.portal_alive is False:
                return "down"
            return "ready"
        if self.tenant_id:
            return "needs_backfill"
        return "no_tenant"


class HarvestRun(models.Model):
    """Audit log for each harvest or platform-detection execution."""

    class RunType(models.TextChoices):
        HARVEST = "HARVEST", "Harvest"
        DETECTION = "DETECTION", "Platform detection"

    class TriggerType(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        MANUAL = "MANUAL", "Manual (Superuser)"

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    run_type = models.CharField(
        max_length=12,
        choices=RunType.choices,
        default=RunType.HARVEST,
    )
    platform = models.ForeignKey(
        JobBoardPlatform,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="harvest_runs",
    )
    triggered_by = models.CharField(
        max_length=10, choices=TriggerType.choices, default=TriggerType.SCHEDULED
    )
    triggered_user = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_harvest_runs",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.RUNNING
    )
    companies_targeted = models.PositiveIntegerField(default=0)
    jobs_fetched = models.PositiveIntegerField(default=0)
    jobs_new = models.PositiveIntegerField(default=0)
    jobs_duplicate = models.PositiveIntegerField(default=0)
    jobs_failed = models.PositiveIntegerField(default=0)
    detection_detected = models.PositiveIntegerField(
        default=0,
        help_text="Companies with a detected platform (detection runs only).",
    )
    detection_total = models.PositiveIntegerField(
        default=0,
        help_text="Companies processed in this detection run.",
    )
    error_log = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Harvest Run"
        verbose_name_plural = "Harvest Runs"

    def __str__(self):
        if self.run_type == self.RunType.DETECTION:
            return f"Run #{self.pk} – Platform detection ({self.status})"
        label = self.platform.name if self.platform else "—"
        return f"Run #{self.pk} – {label} ({self.status})"

    @property
    def duration_seconds(self):
        if self.finished_at and self.started_at:
            return int((self.finished_at - self.started_at).total_seconds())
        return None

    @property
    def status_color(self):
        return {
            "RUNNING": "blue",
            "SUCCESS": "green",
            "PARTIAL": "yellow",
            "FAILED": "red",
        }.get(self.status, "gray")


class HarvestedJob(models.Model):
    """Raw job listing fetched from an external ATS platform."""

    class SyncStatus(models.TextChoices):
        PENDING = "PENDING", "Pending Review"
        SYNCED = "SYNCED", "Synced to Pool"
        SKIPPED = "SKIPPED", "Skipped (Duplicate)"
        FAILED = "FAILED", "Sync Failed"

    class JobType(models.TextChoices):
        FULL_TIME = "FULL_TIME", "Full-Time"
        PART_TIME = "PART_TIME", "Part-Time"
        CONTRACT = "CONTRACT", "Contract"
        INTERNSHIP = "INTERNSHIP", "Internship"
        UNKNOWN = "UNKNOWN", "Unknown"

    # Relations
    harvest_run = models.ForeignKey(
        HarvestRun, on_delete=models.CASCADE, related_name="harvested_jobs"
    )
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="harvested_jobs",
    )
    platform = models.ForeignKey(
        JobBoardPlatform, on_delete=models.CASCADE, related_name="harvested_jobs"
    )

    # Identity / Dedup
    external_id = models.CharField(max_length=500, blank=True)
    url_hash = models.CharField(max_length=64, db_index=True)
    original_url = models.URLField(max_length=1000)

    # Core
    title = models.CharField(max_length=300)
    company_name = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=255, blank=True)
    is_remote = models.BooleanField(null=True, blank=True)
    job_type = models.CharField(
        max_length=15, choices=JobType.choices, default=JobType.UNKNOWN
    )
    department = models.CharField(max_length=255, blank=True)

    # Compensation
    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_currency = models.CharField(max_length=10, default="USD")
    salary_raw = models.CharField(max_length=200, blank=True)

    # Content
    description_html = models.TextField(blank=True)
    description_text = models.TextField(blank=True)
    requirements_text = models.TextField(blank=True)
    benefits_text = models.TextField(blank=True)

    # Dates & Lifecycle
    posted_date = models.DateField(null=True, blank=True)
    closes_date = models.DateField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    # Sync
    synced_to_job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="harvested_source",
    )
    sync_status = models.CharField(
        max_length=10, choices=SyncStatus.choices, default=SyncStatus.PENDING
    )

    # Raw payload
    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-fetched_at"]
        unique_together = [("platform", "url_hash")]
        verbose_name = "Harvested Job"
        verbose_name_plural = "Harvested Jobs"

    def __str__(self):
        return f"{self.title} @ {self.company_name} ({self.platform.name})"

    def save(self, *args, **kwargs):
        if not self.url_hash and self.original_url:
            self.url_hash = hashlib.sha256(self.original_url.strip().encode()).hexdigest()
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=24)
        super().save(*args, **kwargs)

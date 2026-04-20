import hashlib
from datetime import timedelta

from django.conf import settings
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


# ─────────────────────────────────────────────────────────────────────────────
# Raw Jobs — comprehensive per-company job harvesting with full field coverage
# ─────────────────────────────────────────────────────────────────────────────

class FetchBatch(models.Model):
    """Groups a bulk fetch session (e.g. 'all Workday companies on 2026-04-16')."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Partial"
        CANCELLED = "CANCELLED", "Cancelled"

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    name = models.CharField(max_length=256, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    platform_filter = models.CharField(
        max_length=64, blank=True,
        help_text="Platform slug filter, e.g. 'workday'. Empty = all platforms.",
    )
    task_id = models.CharField(max_length=64, blank=True)
    total_companies = models.PositiveIntegerField(default=0)
    completed_companies = models.PositiveIntegerField(default=0)
    failed_companies = models.PositiveIntegerField(default=0)
    total_jobs_found = models.PositiveIntegerField(default=0)
    total_jobs_new = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Fetch Batch"
        verbose_name_plural = "Fetch Batches"

    def __str__(self):
        return self.name or f"Batch #{self.pk} ({self.status})"

    @property
    def progress_pct(self):
        if not self.total_companies:
            return 0
        done = self.completed_companies + self.failed_companies
        return min(100, int(done / self.total_companies * 100))

    @property
    def duration_seconds(self):
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        return None


class CompanyFetchRun(models.Model):
    """Tracks a single per-company raw-jobs fetch attempt."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    class ErrorType(models.TextChoices):
        TIMEOUT = "TIMEOUT", "Timeout"
        HTTP_ERROR = "HTTP_ERROR", "HTTP Error"
        PARSE_ERROR = "PARSE_ERROR", "Parse Error"
        NO_TENANT = "NO_TENANT", "No Tenant ID"
        PLATFORM_ERROR = "PLATFORM_ERROR", "Platform Error"
        RATE_LIMITED = "RATE_LIMITED", "Rate Limited"

    label = models.ForeignKey(
        CompanyPlatformLabel,
        on_delete=models.CASCADE,
        related_name="raw_fetch_runs",
    )
    batch = models.ForeignKey(
        FetchBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="company_runs",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    task_id = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    jobs_found = models.PositiveIntegerField(default=0)
    jobs_total_available = models.PositiveIntegerField(
        default=0,
        help_text="Total jobs reported by the platform API (even if we only fetched a subset)",
    )
    jobs_new = models.PositiveIntegerField(default=0)
    jobs_updated = models.PositiveIntegerField(default=0)
    jobs_duplicate = models.PositiveIntegerField(default=0)
    jobs_failed = models.PositiveIntegerField(default=0)
    pages_fetched = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    error_type = models.CharField(
        max_length=16,
        choices=ErrorType.choices,
        blank=True,
    )
    triggered_by = models.CharField(
        max_length=16,
        default="MANUAL",
        help_text="MANUAL | SCHEDULED | BATCH",
    )

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Company Fetch Run"
        verbose_name_plural = "Company Fetch Runs"

    def __str__(self):
        return f"{self.label} – {self.status} ({self.started_at})"

    @property
    def duration_seconds(self):
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        return None


class RawJob(models.Model):
    """Comprehensive job record harvested from an external ATS platform."""

    class LocationType(models.TextChoices):
        REMOTE = "REMOTE", "Remote"
        HYBRID = "HYBRID", "Hybrid"
        ONSITE = "ONSITE", "On-Site"
        UNKNOWN = "UNKNOWN", "Unknown"

    class EmploymentType(models.TextChoices):
        FULL_TIME = "FULL_TIME", "Full-Time"
        PART_TIME = "PART_TIME", "Part-Time"
        CONTRACT = "CONTRACT", "Contract"
        INTERNSHIP = "INTERNSHIP", "Internship"
        TEMPORARY = "TEMPORARY", "Temporary"
        OTHER = "OTHER", "Other"
        UNKNOWN = "UNKNOWN", "Unknown"

    class ExperienceLevel(models.TextChoices):
        ENTRY = "ENTRY", "Entry Level"
        MID = "MID", "Mid Level"
        SENIOR = "SENIOR", "Senior"
        LEAD = "LEAD", "Lead"
        MANAGER = "MANAGER", "Manager"
        DIRECTOR = "DIRECTOR", "Director"
        EXECUTIVE = "EXECUTIVE", "Executive"
        UNKNOWN = "UNKNOWN", "Unknown"

    class SyncStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SYNCED = "SYNCED", "Synced"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    # ── Relations ─────────────────────────────────────────────────────────────
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        related_name="raw_jobs",
    )
    platform_label = models.ForeignKey(
        CompanyPlatformLabel,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="raw_jobs",
    )
    job_platform = models.ForeignKey(
        JobBoardPlatform,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="raw_jobs",
    )

    # ── Identity / Dedup ──────────────────────────────────────────────────────
    external_id = models.CharField(max_length=512, blank=True)
    url_hash = models.CharField(max_length=64, unique=True, db_index=True)
    original_url = models.URLField(max_length=1024, blank=True)
    apply_url = models.URLField(max_length=1024, blank=True)

    # ── Core fields ───────────────────────────────────────────────────────────
    title = models.CharField(max_length=512)
    company_name = models.CharField(max_length=256, blank=True)
    department = models.CharField(max_length=256, blank=True)
    team = models.CharField(max_length=256, blank=True)

    # ── Location ──────────────────────────────────────────────────────────────
    location_raw = models.CharField(max_length=512, blank=True)
    city = models.CharField(max_length=128, blank=True)
    state = models.CharField(max_length=128, blank=True)
    country = models.CharField(max_length=128, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    location_type = models.CharField(
        max_length=8, choices=LocationType.choices, default=LocationType.UNKNOWN
    )
    is_remote = models.BooleanField(default=False)

    # ── Employment ────────────────────────────────────────────────────────────
    employment_type = models.CharField(
        max_length=12, choices=EmploymentType.choices, default=EmploymentType.UNKNOWN
    )
    experience_level = models.CharField(
        max_length=10, choices=ExperienceLevel.choices, default=ExperienceLevel.UNKNOWN
    )

    # ── Compensation ──────────────────────────────────────────────────────────
    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_currency = models.CharField(max_length=8, default="USD")
    salary_period = models.CharField(max_length=16, blank=True)
    salary_raw = models.CharField(max_length=256, blank=True)

    # ── Content ───────────────────────────────────────────────────────────────
    description = models.TextField(blank=True)
    requirements = models.TextField(blank=True)
    benefits = models.TextField(blank=True)

    # ── Dates ─────────────────────────────────────────────────────────────────
    posted_date = models.DateField(null=True, blank=True)
    closing_date = models.DateField(null=True, blank=True)

    # ── Platform meta ─────────────────────────────────────────────────────────
    platform_slug = models.CharField(max_length=64, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)

    # ── Enriched: skills & tech ───────────────────────────────────────────────
    # All extracted skills (tech + soft); populated by enrichments.extract_enrichments()
    skills = models.JSONField(default=list, blank=True)
    # Subset of skills that are programming languages / frameworks / tools
    tech_stack = models.JSONField(default=list, blank=True)
    # Job function category (Engineering, Data & Analytics, Product, etc.)
    job_category = models.CharField(max_length=64, blank=True)

    # ── Enriched: experience requirements ────────────────────────────────────
    # "5+ years" → years_required=5; "3-7 years" → years_required=3, years_required_max=7
    years_required = models.PositiveSmallIntegerField(null=True, blank=True)
    years_required_max = models.PositiveSmallIntegerField(null=True, blank=True)
    education_required = models.CharField(
        max_length=12,
        choices=[
            ("", "Unknown"), ("HS", "High School"), ("ASSOCIATE", "Associate's"),
            ("BS", "Bachelor's"), ("MS", "Master's"), ("MBA", "MBA"), ("PHD", "PhD"),
        ],
        blank=True,
    )

    # ── Enriched: legal & visa ────────────────────────────────────────────────
    # True = sponsors, False = doesn't sponsor, None = not mentioned
    visa_sponsorship = models.BooleanField(null=True, blank=True)
    # e.g. "US citizens only", "US persons", "Any"
    work_authorization = models.CharField(max_length=64, blank=True)
    clearance_required = models.BooleanField(default=False)

    # ── Enriched: compensation extras ────────────────────────────────────────
    salary_equity = models.BooleanField(default=False)
    signing_bonus = models.BooleanField(default=False)
    relocation_assistance = models.BooleanField(default=False)

    # ── Enriched: work conditions ─────────────────────────────────────────────
    # e.g. "up to 25%", "occasional", "extensive"
    travel_required = models.CharField(max_length=64, blank=True)

    # ── Enriched: structured lists ────────────────────────────────────────────
    certifications = models.JSONField(default=list, blank=True)
    benefits_list = models.JSONField(default=list, blank=True)
    languages_required = models.JSONField(default=list, blank=True)

    # ── Enriched: quality signals ─────────────────────────────────────────────
    word_count = models.PositiveIntegerField(default=0)
    # 0.0–1.0: fraction of key fields populated (description, salary, location…)
    quality_score = models.FloatField(null=True, blank=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    sync_status = models.CharField(
        max_length=8, choices=SyncStatus.choices, default=SyncStatus.PENDING
    )
    is_active = models.BooleanField(default=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # Parallel JD backfill: set while a worker holds the row (cleared when done).
    # Stale locks are reclaimed after BACKFILL_LOCK_STALE_MINUTES in tasks.
    jd_backfill_locked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-fetched_at"]
        indexes = [
            models.Index(fields=["company", "platform_slug"]),
            models.Index(fields=["platform_slug"]),
            models.Index(fields=["sync_status"]),
            models.Index(fields=["posted_date"]),
            models.Index(fields=["employment_type"]),
            models.Index(fields=["location_type"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["job_category"]),
            models.Index(fields=["education_required"]),
            models.Index(fields=["visa_sponsorship"]),
            models.Index(fields=["clearance_required"]),
            models.Index(fields=["quality_score"]),
        ]
        verbose_name = "Raw Job"
        verbose_name_plural = "Raw Jobs"

    def __str__(self):
        return f"{self.title} @ {self.company_name}"

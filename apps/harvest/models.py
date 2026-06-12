import hashlib
import gzip
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
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

    class SupportTier(models.TextChoices):
        HEALTHY      = "healthy",       "Healthy"
        DEGRADED     = "degraded",      "Degraded"
        EXPERIMENTAL = "experimental",  "Experimental"
        UNSUPPORTED  = "unsupported",   "Unsupported"

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
    support_tier = models.CharField(
        max_length=16,
        choices=SupportTier.choices,
        default=SupportTier.HEALTHY,
        help_text="Operational health tier. UNSUPPORTED = excluded from normal harvest rotation.",
    )
    notes = models.TextField(blank=True)
    last_harvested_at = models.DateTimeField(null=True, blank=True)
    title_in_list = models.BooleanField(
        default=False,
        help_text="True when the list endpoint exposes enough title data for selective role filtering before JD fetch.",
    )
    list_has_description = models.BooleanField(
        default=False,
        help_text=(
            "True when the list endpoint returns job description text (e.g. Lever, Ashby, Greenhouse). "
            "Enables Tier-2 JD gate with ZERO extra HTTP calls — snippet is extracted from the list payload."
        ),
    )
    unknown_jd_budget_per_run = models.PositiveSmallIntegerField(
        default=2,
        help_text="Max UNKNOWN title decisions per company run that may continue to JD fetch in selective harvest.",
    )
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
    portal_consecutive_failures = models.PositiveSmallIntegerField(
        default=0,
        db_index=True,
        help_text="Consecutive failed health checks. Portal is marked down only after the configured threshold.",
    )
    consecutive_zero_tech_fetches = models.PositiveSmallIntegerField(default=0)
    zero_tech_last_flagged_at = models.DateTimeField(null=True, blank=True)
    skip_in_selective_harvest = models.BooleanField(default=False)
    skip_expires_at = models.DateTimeField(null=True, blank=True)
    custom_include_phrases = models.JSONField(default=list, blank=True)

    # ── Pagination checkpoint (Workday 3k-job timeout fix) ───────────────────
    last_fetch_offset = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Pagination checkpoint for incremental harvests. "
            "Stores the page offset where the last run stopped (e.g. after a timeout). "
            "Next run resumes from here. Reset to 0 on successful completion."
        ),
    )

    # ── Hit-rate intelligence (company-level harvest quality tracking) ────────
    # Cumulative counts updated after each harvest run. Used to tune per-company
    # thresholds and identify low-yield vs high-yield companies automatically.
    historical_hard_yes_count = models.PositiveIntegerField(
        default=0,
        help_text="Cumulative HARD_YES title-gate decisions for this company.",
    )
    historical_ambiguous_count = models.PositiveIntegerField(
        default=0,
        help_text="Cumulative AMBIGUOUS title-gate decisions for this company.",
    )
    historical_hard_no_count = models.PositiveIntegerField(
        default=0,
        help_text="Cumulative HARD_NO title-gate decisions for this company.",
    )
    historical_confirmed_count = models.PositiveIntegerField(
        default=0,
        help_text="Cumulative CONFIRMED JD-gate decisions for this company.",
    )
    historical_rejected_count = models.PositiveIntegerField(
        default=0,
        help_text="Cumulative REJECTED JD-gate decisions for this company.",
    )
    last_hit_rate_computed_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When hit-rate counters were last updated.",
    )
    jd_gate_threshold_override = models.FloatField(
        null=True, blank=True,
        help_text=(
            "Per-company JD gate confidence threshold override. "
            "NULL = use global HarvestEngineConfig value. "
            "Set lower (e.g. 0.50) for high-yield companies; higher for low-yield."
        ),
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


# Phase 5: HarvestRun and HarvestedJob removed.
# All harvest audit events → jobs.PipelineEvent
# All raw job storage → harvest.RawJob


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
    # Set True by StopBatchView → every queued task checks this at startup and exits fast.
    # Cleared back to False by ResumeBatchView before re-dispatching.
    stop_requested = models.BooleanField(
        default=False,
        help_text="If True, queued tasks for this batch will exit immediately on pickup.",
    )
    is_full_crawl = models.BooleanField(
        default=False,
        help_text=(
            "True when the batch was launched with fetch_all=True — "
            "every company fetches its entire board, ignoring the since_hours window. "
            "False = incremental (last 25 h only)."
        ),
    )
    total_companies = models.PositiveIntegerField(default=0)
    completed_companies = models.PositiveIntegerField(default=0)
    failed_companies = models.PositiveIntegerField(default=0)
    total_jobs_found = models.PositiveIntegerField(default=0)
    total_jobs_new = models.PositiveIntegerField(default=0)
    audit_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured harvest audit: queue snapshot + completion metrics (UI + grep-friendly logs).",
    )
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


class HarvestRoleCategory(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=0)
    include_phrases = models.JSONField(default=list)
    exclude_phrases = models.JSONField(default=list)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "name"]
        verbose_name = "Harvest role category"
        verbose_name_plural = "Harvest role categories"

    def __str__(self):
        return self.name


class JobDomain(models.Model):
    """
    GUI-editable replacement for the hardcoded _DOMAIN_PATTERNS list in enrichments.py.

    Each row is one domain slug + its regex pattern.  The regex is validated
    before save so a bad pattern can never reach the harvest engine.
    Patterns are loaded from DB at runtime (5-min cache) so changes take
    effect within one cache window — no code change, no deploy needed.

    Priority controls match order: lower number wins (same as _DOMAIN_PATTERNS order).
    """

    class TopCategory(models.TextChoices):
        IT          = "IT",          "Information Technology"
        NON_IT      = "NON_IT",      "Non-IT / Business"
        ENGINEERING = "ENGINEERING", "Engineering (Non-IT)"
        HEALTHCARE  = "HEALTHCARE",  "Healthcare & Clinical"
        OTHER       = "OTHER",       "Other"

    slug            = models.SlugField(max_length=80, unique=True)
    name            = models.CharField(max_length=100)
    regex_pattern   = models.TextField(
        help_text="Python regex matched against job title and description (case-insensitive). "
                  "Validated before save — bad regex is rejected."
    )
    top_category    = models.CharField(
        max_length=20, choices=TopCategory.choices, default=TopCategory.IT, db_index=True
    )
    priority        = models.PositiveSmallIntegerField(
        default=500,
        help_text="Lower = matched first. Keep gaps (10, 20, 30…) so you can insert between.",
    )
    is_active       = models.BooleanField(default=True, db_index=True)
    notes           = models.TextField(blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "slug"]
        verbose_name = "Job Domain"
        verbose_name_plural = "Job Domains"

    def __str__(self):
        return f"{self.name} ({self.slug})"

    @staticmethod
    def _split_regex_alternatives(pattern: str) -> list[str]:
        """Split a simple regex alternation without splitting nested groups."""
        parts: list[str] = []
        depth = 0
        buf: list[str] = []
        escaped = False
        for char in pattern:
            if escaped:
                buf.append(char)
                escaped = False
                continue
            if char == "\\":
                buf.append(char)
                escaped = True
                continue
            if char == "(":
                depth += 1
            elif char == ")" and depth > 0:
                depth -= 1
            elif char == "|" and depth == 0:
                item = "".join(buf).strip()
                if item:
                    parts.append(item)
                buf = []
                continue
            buf.append(char)
        item = "".join(buf).strip()
        if item:
            parts.append(item)
        return parts

    @classmethod
    def keywords_from_pattern(cls, pattern: str, *, name: str = "", slug: str = "") -> list[str]:
        """
        Best-effort plain keywords for MarketingRole.match_keywords.

        JobDomain remains the source of truth for regex classification. MarketingRole
        keywords are a fallback signal, so only obviously literal regex pieces are
        converted here; complex patterns still get the rule name/slug as keywords.
        """
        import re as _re

        keywords: list[str] = []
        raw = (pattern or "").strip()
        if raw.startswith(r"\b(") and raw.endswith(r")\b"):
            raw = raw[3:-3]
        for part in cls._split_regex_alternatives(raw):
            item = part.strip()
            item = _re.sub(r"^\\b", "", item)
            item = _re.sub(r"\\b$", "", item)
            item = item.replace(r"\s*", " ").replace(r"\s+", " ")
            item = item.replace(r"\-", "-").replace(r"\/", "/")
            item = item.replace(r"\.", ".").replace(r"\&", "&")
            item = _re.sub(r"\s+", " ", item).strip(" ()")
            if not item:
                continue
            if _re.search(r"[\[\]\(\)\{\}\?\+\*\|\^\$]", item):
                continue
            keywords.append(item.lower())

        for fallback in (name, slug.replace("-", " ")):
            fallback = " ".join((fallback or "").lower().split())
            if fallback:
                keywords.append(fallback)

        out: list[str] = []
        seen: set[str] = set()
        for keyword in keywords:
            if keyword and keyword not in seen:
                seen.add(keyword)
                out.append(keyword)
        return out[:25]

    def sync_marketing_role(self):
        """Ensure the matching MarketingRole exists for downstream matching."""
        from users.models import MarketingRole

        role = MarketingRole.objects.filter(slug=self.slug).first()
        if role is None:
            role = MarketingRole.objects.filter(name=self.name).first()
            if role is not None:
                role.slug = self.slug

        if role is None:
            role = MarketingRole(slug=self.slug, name=self.name, is_active=self.is_active)

        if not MarketingRole.objects.exclude(pk=role.pk).filter(name=self.name).exists():
            role.name = self.name
        role.top_category = self.top_category
        role.display_order = self.priority
        if self.is_active:
            role.is_active = True
        role.match_keywords = self.keywords_from_pattern(
            self.regex_pattern,
            name=self.name,
            slug=self.slug,
        )
        role.save()
        return role

    def save(self, *args, **kwargs):
        import re as _re
        sync_marketing_role = kwargs.pop("sync_marketing_role", True)
        # Validate regex — reject bad patterns before they ever reach the engine
        try:
            _re.compile(self.regex_pattern, _re.IGNORECASE)
        except _re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc
        super().save(*args, **kwargs)
        self._bust_cache()
        if sync_marketing_role:
            self.sync_marketing_role()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        self._bust_cache()
        return result

    @staticmethod
    def _bust_cache():
        try:
            from django.core.cache import cache
            cache.delete("job_domain_patterns_v1")
        except Exception:
            pass

    @classmethod
    def compiled_patterns(cls) -> list:
        """
        Return ordered list of (slug, compiled_regex) tuples.
        Cached for 5 minutes.  Falls back to hardcoded patterns if DB is empty
        (e.g. before the seed migration runs on a fresh deploy).
        """
        import re as _re
        from django.core.cache import cache

        cached = cache.get("job_domain_patterns_v1")
        if cached is not None:
            return cached

        rows = list(cls.objects.filter(is_active=True).order_by("priority").values("slug", "regex_pattern"))
        if rows:
            patterns = []
            for row in rows:
                try:
                    patterns.append((row["slug"], _re.compile(row["regex_pattern"], _re.IGNORECASE)))
                except _re.error:
                    pass  # skip broken patterns — don't crash the harvest engine
        else:
            # Fallback: DB empty or not yet seeded — use hardcoded list
            from .enrichments import _COMPILED_DOMAIN_PATTERNS
            patterns = list(_COMPILED_DOMAIN_PATTERNS)

        cache.set("job_domain_patterns_v1", patterns, 300)
        return patterns


class HarvestFilterSnapshot(models.Model):
    snapshot_id = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    taken_at = models.DateTimeField(auto_now_add=True)
    category_data = models.JSONField()
    phrase_hash = models.CharField(max_length=64)
    batch = models.ForeignKey(
        FetchBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="filter_snapshots",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-taken_at"]
        verbose_name = "Harvest filter snapshot"
        verbose_name_plural = "Harvest filter snapshots"

    def __str__(self):
        return str(self.snapshot_id)

    @classmethod
    def create_snapshot(cls, batch=None, notes: str = ""):
        from .role_filter import compute_phrase_hash

        cfg = HarvestEngineConfig.get()
        categories = list(
            HarvestRoleCategory.objects.filter(is_active=True)
            .order_by("priority", "name")
            .values("name", "slug", "priority", "include_phrases", "exclude_phrases")
        )
        hard_negatives = cfg.hard_negative_phrases if isinstance(cfg.hard_negative_phrases, list) else []
        payload = {
            "categories": categories,
            "hard_negative_phrases": hard_negatives,
        }
        return cls.objects.create(
            batch=batch,
            category_data=payload,
            phrase_hash=compute_phrase_hash(payload),
            notes=notes,
        )

    def get_categories(self) -> list[dict]:
        data = self.category_data if isinstance(self.category_data, dict) else {}
        categories = data.get("categories") or []
        return categories if isinstance(categories, list) else []

    def get_hard_negatives(self) -> list[str]:
        data = self.category_data if isinstance(self.category_data, dict) else {}
        phrases = data.get("hard_negative_phrases") or []
        return phrases if isinstance(phrases, list) else []


class HarvestOpsRun(models.Model):
    """Audit trail for pipeline ops that are not tied to a FetchBatch (detect, backfill, sync, etc.)."""

    class Operation(models.TextChoices):
        DETECT_PLATFORMS = "detect_platforms", "Detect platforms"
        BACKFILL_JD = "backfill_jd", "Backfill JD"
        VALIDATE_URLS = "validate_urls", "Validate live links"
        SYNC_POOL = "sync_pool", "Sync to vet pool"
        CLEANUP = "cleanup", "Cleanup harvested"
        CLASSIFY = "classify", "Classify raw jobs"
        CLASSIFY_DOMAINS = "classify_domains", "Classify domains"
        LLM_CLASSIFY = "llm_classify", "LLM classify (second pass)"
        EVALUATE_SCOPE = "evaluate_scope", "Evaluate RawJob scope"
        BACKFILL_ROLES = "backfill_roles", "Backfill marketing roles"
        REFETCH_LOCATIONS = "refetch_locations", "Refetch ambiguous locations"
        BACKFILL_ENRICHMENT = "backfill_enrichment", "Backfill enrichment"
        CONFIG_FAILURE = "config_failure", "Config read failure"
        RETRY_FAILED = "retry_failed", "Retry failed fetches"

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    operation = models.CharField(max_length=64, choices=Operation.choices, db_index=True)
    celery_task_id = models.CharField(max_length=128, blank=True, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    audit_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured audit: queue snapshot + completion metrics (same shape spirit as FetchBatch).",
    )
    progress_current = models.IntegerField(default=0)
    progress_total = models.IntegerField(default=0)
    progress_message = models.CharField(max_length=256, blank=True)
    last_heartbeat_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Last progress heartbeat written by the worker. Used to detect orphaned RUNNING ops.",
    )
    triggered_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="harvest_ops_runs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = "Harvest ops run"
        verbose_name_plural = "Harvest ops runs"

    def __str__(self):
        return f"{self.operation} #{self.pk} ({self.status})"


class CompanyFetchRun(models.Model):
    """Tracks a single per-company raw-jobs fetch attempt."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        EMPTY   = "EMPTY",   "Empty (0 jobs)"   # fetch succeeded but returned 0 jobs
        PARTIAL = "PARTIAL", "Partial"
        FAILED  = "FAILED",  "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    class ErrorType(models.TextChoices):
        TIMEOUT        = "TIMEOUT",        "Timeout"
        HTTP_ERROR     = "HTTP_ERROR",     "HTTP Error"
        PARSE_ERROR    = "PARSE_ERROR",    "Parse Error"
        NO_TENANT      = "NO_TENANT",      "No Tenant ID"
        PLATFORM_ERROR = "PLATFORM_ERROR", "Platform Error"
        RATE_LIMITED   = "RATE_LIMITED",   "Rate Limited"

    class IssueCode(models.TextChoices):
        """Fine-grained reason code — supplements error_type for analytics."""
        NONE                = "",                   "None"
        PORTAL_DOWN         = "PORTAL_DOWN",        "Portal Down / Unreachable"
        PORTAL_BLOCKED      = "PORTAL_BLOCKED",     "Anti-bot / Access Blocked"
        NO_JOBS_RETURNED    = "NO_JOBS_RETURNED",   "Portal reachable but returned 0 jobs"
        NO_ACTIVE_TENANT    = "NO_ACTIVE_TENANT",   "Tenant has no active jobs"
        PARSE_FAILED        = "PARSE_FAILED",       "Response received but parse failed"
        TENANT_INVALID      = "TENANT_INVALID",     "Tenant ID missing or invalid"
        RATE_LIMITED        = "RATE_LIMITED",       "Rate limited by platform"
        FETCH_TIMEOUT       = "FETCH_TIMEOUT",      "Fetch timed out mid-crawl"
        PARTIAL_RESULTS     = "PARTIAL_RESULTS",    "Partial results (e.g. timeout after some pages)"

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
    is_test_run = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when this run intentionally capped writes for a smoke/test harvest.",
    )
    jobs_cap_applied = models.BooleanField(
        default=False,
        help_text="True when the platform returned more jobs than this run was allowed to write.",
    )
    jobs_new = models.PositiveIntegerField(default=0)
    jobs_updated = models.PositiveIntegerField(default=0)
    jobs_duplicate = models.PositiveIntegerField(default=0)
    jobs_failed = models.PositiveIntegerField(default=0)
    pages_fetched = models.PositiveIntegerField(default=0)
    jobs_detail_fetched = models.PositiveIntegerField(
        default=0,
        help_text="Number of jobs for which a detail page / second HTTP call was fetched.",
    )
    field_presence = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Per-field counts of jobs that had the field populated at harvest time. "
            "Keys: jd, requirements, responsibilities, department, geo, salary, "
            "employment_type, education, experience_level."
        ),
    )
    error_message = models.TextField(blank=True)
    error_type = models.CharField(
        max_length=16,
        choices=ErrorType.choices,
        blank=True,
    )
    issue_code = models.CharField(
        max_length=20,
        choices=IssueCode.choices,
        blank=True,
        default="",
        help_text="Fine-grained issue code for analytics (supplements error_type).",
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


class RawJobManager(models.Manager):
    def missing_jd(self, stale_minutes: int = 60):
        """Jobs that need JD backfill: no description + no active lock."""
        from django.utils import timezone
        from datetime import timedelta
        stale_before = timezone.now() - timedelta(minutes=stale_minutes)
        return self.filter(
            has_description=False,
        ).exclude(original_url="").exclude(
            Q(is_cold=True) | Q(jd_fetch_skipped=True) | Q(filter_decision="NO_MATCH")
        ).filter(
            Q(jd_backfill_locked_at__isnull=True)
            | Q(jd_backfill_locked_at__lt=stale_before),
        )


class LocationCache(models.Model):
    """Normalized location resolution cache used before any paid provider call."""

    class Status(models.TextChoices):
        RESOLVED = "RESOLVED", "Resolved"
        UNKNOWN = "UNKNOWN", "Unknown"
        FAILED = "FAILED", "Failed"
        RATE_LIMITED = "RATE_LIMITED", "Rate limited"
        DISABLED = "DISABLED", "Provider disabled"

    raw_text = models.CharField(max_length=512, blank=True)
    normalized_text = models.CharField(max_length=512, unique=True, db_index=True)
    country_code = models.CharField(max_length=2, blank=True, db_index=True)
    country_name = models.CharField(max_length=128, blank=True)
    region_code = models.CharField(max_length=16, blank=True)
    region_name = models.CharField(max_length=128, blank=True)
    city = models.CharField(max_length=128, blank=True)
    confidence = models.FloatField(default=0.0)
    source = models.CharField(max_length=32, blank=True, db_index=True)
    provider = models.CharField(max_length=32, blank=True)
    provider_place_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.UNKNOWN,
        db_index=True,
    )
    request_count = models.PositiveIntegerField(default=0)
    looked_up_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["normalized_text"]
        indexes = [
            models.Index(fields=["country_code", "status"], name="loc_cache_country_status_idx"),
            models.Index(fields=["source", "status"], name="loc_cache_source_status_idx"),
            models.Index(fields=["provider", "created_at"], name="loc_cache_provider_created_idx"),
        ]

    def __str__(self):
        label = self.country_code or self.status
        return f"{self.normalized_text} -> {label}"


class RawJob(models.Model):
    """Comprehensive job record harvested from an external ATS platform."""

    objects = RawJobManager()

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
        PENDING   = "PENDING",    "Pending"
        SYNCED    = "SYNCED",     "Synced"
        FAILED    = "FAILED",     "Failed"
        SKIPPED   = "SKIPPED",    "Skipped"
        DUPLICATE = "DUPLICATE",  "Duplicate"

    class ScopeStatus(models.TextChoices):
        UNSCOPED = "UNSCOPED", "Unscoped"
        PRIORITY_TARGET = "PRIORITY_TARGET", "Priority target"
        REVIEW_UNKNOWN_COUNTRY = "REVIEW_UNKNOWN_COUNTRY", "Review unknown country"
        COLD_NON_TARGET_COUNTRY = "COLD_NON_TARGET_COUNTRY", "Cold non-target country"
        COLD_NO_LOCATION = "COLD_NO_LOCATION", "Cold no location"

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
    fetch_batch = models.ForeignKey(
        "FetchBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="raw_jobs",
        help_text="Batch that produced or last refreshed this raw job.",
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
    # content_hash: sha256[:32] of (company_id|normalized_title|location_raw).
    # Not unique — allows re-posts of closed jobs. Used only to skip active dupes.
    content_hash = models.CharField(max_length=32, blank=True, db_index=True)
    original_url = models.URLField(max_length=1024, blank=True, db_index=True)
    apply_url = models.URLField(max_length=1024, blank=True)

    # ── Core fields ───────────────────────────────────────────────────────────
    title = models.CharField(max_length=512)
    normalized_title = models.CharField(max_length=255, blank=True)
    company_name = models.CharField(max_length=256, blank=True)
    department = models.CharField(max_length=256, blank=True)
    department_normalized = models.CharField(max_length=128, blank=True)
    team = models.CharField(max_length=256, blank=True)

    # ── Location ──────────────────────────────────────────────────────────────
    location_raw = models.CharField(max_length=512, blank=True)
    city = models.CharField(max_length=128, blank=True)
    state = models.CharField(max_length=128, blank=True)
    country = models.CharField(max_length=128, blank=True)
    location_candidates = models.JSONField(
        default=list,
        blank=True,
        help_text="All vendor/detail locations for multi-location postings.",
    )
    country_codes = models.JSONField(
        default=list,
        blank=True,
        help_text="All resolved ISO country codes from location_candidates.",
    )
    postal_code = models.CharField(max_length=32, blank=True)
    location_type = models.CharField(
        max_length=8, choices=LocationType.choices, default=LocationType.UNKNOWN
    )
    is_remote = models.BooleanField(default=False)

    # ── Scoped harvest routing ────────────────────────────────────────────────
    country_code = models.CharField(max_length=2, blank=True, db_index=True)
    country_confidence = models.FloatField(null=True, blank=True)
    country_source = models.CharField(max_length=32, blank=True, db_index=True)
    scope_status = models.CharField(
        max_length=32,
        choices=ScopeStatus.choices,
        default=ScopeStatus.UNSCOPED,
        db_index=True,
    )
    scope_reason = models.CharField(max_length=128, blank=True)
    is_priority = models.BooleanField(default=False, db_index=True)
    last_scope_evaluated_at = models.DateTimeField(null=True, blank=True)

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
    description_clean = models.TextField(blank=True)
    description_raw_html = models.TextField(blank=True)
    has_html_content = models.BooleanField(default=False, db_index=True)
    cleaning_version = models.CharField(max_length=20, blank=True, default="v2")
    requirements = models.TextField(blank=True)
    responsibilities = models.TextField(blank=True)
    benefits = models.TextField(blank=True)

    # ── Dates ─────────────────────────────────────────────────────────────────
    posted_date = models.DateField(null=True, blank=True)
    closing_date = models.DateField(null=True, blank=True)

    # ── Platform meta ─────────────────────────────────────────────────────────
    platform_slug = models.CharField(max_length=64, blank=True)
    vendor_job_identification = models.CharField(max_length=128, blank=True)
    vendor_job_category = models.CharField(max_length=128, blank=True)
    vendor_degree_level = models.CharField(max_length=128, blank=True)
    vendor_job_schedule = models.CharField(max_length=128, blank=True)
    vendor_job_shift = models.CharField(max_length=128, blank=True)
    vendor_location_block = models.CharField(max_length=512, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    list_payload_json = models.JSONField(null=True, blank=True)

    # ── Selective harvest role filter (Tier 1 — title gate) ─────────────────
    role_category = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    filter_decision = models.CharField(max_length=16, null=True, blank=True, db_index=True)
    filter_reason = models.CharField(max_length=512, null=True, blank=True)
    filter_snapshot_id = models.UUIDField(null=True, blank=True, db_index=True)
    is_cold = models.BooleanField(default=False, db_index=True)
    jd_fetch_skipped = models.BooleanField(default=False)
    # Tier-1 gate outputs (confidence-aware title classification)
    title_gate_decision = models.CharField(
        max_length=16, null=True, blank=True, db_index=True,
        help_text="HARD_YES | HARD_NO | AMBIGUOUS — output of confidence-aware title gate (Tier 1).",
    )
    title_gate_confidence = models.FloatField(
        null=True, blank=True,
        help_text="0.0–1.0 phrase-match confidence score from Tier-1 title gate.",
    )

    # ── JD content gate (Tier 2 — LLM relevance gate) ───────────────────────
    # Tier-2 gate runs on AMBIGUOUS jobs; reads a JD snippet and makes a
    # binary YES/NO decision via LLM before committing to a full JD fetch.
    jd_gate_decision = models.CharField(
        max_length=16, null=True, blank=True, db_index=True,
        help_text="CONFIRMED | REJECTED | UNCERTAIN | SKIPPED | PENDING — Tier-2 LLM content gate.",
    )
    jd_gate_confidence = models.FloatField(
        null=True, blank=True,
        help_text="0.0–1.0 LLM confidence score from Tier-2 JD gate.",
    )
    jd_gate_reason = models.TextField(
        blank=True,
        help_text="LLM-provided one-sentence reason for the gate decision (for audit).",
    )
    jd_gate_snippet = models.TextField(
        blank=True,
        help_text="First 800 chars of clean JD text used for the gate decision (for audit/debug).",
    )
    jd_gate_model = models.CharField(
        max_length=64, blank=True,
        help_text="LLM model used for JD gate (e.g. gpt-4o-mini).",
    )
    jd_gate_category = models.CharField(
        max_length=64, blank=True,
        help_text="Tech-category hint from JD gate LLM (e.g. devops, mlops). Pre-hints enrichment.",
    )
    jd_gate_ran_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp when the JD content gate was last evaluated for this job.",
    )

    # ── Enriched: skills & tech ───────────────────────────────────────────────
    # All extracted skills (tech + soft); populated by enrichments.extract_enrichments()
    skills = models.JSONField(default=list, blank=True)
    # Subset of skills that are programming languages / frameworks / tools
    tech_stack = models.JSONField(default=list, blank=True)
    # Job function category (Engineering, Data & Analytics, Product, etc.)
    job_category = models.CharField(max_length=64, blank=True)
    # Domain slug — maps directly to a MarketingRole.slug (e.g. "servicenow-developer")
    # Set by detect_job_domain() in enrichments.py; used to auto-assign Job.marketing_roles on sync.
    job_domain = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        help_text="MarketingRole slug auto-assigned by domain classification engine",
    )
    job_domain_candidates = models.JSONField(
        default=list,
        blank=True,
        help_text="Ordered candidate MarketingRole slugs considered during domain routing.",
    )
    # Version tag so we can re-classify when _DOMAIN_PATTERNS changes
    domain_version = models.CharField(max_length=16, blank=True, default="")

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
    clearance_level = models.CharField(max_length=64, blank=True)

    # ── Enriched: compensation extras ────────────────────────────────────────
    salary_equity = models.BooleanField(default=False)
    signing_bonus = models.BooleanField(default=False)
    relocation_assistance = models.BooleanField(default=False)

    # ── Enriched: work conditions ─────────────────────────────────────────────
    # e.g. "up to 25%", "occasional", "extensive"
    travel_required = models.CharField(max_length=64, blank=True)
    travel_pct_min = models.PositiveSmallIntegerField(null=True, blank=True)
    travel_pct_max = models.PositiveSmallIntegerField(null=True, blank=True)
    schedule_type = models.CharField(max_length=32, blank=True)
    shift_schedule = models.CharField(max_length=128, blank=True)
    shift_details = models.CharField(max_length=255, blank=True)
    hours_hint = models.CharField(max_length=64, blank=True)
    weekend_required = models.BooleanField(null=True, blank=True)

    # ── Enriched: structured lists ────────────────────────────────────────────
    certifications = models.JSONField(default=list, blank=True)
    licenses_required = models.JSONField(default=list, blank=True)
    benefits_list = models.JSONField(default=list, blank=True)
    languages_required = models.JSONField(default=list, blank=True)
    encouraged_to_apply = models.JSONField(default=list, blank=True)
    job_keywords = models.JSONField(default=list, blank=True)
    title_keywords = models.JSONField(default=list, blank=True)

    # ── Denormalized company context (for fast Raw Jobs filtering) ───────────
    company_industry = models.CharField(max_length=255, blank=True)
    company_stage = models.CharField(max_length=64, blank=True)
    company_funding = models.CharField(max_length=128, blank=True)
    company_size = models.CharField(max_length=64, blank=True)
    company_employee_count_band = models.CharField(max_length=64, blank=True)
    company_founding_year = models.PositiveSmallIntegerField(null=True, blank=True)

    # ── Enriched: quality signals ─────────────────────────────────────────────
    word_count = models.PositiveIntegerField(default=0)
    # 0.0–1.0: fraction of key fields populated (description, salary, location…)
    quality_score = models.FloatField(null=True, blank=True)
    jd_quality_score = models.FloatField(null=True, blank=True)
    classification_confidence = models.FloatField(null=True, blank=True)
    # Separate signal: how confident is the *category* detection specifically
    # (title match → 0.92, desc-only → 0.72, both agree → 0.97, no match → 0.0)
    # Used by gating trust score instead of the broken field-completeness average.
    category_confidence = models.FloatField(null=True, blank=True)
    enrichment_version = models.CharField(max_length=16, default="v3", blank=True)
    classification_source = models.CharField(max_length=16, blank=True)
    classification_provenance = models.JSONField(default=dict, blank=True)
    field_confidence = models.JSONField(default=dict, blank=True)
    field_provenance = models.JSONField(default=dict, blank=True)
    resume_ready_score = models.FloatField(null=True, blank=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    is_test_run = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True for smoke/test harvest rows that must stay out of production backlog counts.",
    )
    sync_status = models.CharField(
        max_length=16, choices=SyncStatus.choices, default=SyncStatus.PENDING
    )
    sync_skip_reason = models.CharField(
        max_length=32,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Gate reason code when sync_status=SKIPPED. "
            "Mirrors jobs.gating reason codes: INACTIVE_POSTING, JD_TOO_WEAK, "
            "PLATFORM_MISMATCH, DUPLICATE_RISK, COMPANY_UNRESOLVED, etc."
        ),
    )
    is_active = models.BooleanField(default=True)
    # Denormalized flag — set on every save so JD filter hits an index instead of
    # running Length(Trim(Coalesce(description))) over 100k+ rows.
    has_description = models.BooleanField(default=False, db_index=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # Parallel JD backfill: set while a worker holds the row (cleared when done).
    # Stale locks are reclaimed after BACKFILL_LOCK_STALE_MINUTES in tasks.
    jd_backfill_locked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-fetched_at"]
        indexes = [
            # Single-column
            models.Index(fields=["company", "platform_slug"]),
            models.Index(fields=["platform_slug"]),
            models.Index(fields=["sync_status"]),
            models.Index(fields=["is_test_run"]),
            models.Index(fields=["fetched_at"],       name="harvest_raw_fetched_idx"),
            models.Index(fields=["is_remote"],         name="harvest_raw_remote_idx"),
            models.Index(fields=["has_description"],   name="harvest_raw_hasdesc_idx"),
            models.Index(fields=["filter_decision"]),
            models.Index(fields=["is_cold"]),
            models.Index(fields=["posted_date"]),
            models.Index(fields=["employment_type"]),
            models.Index(fields=["location_type"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["job_category"]),
            models.Index(fields=["job_domain"]),
            models.Index(fields=["country_code"]),
            models.Index(fields=["scope_status"]),
            models.Index(fields=["is_priority"]),
            models.Index(fields=["normalized_title"]),
            models.Index(fields=["department_normalized"]),
            models.Index(fields=["country"]),
            models.Index(fields=["state"]),
            models.Index(fields=["years_required"]),
            models.Index(fields=["education_required"]),
            models.Index(fields=["visa_sponsorship"]),
            models.Index(fields=["clearance_required"]),
            models.Index(fields=["clearance_level"]),
            models.Index(fields=["schedule_type"]),
            models.Index(fields=["weekend_required"]),
            models.Index(fields=["shift_schedule"]),
            models.Index(fields=["travel_pct_min"]),
            models.Index(fields=["travel_pct_max"]),
            models.Index(fields=["company_industry"]),
            models.Index(fields=["company_size"]),
            models.Index(fields=["company_employee_count_band"]),
            models.Index(fields=["company_founding_year"]),
            models.Index(fields=["quality_score"]),
            models.Index(fields=["jd_quality_score"]),
            models.Index(fields=["classification_confidence"]),
            models.Index(fields=["resume_ready_score"]),
            models.Index(fields=["has_html_content"]),
            # Composite — filter + default ORDER BY fetched_at DESC
            models.Index(fields=["sync_status",    "-fetched_at"], name="harvest_raw_sync_fetched_idx"),
            models.Index(fields=["is_test_run",    "-fetched_at"], name="harvest_raw_test_fetched_idx"),
            models.Index(fields=["is_active",      "-fetched_at"], name="harvest_raw_active_fetched_idx"),
            models.Index(fields=["is_priority",    "-fetched_at"], name="raw_priority_fetched_idx"),
            models.Index(fields=["scope_status",   "-fetched_at"], name="raw_scope_fetched_idx"),
            models.Index(fields=["is_remote",      "-fetched_at"], name="harvest_raw_remote_fetched_idx"),
            models.Index(fields=["has_description","-fetched_at"], name="harvest_raw_hd_fetched_idx"),
            models.Index(fields=["platform_slug",  "-fetched_at"], name="harvest_raw_plat_fetched_idx"),
            models.Index(fields=["posted_date"],                    name="harvest_raw_posted_idx"),
            # Compound index for backfill eligibility queries
            models.Index(fields=["has_description", "jd_backfill_locked_at"], name="harvest_raw_has_desc_lock_idx"),
        ]
        verbose_name = "Raw Job"
        verbose_name_plural = "Raw Jobs"

    def has_meaningful_description(self) -> bool:
        """True when stored description has more than trivial whitespace (matches Jobs Browser)."""
        return len((self.description or "").strip()) > 1

    def save(self, *args, **kwargs):
        self.has_description = self.has_meaningful_description()
        super().save(*args, **kwargs)

    def is_expired_listing(self) -> bool:
        """
        Best-effort: job is no longer open (closed date passed, explicit expiry, delisted,
        ATS says inactive, or posting is very stale with no JD text).

        Keep in sync with ``raw_jobs_missing_jd_expired_count`` in ``harvest.views``.
        """
        from django.conf import settings
        from django.utils import timezone

        now = timezone.now()
        today = now.date()
        if self.expires_at and self.expires_at < now:
            return True
        if self.closing_date and self.closing_date < today:
            return True
        if not self.is_active:
            return True

        payload = self.raw_payload if isinstance(self.raw_payload, dict) else {}
        # SmartRecruiters (and similar) detail API: {"active": false}
        if payload.get("active") is False:
            return True

        stale_days = max(30, int(getattr(settings, "HARVEST_JD_STALE_DAYS", 120)))
        if self.posted_date and self.posted_date < today - timedelta(days=stale_days):
            return True

        return False

    def jd_browser_label(self) -> str:
        """Badge key for harvest UI: 'yes' | 'no' | 'expired'."""
        if self.has_meaningful_description():
            return "yes"
        if self.is_expired_listing():
            return "expired"
        return "no"

    def resume_jd_gate(self) -> dict:
        """Resume-generation JD gate with reason and thresholds."""
        from .jd_gate import evaluate_raw_job_resume_gate

        cache_attr = "_resume_jd_gate_cache"
        cached = getattr(self, cache_attr, None)
        if cached is not None:
            return cached
        gate = evaluate_raw_job_resume_gate(self).asdict()
        setattr(self, cache_attr, gate)
        return gate

    def is_resume_jd_usable(self) -> bool:
        return bool(self.resume_jd_gate().get("usable"))

    def pipeline_stage_label(self) -> str:
        """
        Coarse pipeline stage used by Raw Jobs workflow board.

        Flow: Fetched -> Parsed -> Enriched -> Classified -> Ready -> Synced
        """
        effective_conf = (
            self.category_confidence
            if self.category_confidence is not None
            else self.classification_confidence
        ) or 0
        from .runtime_config import get_ready_stage_min_confidence

        if self.sync_status == self.SyncStatus.SYNCED:
            return "SYNCED"
        if self.sync_status == self.SyncStatus.FAILED:
            return "FAILED"
        if self.sync_status == self.SyncStatus.SKIPPED:
            return "DUPLICATE"
        if self.is_cold or self.jd_fetch_skipped or self.filter_decision in {"COLD", "NO_MATCH"}:
            return "FILTERED OUT"
        weak_domains = {
            "",
            "general-business",
            "marketing-specialist",
            "other-generalist",
            "uncategorized",
            "unknown",
        }
        filter_allows_pool = self.filter_decision in {None, "", "STRONG", "POSSIBLE"}
        if (
            not self.is_test_run
            and self.has_description
            and self.is_resume_jd_usable()
            and effective_conf >= get_ready_stage_min_confidence()
            and self.is_active
            and filter_allows_pool
            and (self.job_domain or "").strip().lower() not in weak_domains
        ):
            return "READY"
        if effective_conf > 0:
            return "CLASSIFIED"
        if self.quality_score is not None or self.jd_quality_score is not None:
            return "ENRICHED"
        if self.has_description:
            return "PARSED"
        return "FETCHED"

    def owner_pipeline_label(self) -> str:
        slug = (self.platform_slug or "").lower()
        if slug in {"jarvis"}:
            return "Jarvis"
        if slug in {"workday", "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "bamboohr", "dayforce"}:
            return "API"
        return "Scraper"

    def retry_count_estimate(self) -> int:
        payload = self.raw_payload if isinstance(self.raw_payload, dict) else {}
        for key in ("retry_count", "retries", "attempts"):
            val = payload.get(key)
            if isinstance(val, int) and val >= 0:
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)
        return 0

    def last_error_text(self) -> str:
        payload = self.raw_payload if isinstance(self.raw_payload, dict) else {}
        for key in ("last_error", "error_message", "error", "sync_error"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:220]
        if self.sync_status == self.SyncStatus.FAILED:
            return "Sync to pool failed"
        return ""

    def __str__(self):
        return f"{self.title} @ {self.company_name}"


class HarvestSkippedTitle(models.Model):
    raw_job = models.ForeignKey(
        RawJob,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="skip_log",
    )
    company_name = models.CharField(max_length=256)
    platform_slug = models.CharField(max_length=64)
    job_title = models.CharField(max_length=512)
    job_external_id = models.CharField(max_length=256, blank=True)
    department = models.CharField(max_length=256, blank=True)
    filter_decision = models.CharField(max_length=16)
    filter_reason = models.CharField(max_length=512)
    matched_negative = models.CharField(max_length=256, blank=True)
    snapshot_id = models.UUIDField(null=True, blank=True)
    batch_id = models.IntegerField(null=True, blank=True)
    skipped_at = models.DateTimeField(auto_now_add=True)
    is_sampled = models.BooleanField(default=False)

    class Meta:
        ordering = ["-skipped_at"]
        indexes = [
            models.Index(fields=["skipped_at"]),
            models.Index(fields=["platform_slug"]),
            models.Index(fields=["filter_decision"]),
            models.Index(fields=["is_sampled"]),
        ]
        verbose_name = "Harvest skipped title"
        verbose_name_plural = "Harvest skipped titles"

    def __str__(self):
        return f"{self.filter_decision}: {self.job_title}"


class RawJobPayloadSnapshot(models.Model):
    """Immutable vendor/source evidence captured before normalization/classification."""

    class PayloadKind(models.TextChoices):
        LIST = "list", "List payload"
        DETAIL = "detail", "Detail payload"
        JSONLD = "jsonld", "JSON-LD"
        HTML = "html", "Raw HTML"
        API_RESPONSE = "api_response", "API response"
        BACKFILL = "backfill", "Legacy backfill"
        FAILURE = "failure", "Failure metadata"

    raw_job = models.ForeignKey(
        RawJob,
        on_delete=models.CASCADE,
        related_name="payload_snapshots",
    )
    fetch_batch = models.ForeignKey(
        FetchBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payload_snapshots",
    )
    platform_slug = models.CharField(max_length=64, blank=True, db_index=True)
    source_url = models.URLField(max_length=1024, blank=True)
    payload_kind = models.CharField(
        max_length=16,
        choices=PayloadKind.choices,
        default=PayloadKind.API_RESPONSE,
        db_index=True,
    )
    schema_version = models.CharField(max_length=24, blank=True, default="source-v1")
    payload = models.JSONField(default=dict, blank=True)
    raw_html_gzip = models.BinaryField(null=True, blank=True, editable=False)
    content_hash = models.CharField(max_length=64, db_index=True)
    payload_size_bytes = models.PositiveIntegerField(default=0)
    raw_html_size_bytes = models.PositiveIntegerField(default=0)
    redaction_version = models.CharField(max_length=16, blank=True, default="v1")
    source_metadata = models.JSONField(default=dict, blank=True)
    is_failure = models.BooleanField(default=False, db_index=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-captured_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["raw_job", "payload_kind", "content_hash"],
                name="uniq_rawjob_payload_kind_hash",
            )
        ]
        indexes = [
            models.Index(fields=["raw_job", "-captured_at"], name="raw_payload_job_time_idx"),
            models.Index(fields=["platform_slug", "payload_kind"], name="raw_payload_platform_kind_idx"),
        ]
        verbose_name = "Raw Job Payload Snapshot"
        verbose_name_plural = "Raw Job Payload Snapshots"

    @property
    def raw_html(self) -> str:
        if not self.raw_html_gzip:
            return ""
        try:
            return gzip.decompress(bytes(self.raw_html_gzip)).decode("utf-8", errors="replace")
        except Exception:
            return ""

    @property
    def raw_html_preview(self) -> str:
        html = self.raw_html
        if len(html) > 50_000:
            return f"{html[:50_000]}...[TRUNCATED PREVIEW]"
        return html

    @property
    def size_label(self) -> str:
        total = int(self.payload_size_bytes or 0) + int(self.raw_html_size_bytes or 0)
        if total >= 1024 * 1024:
            return f"{total / (1024 * 1024):.1f} MB"
        if total >= 1024:
            return f"{total / 1024:.1f} KB"
        return f"{total} B"

    def __str__(self):
        return f"{self.raw_job_id} {self.payload_kind} {self.content_hash[:10]}"


# ── Duplicate Detection ───────────────────────────────────────────────────────

class DuplicateLabel(models.TextChoices):
    EXACT            = 'EXACT',            'Exact Duplicate'
    STRONG_MATCH     = 'STRONG_MATCH',     'Strong Match'
    URL_DUPLICATE    = 'URL_DUPLICATE',    'URL Duplicate'
    REQUISITION      = 'REQUISITION',      'Requisition Duplicate'
    NEAR_DUPLICATE   = 'NEAR_DUPLICATE',   'Near Duplicate'
    LOCATION_VARIANT = 'LOCATION_VARIANT', 'Location Variant'
    REPOST           = 'REPOST',           'Repost'
    AGENCY_DUP       = 'AGENCY_DUP',       'Agency Duplicate'
    NOT_DUPLICATE    = 'NOT_DUPLICATE',    'Not Duplicate'


class DuplicateResolution(models.TextChoices):
    PENDING   = 'PENDING',   'Pending Review'
    MERGED    = 'MERGED',    'Merged'
    DISMISSED = 'DISMISSED', 'Keep Both'
    CONFIRMED = 'CONFIRMED', 'Confirmed Duplicate'


class RawJobDuplicatePair(models.Model):
    primary   = models.ForeignKey(
        RawJob, on_delete=models.CASCADE, related_name='duplicate_as_primary',
    )
    duplicate = models.ForeignKey(
        RawJob, on_delete=models.CASCADE, related_name='duplicate_as_secondary',
    )
    label      = models.CharField(max_length=32, choices=DuplicateLabel.choices, db_index=True)
    similarity = models.FloatField(default=0.0)
    method     = models.CharField(max_length=64, blank=True)
    resolution = models.CharField(
        max_length=16, choices=DuplicateResolution.choices,
        default=DuplicateResolution.PENDING, db_index=True,
    )
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
    )
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ('primary', 'duplicate')
        ordering = ['-detected_at']
        indexes = [
            models.Index(fields=['resolution', '-detected_at'], name='dup_res_detected_idx'),
            models.Index(fields=['label', '-detected_at'],      name='dup_label_detected_idx'),
        ]

    def __str__(self):
        return f"{self.get_label_display()}: #{self.primary_id} ↔ #{self.duplicate_id}"


class PlatformEngineConfig(models.Model):
    """Per-platform runtime config — replaces hardcoded `_NEEDS_BACKFILL` list and sleep delays.

    One row per JobBoardPlatform. Edits take effect on next task run, no deploy needed.
    """
    platform = models.OneToOneField(
        JobBoardPlatform, on_delete=models.CASCADE, related_name='config',
    )
    auto_backfill = models.BooleanField(
        default=False,
        help_text="If true, fetch auto-queues JD backfill for new jobs on this platform.",
    )
    backfill_priority = models.PositiveSmallIntegerField(
        default=5,
        help_text="1 = highest, 10 = lowest. Backfill workers dequeue lowest number first.",
    )
    fetch_cadence_hours = models.PositiveIntegerField(
        default=24,
        help_text="Minimum hours between per-company fetches on this platform.",
    )
    inter_request_delay_ms = models.PositiveIntegerField(
        default=1500,
        help_text="Delay between consecutive requests (APIs ~1500, scrapers ~5000).",
    )
    min_quality_score = models.FloatField(
        default=0.3,
        help_text="Jobs below this score are auto-archived after enrichment.",
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Platform Config"
        ordering = ['platform__name']

    def __str__(self):
        return f"Config[{self.platform.slug}]"


class HarvestEngineConfig(models.Model):
    """
    Singleton — harvest engine runtime configuration.
    One row (pk=1) always exists; editable from the GUI at /harvest/engine/.
    All task code reads from this — zero hardcoded values.

    Changes to task_rate_limit are broadcast to running Celery workers immediately.
    Changes to worker_concurrency require restarting the celery_harvest container
    (set HARVEST_CONCURRENCY in .env.production, then docker compose restart celery_harvest).
    All other fields take effect on the next batch run.
    """

    # ── Worker-level concurrency ──────────────────────────────────────────────
    worker_concurrency = models.PositiveSmallIntegerField(
        default=2,
        verbose_name="Worker concurrency",
        help_text=(
            "How many company-fetch tasks run in parallel per worker. "
            "Matches the --concurrency flag on the celery_harvest container. "
            "Rule of thumb: CPU count for CPU-heavy workloads, 2×CPU for I/O-heavy. "
            "Change HARVEST_CONCURRENCY in .env.production and restart celery_harvest to apply."
        ),
    )

    # ── Task rate limit (applies LIVE via Celery broadcast on save) ───────────
    task_rate_limit = models.PositiveSmallIntegerField(
        default=3,
        verbose_name="Tasks per worker per minute",
        help_text=(
            "Max fetch tasks each worker runs per minute. "
            "Total throughput = this × worker_concurrency. "
            "Applied immediately to running workers — no restart needed."
        ),
    )

    # ── Batch dispatch stagger ────────────────────────────────────────────────
    api_stagger_ms = models.PositiveIntegerField(
        default=1000,
        verbose_name="API platform stagger (ms)",
        help_text=(
            "Milliseconds between queuing tasks for JSON-API platforms "
            "(Greenhouse, Lever, Workday …). Lower = faster queue fill. "
            "Takes effect on next batch run."
        ),
    )
    scraper_stagger_ms = models.PositiveIntegerField(
        default=5000,
        verbose_name="Scraper platform stagger (ms)",
        help_text=(
            "Milliseconds between queuing tasks for HTML-scraper platforms "
            "(iCIMS, Taleo, Jobvite …). Keep ≥1000 to avoid hammering slow sites. "
            "Takes effect on next batch run."
        ),
    )

    # ── Freshness guard ───────────────────────────────────────────────────────
    min_hours_since_fetch = models.PositiveSmallIntegerField(
        default=6,
        verbose_name="Min hours between re-fetches",
        help_text=(
            "Skip any company that was successfully fetched within this many hours. "
            "Set to 0 to force re-fetch everything every run. "
            "Takes effect on next batch run."
        ),
    )

    # ── Per-task timeouts ─────────────────────────────────────────────────────
    task_soft_time_limit_secs = models.PositiveSmallIntegerField(
        default=480,
        verbose_name="Soft time limit (seconds)",
        help_text=(
            "A single company-fetch task that runs longer than this gets a graceful "
            "shutdown signal (marks run as PARTIAL). Hard kill fires 120s later."
        ),
    )
    portal_health_failure_threshold = models.PositiveSmallIntegerField(
        default=2,
        verbose_name="Portal health failure threshold",
        help_text=(
            "Consecutive failed portal checks required before a company portal is "
            "marked down. Prevents one transient 5xx from locking out a company."
        ),
    )

    # ── Auto-pipeline funnel toggles ──────────────────────────────────────────
    # When all three are True (the default), pressing "Fetch All" runs the full
    # pipeline automatically: fetch → JD backfill → enrich → sync to pool.
    # Disable individual steps here without touching code.
    auto_backfill_jd = models.BooleanField(
        default=True,
        verbose_name="Auto JD backfill",
        help_text=(
            "After each company fetch, automatically queue a description backfill "
            "for new jobs that have no JD yet. Fires 30s after harvest."
        ),
    )
    auto_enrich = models.BooleanField(
        default=True,
        verbose_name="Auto enrich",
        help_text=(
            "After a full batch completes, automatically run enrichment (skills, "
            "category, experience level …) on all new unenriched jobs. "
            "Fires 2 min after the last company task finishes."
        ),
    )
    auto_sync_to_pool = models.BooleanField(
        default=True,
        verbose_name="Auto sync to pool",
        help_text=(
            "After enrichment, automatically promote enriched jobs with real "
            "descriptions into the Vet Queue (Job Pool). "
            "Fires 5 min after the last company task finishes."
        ),
    )

    # ── Resume-JD gate thresholds (live, no restart) ─────────────────────────
    resume_jd_min_words = models.PositiveSmallIntegerField(
        default=80,
        verbose_name="Resume JD minimum words",
        help_text=(
            "Minimum cleaned JD word count required before a raw job is considered "
            "resume-usable."
        ),
    )
    resume_jd_min_chars = models.PositiveSmallIntegerField(
        default=400,
        verbose_name="Resume JD minimum characters",
        help_text=(
            "Minimum cleaned JD character length required before a raw job is considered "
            "resume-usable."
        ),
    )
    resume_jd_min_classification_confidence = models.FloatField(
        default=0.35,
        verbose_name="Resume JD minimum classification confidence",
        help_text=(
            "Minimum classification confidence (0-1) required for resume-ready gating."
        ),
    )
    ready_stage_min_confidence = models.FloatField(
        default=0.55,
        verbose_name="Ready stage minimum confidence",
        help_text=(
            "Minimum effective classification confidence (0-1) required before a RawJob "
            "is considered READY in pipeline analytics and queue counts."
        ),
    )
    legacy_hash_bridge_enabled = models.BooleanField(
        default=True,
        verbose_name="Legacy SHA256 URL hash bridge",
        help_text=(
            "Temporarily reconcile old SHA256 url_hash rows during upsert. Turn off "
            "after the historical hash migration/backfill has completed."
        ),
    )
    jd_backfill_lock_stale_minutes = models.PositiveSmallIntegerField(
        default=15,
        verbose_name="JD backfill stale lock minutes",
        help_text=(
            "If a JD backfill worker crashes, locks older than this are reclaimed. "
            "Keep above the longest normal single-job fetch duration."
        ),
    )

    # ── Scoped harvest controls ──────────────────────────────────────────────
    target_countries = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Target countries",
        help_text="ISO country codes that should receive full processing. Empty uses US, IN, CA, GB, AU.",
    )
    process_unknown_country_with_target_domain = models.BooleanField(
        default=True,
        verbose_name="Process unknown country if domain is target",
        help_text=(
            "Keep unknown-location jobs in review unless the title/domain strongly matches "
            "a target IT/engineering route."
        ),
    )
    rescope_on_target_country_change = models.BooleanField(
        default=True,
        verbose_name="Re-scope cold jobs when target countries change",
        help_text=(
            "Queue a safe background pass over cold/review RawJobs when the target-country "
            "list changes so newly enabled markets do not stay cold forever."
        ),
    )
    geocoding_cache_enabled = models.BooleanField(
        default=True,
        verbose_name="Location cache enabled",
        help_text="Read/write normalized location resolutions before any provider lookup.",
    )
    geocoding_provider_enabled = models.BooleanField(
        default=False,
        verbose_name="Provider fallback enabled",
        help_text="Allow external geocoding only for unresolved unique locations.",
    )
    geocoding_provider = models.CharField(
        max_length=16,
        default="none",
        choices=[("none", "None"), ("mapbox", "Mapbox"), ("google", "Google")],
        verbose_name="Geocoding provider",
        help_text="External fallback provider. Token must come from environment, never the database.",
    )
    geocoding_monthly_limit = models.PositiveIntegerField(
        default=80000,
        verbose_name="Provider monthly hard limit",
        help_text="Hard stop for provider requests per calendar month. Default 80k to stay below 100k free-tier claims.",
    )
    geocoding_hourly_limit = models.PositiveIntegerField(
        default=1000,
        verbose_name="Provider hourly hard limit",
        help_text="Hard stop for provider requests per hour so one bad harvest cannot burn the month in a spike.",
    )
    geocoding_warning_pct = models.PositiveSmallIntegerField(
        default=80,
        verbose_name="Provider warning threshold percent",
        help_text="Log a warning when monthly or hourly provider usage reaches this percentage.",
    )
    geocoding_provider_token = models.CharField(
        max_length=512,
        blank=True,
        default="",
        verbose_name="Geocoding provider token (DB)",
        help_text=(
            "Optional API token stored in DB so it can be rotated from the GUI. "
            "When blank, the resolver falls back to MAPBOX_ACCESS_TOKEN / "
            "GOOGLE_MAPS_API_KEY environment variables. "
            "Env var is the more secure pattern; DB storage exists for convenience."
        ),
    )

    # ── Full-fetch cooldown ───────────────────────────────────────────────────
    full_fetch_cooldown_minutes = models.PositiveSmallIntegerField(
        default=360,
        verbose_name="Full fetch cooldown (minutes)",
        help_text=(
            "Minimum minutes between two Full Crawl runs. "
            "Enforced both in the UI and in the task via a cache key so direct API "
            "calls also respect the limit. Default 120 (2 h)."
        ),
    )

    # ── JD backfill controls ──────────────────────────────────────────────────
    backfill_jd_workers = models.PositiveSmallIntegerField(
        default=1,
        verbose_name="JD backfill parallel workers",
        help_text=(
            "Concurrent chunk-worker threads for description backfill. "
            "Hard-capped at 1 internally on this deployment to protect web uptime."
        ),
    )
    backfill_jd_reset_locks = models.BooleanField(
        default=True,
        verbose_name="Reset stale JD locks by default",
        help_text=(
            "When True the Backfill JD button automatically clears locks older "
            "than BACKFILL_LOCK_STALE_MINUTES before claiming rows. "
            "Prevents stale worker-crash locks from silently blocking re-runs."
        ),
    )
    backfill_jd_include_cold = models.BooleanField(
        default=False,
        verbose_name="Include COLD / REVIEW jobs in JD backfill",
        help_text=(
            "When False (default) only PRIORITY (target-country) jobs get JD backfill. "
            "Enable to also fetch descriptions for COLD and REVIEW_* jobs — "
            "useful after expanding target_countries or for full-coverage audits."
        ),
    )

    # ── Link validation controls ──────────────────────────────────────────────
    validate_links_include_synced = models.BooleanField(
        default=False,
        verbose_name="Validate links for SYNCED pool jobs too",
        help_text=(
            "When False (default) the Validate Live Links button only checks jobs "
            "still in PENDING sync state. Enable to also re-validate jobs already "
            "promoted to the pool — catches URLs that expired after sync."
        ),
    )
    validate_links_recent_hours = models.PositiveSmallIntegerField(
        default=168,
        verbose_name="Validate links — recent hours window",
        help_text=(
            "Only check jobs fetched within this many hours. "
            "Default 168 (7 days). Set 0 to validate all active jobs regardless of age."
        ),
    )

    # ── Selective harvest role filter (Tier 1 — title gate) ──────────────────
    selective_filter_enabled = models.BooleanField(
        default=False,
        verbose_name="Selective role filter enabled",
        help_text="Master switch. When False, harvest keeps the existing full-storage behavior.",
    )
    filter_audit_mode = models.BooleanField(
        default=True,
        verbose_name="Selective filter audit mode",
        help_text="When True, classify and audit decisions without skipping JD fetch/backfill.",
    )
    pre_storage_filter_enabled = models.BooleanField(
        default=False,
        verbose_name="Pre-storage filter (selective fetch)",
        help_text=(
            "When True, HARD_NO titles (NO_MATCH or COLD with confidence < 0.2) are dropped "
            "BEFORE writing to DB — only relevant jobs are ever stored. "
            "Requires selective_filter_enabled=True and filter_audit_mode=False. "
            "Enable only after verifying false-negative rate in audit mode."
        ),
    )
    filter_full_crawl = models.BooleanField(
        default=False,
        verbose_name="Enforce filter during full crawls",
        help_text=(
            "By default, full-crawl fetches (fetch_all=True) run in filter audit mode — jobs are "
            "classified but nothing is suppressed, so admin bulk imports are always complete. "
            "When this flag is True, the selective filter enforces (drops HARD_NO jobs) even "
            "during full crawls. Enable for selective harvesting from day one."
        ),
    )
    zero_tech_threshold = models.PositiveSmallIntegerField(default=5)
    zero_tech_skip_ttl_days = models.PositiveSmallIntegerField(default=30)
    cold_no_match_sample_rate_pct = models.PositiveSmallIntegerField(default=5)
    hard_negative_phrases = models.JSONField(default=list, blank=True)
    title_hard_yes_confidence = models.FloatField(
        default=0.80,
        verbose_name="Title gate — HARD_YES confidence threshold",
        help_text=(
            "Min phrase-match confidence to classify a title as HARD_YES (skips JD gate, goes straight to backfill). "
            "Below this threshold → AMBIGUOUS (sent to Tier-2 JD gate). "
            "Range 0.0–1.0. Default 0.80."
        ),
    )

    # ── JD content gate (Tier 2 — LLM relevance gate) ───────────────────────
    # The JD gate intercepts AMBIGUOUS jobs BEFORE committing to full JD fetch.
    # A JD snippet (800 chars) is run through an LLM binary YES/NO prompt.
    # CONFIRMED → full JD backfill. REJECTED → stored cheaply, no further work.
    # Deploy with jd_gate_enabled=False first; enable jd_gate_audit_mode to tune.
    jd_gate_enabled = models.BooleanField(
        default=False,
        verbose_name="JD content gate enabled (Tier 2)",
        help_text=(
            "Master switch for the JD relevance gate. Safe to deploy with False — does nothing. "
            "Enable jd_gate_audit_mode first to tune thresholds before enforcement."
        ),
    )
    jd_gate_audit_mode = models.BooleanField(
        default=True,
        verbose_name="JD gate — audit mode (no suppression)",
        help_text=(
            "When True: gate runs and records decisions (jd_gate_decision, jd_gate_reason) "
            "but does NOT suppress any jobs. Use this for 2–4 weeks to tune thresholds. "
            "Set False only after validating false-negative rate."
        ),
    )
    jd_gate_model = models.CharField(
        max_length=64,
        default="gpt-4o-mini",
        verbose_name="JD gate — LLM model",
        help_text="OpenAI model for JD content gate. gpt-4o-mini is cheapest/fastest (~$0.001 per 20 jobs).",
    )
    jd_gate_confidence_threshold = models.FloatField(
        default=0.65,
        verbose_name="JD gate — confidence threshold",
        help_text=(
            "Min LLM confidence to enforce a YES/NO decision. "
            "Below this → UNCERTAIN (human review queue, ~2–3% of cases). "
            "Range 0.0–1.0. Default 0.65."
        ),
    )
    jd_gate_scope = models.CharField(
        max_length=32,
        default="ambiguous_only",
        verbose_name="JD gate — scope",
        choices=[
            ("ambiguous_only", "AMBIGUOUS titles only (safest — recommended start)"),
            ("all_possible", "AMBIGUOUS + COLD-with-tech-signal (catch more false negatives)"),
            ("all_non_hard_no", "Everything except HARD_NO (maximum accuracy, highest cost)"),
        ],
        help_text=(
            "Which jobs go through the Tier-2 JD content gate. "
            "Start with 'ambiguous_only' to validate, then expand scope."
        ),
    )
    jd_gate_batch_size = models.PositiveSmallIntegerField(
        default=20,
        verbose_name="JD gate — LLM batch size",
        help_text="Jobs per LLM API call in JD gate. 20 is the sweet spot for cost vs latency.",
    )
    jd_gate_snippet_chars = models.PositiveSmallIntegerField(
        default=800,
        verbose_name="JD gate — snippet length (chars)",
        help_text=(
            "Max chars of clean JD text sent to the LLM gate per job. "
            "800 chars (~130 words) is enough to identify tech vs non-tech reliably. "
            "Increase for borderline roles; decrease to reduce token cost."
        ),
    )

    # ── Cleanup controls ──────────────────────────────────────────────────────
    cleanup_inactive_age_days = models.PositiveSmallIntegerField(
        default=7,
        verbose_name="Cleanup — inactive row max age (days)",
        help_text=(
            "Inactive rows older than this are purged by Phase 3 of cleanup. "
            "Default 7 days. Rows that are inactive+PENDING are always purged "
            "immediately (Phase 2) regardless of age."
        ),
    )
    cleanup_pending_safe_minutes = models.PositiveSmallIntegerField(
        default=10,
        verbose_name="Cleanup — PENDING safe buffer (minutes)",
        help_text=(
            "Phase 2 only deletes inactive+PENDING rows fetched more than this "
            "many minutes ago. Prevents a race where cleanup deletes a row that a "
            "sync task just picked up. Default 10 minutes."
        ),
    )

    # ── Classify controls ─────────────────────────────────────────────────────
    classify_chunk_limit = models.PositiveIntegerField(
        default=0,
        verbose_name="Classify chunk limit (0 = unlimited)",
        help_text=(
            "Maximum RawJobs processed per classify run. "
            "Set >0 to prevent timeouts on very large backlogs (e.g. 50000). "
            "Remaining rows are processed on the next scheduled run. "
            "0 means process everything in one shot (original behaviour)."
        ),
    )
    classify_lock_ttl_minutes = models.PositiveSmallIntegerField(
        default=180,
        verbose_name="Classify lock TTL (minutes)",
        help_text=(
            "How long the classify singleton lock is held. If the worker crashes "
            "the lock self-expires after this many minutes. Default 180 (3 h). "
            "Staff can also clear it manually via the Force Unlock button."
        ),
    )

    # ── Detection controls ────────────────────────────────────────────────────
    detect_batch_size = models.PositiveSmallIntegerField(
        default=200,
        verbose_name="Detection batch size",
        help_text=(
            "How many companies are processed per Run Detection task. "
            "Takes effect on next Run Detection trigger."
        ),
    )

    # ── Retry-failed controls ─────────────────────────────────────────────────
    retry_failed_days = models.PositiveSmallIntegerField(
        default=7,
        verbose_name="Retry failed — look-back window (days)",
        help_text=(
            "Retry Failed re-queues company fetch tasks that FAILED within this "
            "many days. Default 7. Increase to recover from longer outages."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "Harvest Engine Config"

    def __str__(self):
        return "Harvest Engine Config"

    @classmethod
    def get(cls):
        """Return the singleton, creating it with defaults if it doesn't exist."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def get_target_countries(self) -> list[str]:
        configured = self.target_countries if isinstance(self.target_countries, list) else []
        cleaned = [
            str(code).strip().upper()
            for code in configured
            if str(code).strip()
        ]
        return cleaned or ["US", "IN", "CA", "GB", "AU"]

    def save(self, *args, **kwargs):
        self.worker_concurrency = max(1, min(int(self.worker_concurrency or 1), 2))
        self.task_rate_limit = max(1, min(int(self.task_rate_limit or 1), 3))
        self.api_stagger_ms = max(int(self.api_stagger_ms or 0), 1000)
        self.scraper_stagger_ms = max(int(self.scraper_stagger_ms or 0), 5000)
        self.full_fetch_cooldown_minutes = max(int(self.full_fetch_cooldown_minutes or 0), 360)
        self.backfill_jd_workers = 1
        self.detect_batch_size = max(10, min(int(self.detect_batch_size or 50), 50))
        self.classify_chunk_limit = min(int(self.classify_chunk_limit or 0), 20000)
        old_target_countries = None
        try:
            old_target_countries = type(self).objects.filter(pk=1).values_list(
                "target_countries",
                flat=True,
            ).first()
        except Exception:
            old_target_countries = None
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)
        try:
            default_countries = ["US", "IN", "CA", "GB", "AU"]
            old_configured = [
                str(code).strip().upper()
                for code in (old_target_countries or [])
                if str(code).strip()
            ]
            old_clean = sorted(old_configured or default_countries)
            new_clean = sorted(self.get_target_countries())
            target_countries_changed = old_target_countries is not None and old_clean != new_clean
        except Exception:
            target_countries_changed = False
        try:
            from django.core.cache import cache

            cache.delete_many([
                "harvest:ready-stage-min-confidence:v1",
                "harvest:jd-backfill-lock-stale-minutes:v1",
                "harvest:legacy-hash-bridge-enabled:v1",
                "harvest:resume_jd_gate:thresholds:v1",
            ])
        except Exception:
            pass
        if target_countries_changed and self.rescope_on_target_country_change:
            def _queue_rescope():
                try:
                    from .tasks import reevaluate_cold_scope_jobs_task
                    reevaluate_cold_scope_jobs_task.apply_async(
                        kwargs={"reason": "target_countries_changed"},
                        countdown=5,
                        queue="harvest",
                    )
                except Exception:
                    pass

            transaction.on_commit(_queue_rescope)
        # Broadcast updated rate limit to all running Celery workers immediately.
        # Workers that are offline will pick up the new rate from DB on next task start.
        try:
            from celery import current_app
            current_app.control.rate_limit(
                "harvest.fetch_raw_jobs_for_company",
                f"{self.task_rate_limit}/m",
            )
        except Exception:
            pass  # Non-fatal — workers will apply the rate limit on next restart


class VetGateConfig(models.Model):
    """
    Singleton — GUI-configurable rules for the RawJob → Vet Queue sync gate.
    Edit at /harvest/vet-gate/ instead of hardcoding thresholds in gating.py.
    """

    # ── Scope section ─────────────────────────────────────────────────────────
    allow_unknown_country = models.BooleanField(
        default=True,
        verbose_name="Allow REVIEW_UNKNOWN_COUNTRY",
        help_text="Include jobs whose country could not be determined. Turn off to sync only confirmed target-country jobs.",
    )
    allow_possible_filter = models.BooleanField(
        default=True,
        verbose_name="Allow POSSIBLE filter decision",
        help_text="Include jobs where the pre-storage filter scored POSSIBLE (not just STRONG). Turn off for highest-confidence only.",
    )

    # ── JD quality thresholds ─────────────────────────────────────────────────
    require_description = models.BooleanField(
        default=True,
        verbose_name="Require job description",
        help_text="Block jobs that have no fetched JD. Highly recommended.",
    )
    min_word_count = models.PositiveSmallIntegerField(
        default=80,
        verbose_name="Minimum word count",
        help_text="Jobs with fewer words in the description are blocked. Default: 80.",
    )
    min_char_count = models.PositiveSmallIntegerField(
        default=400,
        verbose_name="Minimum character count",
        help_text="Jobs whose description text is shorter than this are blocked. Default: 400.",
    )

    # ── Lane thresholds (AUTO vs HUMAN) ───────────────────────────────────────
    auto_lane_min_vet_priority = models.FloatField(
        default=0.75,
        verbose_name="AUTO lane: min vet priority score",
        help_text="Jobs scoring at or above this go straight to AUTO lane (no human review needed). 0.0–1.0.",
    )
    auto_lane_min_data_quality = models.FloatField(
        default=0.72,
        verbose_name="AUTO lane: min data quality score",
        help_text="Data-quality score threshold for AUTO lane. 0.0–1.0.",
    )
    auto_lane_min_trust = models.FloatField(
        default=0.70,
        verbose_name="AUTO lane: min trust score",
        help_text="Trust score threshold for AUTO lane. 0.0–1.0.",
    )

    # ── Domain blocklist ───────────────────────────────────────────────────────
    blocked_domains = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Blocked job domains",
        help_text=(
            "JSON list of job_domain slugs to exclude from sync. "
            "Example: [\"hr-recruiter\", \"sales\", \"finance-accounting\"]. "
            "Jobs whose job_domain matches any entry are blocked regardless of other scores."
        ),
    )

    # ── Sync behaviour ─────────────────────────────────────────────────────────
    default_chunk_size = models.PositiveSmallIntegerField(
        default=500,
        verbose_name="Default chunk size",
        help_text="How many RawJobs to process per database page during sync. 50–2000.",
    )
    auto_sync_after_harvest = models.BooleanField(
        default=False,
        verbose_name="Auto-sync after full harvest",
        help_text="Automatically trigger a qualified sync when a full-crawl batch completes.",
    )

    class Meta:
        verbose_name = "Vet Gate Config"
        verbose_name_plural = "Vet Gate Config"

    def __str__(self):
        return "Vet Gate Config (singleton)"

    @classmethod
    def get(cls):
        """Return the singleton, creating it with defaults if it doesn't exist."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        # Clamp numeric fields to safe ranges
        self.min_word_count = max(1, min(int(self.min_word_count or 80), 2000))
        self.min_char_count = max(1, min(int(self.min_char_count or 400), 10000))
        self.auto_lane_min_vet_priority = max(0.0, min(float(self.auto_lane_min_vet_priority or 0.75), 1.0))
        self.auto_lane_min_data_quality = max(0.0, min(float(self.auto_lane_min_data_quality or 0.72), 1.0))
        self.auto_lane_min_trust = max(0.0, min(float(self.auto_lane_min_trust or 0.70), 1.0))
        self.default_chunk_size = max(50, min(int(self.default_chunk_size or 500), 2000))
        if not isinstance(self.blocked_domains, list):
            self.blocked_domains = []
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)


class HarvestPriorityRole(models.Model):
    """
    Roles we're actively trying to fill for clients right now.

    When the JD content gate (Tier 2) confirms a job whose category matches
    one of these, that job's backfill priority is boosted so it drains first.

    Example: Client needs 3 DevOps engineers urgently → add slug "devops-sre"
    with priority_boost=1 and an expiry date. Jobs matching that domain jump
    the backfill queue automatically.
    """

    role_slug = models.CharField(
        max_length=120,
        db_index=True,
        help_text="MarketingRole slug we're actively prioritizing (e.g. 'devops-sre', 'mlops-engineer').",
    )
    priority_boost = models.PositiveSmallIntegerField(
        default=5,
        help_text="1 = highest priority, 10 = lowest. Jobs matching this role jump the backfill queue.",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="NULL = no expiry. Set a date to auto-expire the priority signal.",
    )
    notes = models.TextField(
        blank=True,
        help_text="e.g. 'Client X needs 3 engineers by June 2026'.",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority_boost", "role_slug"]
        verbose_name = "Harvest Priority Role"
        verbose_name_plural = "Harvest Priority Roles"

    def __str__(self):
        return f"{self.role_slug} (boost={self.priority_boost})"

    @classmethod
    def active_slugs(cls) -> list[str]:
        """Return list of currently active priority role slugs (not expired)."""
        now = timezone.now()
        qs = cls.objects.filter(is_active=True).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=now)
        )
        return list(qs.values_list("role_slug", flat=True))

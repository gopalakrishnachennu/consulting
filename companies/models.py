from django.db import models


class Company(models.Model):
    """
    Core company/client profile.
    Initially focuses on identity + relationship metrics; can be extended later.
    """

    name = models.CharField(max_length=255, unique=True)
    # Canonical domain for this company (e.g. google.com). Used for dedupe/enrichment.
    domain = models.CharField(max_length=255, blank=True, db_index=True)
    alias = models.CharField(max_length=255, blank=True)
    website = models.URLField(blank=True)
    career_site_url = models.URLField(
        blank=True,
        help_text="Careers / jobs page URL for this company.",
    )
    linkedin_url = models.URLField(blank=True)
    logo_url = models.URLField(blank=True)

    class CompanyType(models.TextChoices):
        PRODUCT      = "product",      "Product Company"
        SERVICE      = "service",      "Service-Based"
        CONSULTANCY  = "consultancy",  "Consultancy"
        STAFFING     = "staffing",     "Staffing / Body Shop"
        UNKNOWN      = "unknown",      "Unknown"

    company_type = models.CharField(
        max_length=20,
        choices=CompanyType.choices,
        default=CompanyType.UNKNOWN,
        db_index=True,
        help_text="Product / Service-Based / Consultancy / Staffing — auto-detected by enrichment.",
    )

    industry = models.CharField(max_length=255, blank=True)
    size_band = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g. startup, SMB, mid-market, enterprise.",
    )
    headcount_range = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g. 1-10, 11-50, 51-200, 201-1000, 1000+",
    )
    hq_location = models.CharField(max_length=255, blank=True)
    locations = models.TextField(blank=True, help_text="Other office locations (free text).")

    relationship_status = models.CharField(
        max_length=20,
        blank=True,
        help_text="Optional tag like Hot / Warm / Cold / Blacklisted.",
    )
    primary_contact_name = models.CharField(max_length=255, blank=True)
    primary_contact_email = models.EmailField(blank=True)
    primary_contact_phone = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)

    total_submissions = models.PositiveIntegerField(default=0)
    total_interviews = models.PositiveIntegerField(default=0)
    total_offers = models.PositiveIntegerField(default=0)
    total_placements = models.PositiveIntegerField(default=0)
    last_activity_at = models.DateTimeField(null=True, blank=True)

    website_last_checked_at = models.DateTimeField(null=True, blank=True)
    website_is_valid = models.BooleanField(default=False)
    linkedin_last_checked_at = models.DateTimeField(null=True, blank=True)
    linkedin_is_valid = models.BooleanField(default=False)

    is_blacklisted = models.BooleanField(
        default=False,
        help_text="When true, new submissions to this company should be blocked.",
    )
    blacklist_reason = models.TextField(blank=True)

    # Enrichment (Phase 3)
    class EnrichmentStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        ENRICHED = "enriched", "Enriched"
        FAILED = "failed", "Failed"
        STALE = "stale", "Stale"

    description = models.TextField(blank=True, help_text="Short company description (from OG/website).")
    enrichment_status = models.CharField(
        max_length=20,
        choices=EnrichmentStatus.choices,
        default=EnrichmentStatus.PENDING,
        db_index=True,
    )
    enriched_at = models.DateTimeField(null=True, blank=True)
    enrichment_source = models.CharField(max_length=100, blank=True)
    data_quality_score = models.PositiveSmallIntegerField(
        default=0,
        help_text="0-100, how complete this record is.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CompanyDoNotSubmit(models.Model):
    """
    Consultant-level Do-Not-Submit restrictions for a company.
    Used to enforce NDAs, non-competes, or internal policies.
    """

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="dnd_rules")
    consultant = models.ForeignKey(
        "users.ConsultantProfile",
        on_delete=models.CASCADE,
        related_name="company_dnd_rules",
    )
    until = models.DateField(null=True, blank=True, help_text="Optional end date. Blank = indefinite.")
    reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("company", "consultant")

    def __str__(self) -> str:
        return f"DND {self.consultant_id} @ {self.company_id}"


class EnrichmentLog(models.Model):
    """
    Per-company enrichment run log for debugging (Phase 3.5 / 5).
    """
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="enrichment_logs")
    source = models.CharField(max_length=100, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    fields_updated = models.JSONField(default=dict, blank=True)
    success = models.BooleanField(default=False)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return f"{self.company_id} @ {self.timestamp} ({self.source})"

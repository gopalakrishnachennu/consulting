from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.utils.text import slugify
from django.utils import timezone
from uuid import uuid4

class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'ADMIN', _('Admin')
        EMPLOYEE = 'EMPLOYEE', _('Employee')
        CONSULTANT = 'CONSULTANT', _('Consultant')

    role = models.CharField(
        max_length=50,
        choices=Role.choices,
        default=Role.CONSULTANT,
    )

    # Add profile photo or other common fields here if needed
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    timezone = models.CharField(
        max_length=50,
        default="UTC",
        help_text=_("Time zone used for calendars, scheduling, and notifications (e.g. 'Europe/London', 'Asia/Kolkata')."),
    )

    def save(self, *args, **kwargs):
        if not self.pk and self.is_superuser:
            self.role = self.Role.ADMIN
        return super().save(*args, **kwargs)


class UserEmailNotificationPreferences(models.Model):
    """Per-user toggles for outbound email (Phase 3). In-app notifications are separate."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='email_notification_preferences',
    )
    email_submissions = models.BooleanField(
        default=True,
        help_text=_("Email when application status or pipeline events affect you."),
    )
    email_interviews = models.BooleanField(
        default=True,
        help_text=_("Email about interviews and scheduling."),
    )
    email_jobs = models.BooleanField(
        default=True,
        help_text=_("Email about job postings and auto-close notices."),
    )
    email_system = models.BooleanField(
        default=True,
        help_text=_("Email for system and account messages."),
    )
    # In-app (bell) — same categories as email; users can mute in-app without disabling email.
    inapp_submissions = models.BooleanField(
        default=True,
        help_text=_("In-app alerts for applications and pipeline updates."),
    )
    inapp_interviews = models.BooleanField(
        default=True,
        help_text=_("In-app alerts for interviews and scheduling."),
    )
    inapp_jobs = models.BooleanField(
        default=True,
        help_text=_("In-app alerts for job postings and job-related notices."),
    )
    inapp_system = models.BooleanField(
        default=True,
        help_text=_("In-app alerts for system and account messages."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Email prefs for {self.user_id}"


class MarketingRole(models.Model):
    """
    A domain/specialization that can be assigned to both Jobs and Consultants.
    Consultants receive marketing emails and job matches filtered to their roles.

    top_category groups roles into broad verticals:
      IT, NON_IT, ENGINEERING, HEALTHCARE, OTHER

    match_keywords is a list of lowercase keyword phrases. Any phrase found in a
    normalized job title or description (first 2 000 chars) triggers auto-assignment
    of this role during domain classification. Ordered from most-specific to most-generic
    so the first match wins.
    """
    class TopCategory(models.TextChoices):
        IT          = "IT",          "Information Technology"
        NON_IT      = "NON_IT",      "Non-IT / Business"
        ENGINEERING = "ENGINEERING", "Engineering (Non-IT)"
        HEALTHCARE  = "HEALTHCARE",  "Healthcare & Clinical"
        OTHER       = "OTHER",       "Other"

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.TextField(blank=True)
    top_category = models.CharField(
        max_length=20,
        choices=TopCategory.choices,
        default=TopCategory.OTHER,
        blank=True,
        db_index=True,
    )
    match_keywords = models.JSONField(
        default=list,
        blank=True,
        help_text="Lowercase keyword phrases matched against normalized title+description for auto-classification",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)
        # Bust the 5-min role-map cache so Celery workers pick up the change
        # on their next classification call without needing a restart.
        try:
            from jobs.marketing_role_routing import clear_marketing_role_cache
            clear_marketing_role_cache()
        except Exception:
            pass

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        try:
            from jobs.marketing_role_routing import clear_marketing_role_cache
            clear_marketing_role_cache()
        except Exception:
            pass
        return result

    def __str__(self):
        return self.name


class ConsultantProfile(models.Model):
    class WorkAuthRequirement(models.TextChoices):
        UNKNOWN = "unknown", _("Unknown")
        AUTHORIZED = "authorized", _("Authorized without sponsorship")
        SPONSORSHIP_REQUIRED = "sponsorship_required", _("Requires sponsorship")

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='consultant_profile')
    profile_slug = models.SlugField(
        max_length=80,
        unique=True,
        blank=True,
        help_text=_("Shareable URL slug for public profile, e.g. /c/john-doe"),
    )
    bio = models.TextField(blank=True)
    base_resume_text = models.TextField(blank=True)
    skills = models.JSONField(default=list, blank=True)
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True, default='')
    preferred_location = models.CharField(
        max_length=120,
        blank=True,
        default='',
        help_text=_("City, State used in resume header. e.g. 'Jersey City, NJ' or 'Remote'"),
    )
    work_countries = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Preferred work countries for job routing, e.g. ['United States', 'Canada']."),
    )
    preferred_seniority_levels = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Preferred seniority bands for job routing, e.g. ['mid', 'senior']."),
    )
    citizenship_countries = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Citizenship countries used for routing eligibility, e.g. ['United States']."),
    )
    work_authorization_countries = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Countries where this consultant can legally work without new sponsorship."),
    )
    requires_visa_sponsorship = models.BooleanField(
        null=True,
        blank=True,
        help_text=_("Whether the consultant requires visa sponsorship for routed jobs."),
    )
    visa_status = models.CharField(
        max_length=80,
        blank=True,
        help_text=_("Structured visa/work authorization note, e.g. H1B, OPT, Citizen, PR."),
    )
    employment_preferences = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Accepted employment arrangements, e.g. ['w2', 'c2c', '1099', 'full_time']."),
    )
    preferred_work_modes = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Accepted work modes, e.g. ['remote', 'hybrid', 'onsite']."),
    )
    clearance_eligible = models.BooleanField(
        default=False,
        help_text=_("Whether the consultant can be routed to jobs requiring security clearance."),
    )
    marketing_roles = models.ManyToManyField(MarketingRole, blank=True, related_name='consultants')

    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', _('Active')
        BENCH = 'BENCH', _('Bench')
        INACTIVE = 'INACTIVE', _('Inactive')
        PLACED = 'PLACED', _('Placed')

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        help_text=_("Current status of the consultant (Admin only)")
    )

    match_jd_title_override = models.BooleanField(
        null=True,
        blank=True,
        help_text=_("Override global setting for matching the most recent role title to the JD title.")
    )
    available_from = models.DateField(
        null=True,
        blank=True,
        help_text=_("Earliest date this consultant can start a new engagement."),
    )
    notice_period = models.CharField(
        max_length=80,
        blank=True,
        help_text=_("e.g. 2 weeks, immediate, 30 days."),
    )
    onboarding_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Set when the consultant finishes the onboarding wizard."),
    )

    def save(self, *args, **kwargs):
        if not self.profile_slug and self.user_id:
            base = slugify(self.user.get_full_name() or self.user.username or "profile")[:60]
            if not base:
                base = slugify(self.user.username)[:60]
            self.profile_slug = base
            orig = self.profile_slug
            n = 0
            while ConsultantProfile.objects.filter(profile_slug=self.profile_slug).exclude(pk=self.pk).exists():
                n += 1
                self.profile_slug = f"{orig}-{n}"[:80]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username}'s Consultant Profile"

class Department(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class EmployeeProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='employee_profile')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True, related_name='employees')
    designation = models.ForeignKey(
        'core.EmployeeDesignation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees',
        help_text=_('RBAC tier — controls which employee feature flags apply.'),
    )
    company_name = models.CharField(max_length=200, default="My Company")
    phone = models.CharField(max_length=30, blank=True, default="")
    work_location = models.CharField(max_length=160, blank=True, default="")
    can_manage_consultants = models.BooleanField(default=False, help_text="Designates whether this employee can add, edit, or delete consultants.")
    onboarding_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Set when the employee completes the first-run onboarding flow."),
    )
    
    def __str__(self):
        return f"{self.user.username}'s Employee Profile"


class ConsultantLead(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", _("New")
        REVIEWED = "REVIEWED", _("Reviewed")
        CONTACTED = "CONTACTED", _("Contacted")
        CONVERTED = "CONVERTED", _("Converted")
        ARCHIVED = "ARCHIVED", _("Archived")

    full_name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    current_title = models.CharField(max_length=160, blank=True)
    location = models.CharField(max_length=160, blank=True)
    linkedin_url = models.URLField(blank=True)
    resume = models.FileField(upload_to="consultant_leads/resumes/", blank=True, null=True)
    resume_text = models.TextField(blank=True)
    preferred_markets = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Comma-separated role families or market interests."),
    )
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW, db_index=True)
    converted_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consultant_lead_conversions",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} <{self.email}>"


class EmployerAccessRequest(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", _("New")
        REVIEWED = "REVIEWED", _("Reviewed")
        APPROVED = "APPROVED", _("Approved")
        DECLINED = "DECLINED", _("Declined")

    company_name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=160)
    work_email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    team_size = models.CharField(max_length=80, blank=True)
    hiring_volume = models.CharField(max_length=120, blank=True)
    message = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company_name} - {self.contact_name}"



class Experience(models.Model):
    consultant_profile = models.ForeignKey(ConsultantProfile, on_delete=models.CASCADE, related_name='experience')
    title = models.CharField(max_length=200)
    company = models.CharField(max_length=200)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    is_current = models.BooleanField(default=False)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.title} at {self.company}"

class Education(models.Model):
    consultant_profile = models.ForeignKey(ConsultantProfile, on_delete=models.CASCADE, related_name='education')
    institution = models.CharField(max_length=200)
    degree = models.CharField(max_length=200)
    field_of_study = models.CharField(max_length=200)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    
    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.degree} in {self.field_of_study}"

class Certification(models.Model):
    consultant_profile = models.ForeignKey(ConsultantProfile, on_delete=models.CASCADE, related_name='certifications')
    name = models.CharField(max_length=200)
    issuing_organization = models.CharField(max_length=200)
    issue_date = models.DateField()
    expiration_date = models.DateField(null=True, blank=True)
    credential_id = models.CharField(max_length=100, blank=True)
    
    class Meta:
        ordering = ['-issue_date']
        
    def __str__(self):
        return self.name

class SavedJob(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='saved_jobs')
    job = models.ForeignKey('jobs.Job', on_delete=models.CASCADE, related_name='saved_by')
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'job')
        ordering = ['-saved_at']


class ConsultantEmbedding(models.Model):
    """Stores the OpenAI embedding vector for a consultant profile (for semantic job matching)."""
    consultant = models.OneToOneField(
        ConsultantProfile, on_delete=models.CASCADE, related_name='embedding'
    )
    vector = models.JSONField(help_text="Embedding float list from text-embedding-3-small")
    model = models.CharField(max_length=80, default='text-embedding-3-small')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Consultant Embedding"

    def __str__(self):
        return f"Embedding for consultant {self.consultant_id}"

    def __str__(self):
        return f"{self.user.username} saved {self.job.title}"

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.utils.text import slugify

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

    organisation = models.ForeignKey(
        'core.Organisation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='users',
        help_text=_("Organisation / tenant for white-label mode (optional)."),
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

class MarketingRole(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ConsultantProfile(models.Model):
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
    company_name = models.CharField(max_length=200, default="My Company")
    can_manage_consultants = models.BooleanField(default=False, help_text="Designates whether this employee can add, edit, or delete consultants.")
    
    def __str__(self):
        return f"{self.user.username}'s Employee Profile"



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

    def __str__(self):
        return f"{self.user.username} saved {self.job.title}"

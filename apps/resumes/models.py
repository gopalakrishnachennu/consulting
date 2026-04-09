from django.db import models
from django.conf import settings
from django.core.cache import cache
import uuid
from jobs.models import Job
from users.models import ConsultantProfile


class MasterPrompt(models.Model):
    """
    Versioned, admin-editable master prompt for resume generation.
    One active prompt at a time. The system prompt + generation rules
    are sent as a single LLM call — no multi-call pipeline.
    """
    name = models.CharField(max_length=200, help_text="Version label, e.g. 'v2.1 — ATS focus'")
    system_prompt = models.TextField(
        help_text="The SYSTEM role text sent to the LLM (defines who it is and the rules)."
    )
    generation_rules = models.TextField(
        blank=True,
        help_text="Additional generation rules appended after the candidate + JD inputs. "
                  "Use this for output format, section rules, edge cases, quality gates."
    )
    default_input_sections = models.JSONField(
        default=dict,
        blank=True,
        help_text="Default toggles for which consultant data blocks are included in the "
                  "candidate profile sent to the LLM (per prompt version). "
                  "Keys: personal, experience, education, certifications, skills, total_years, base_resume.",
    )
    is_active = models.BooleanField(
        default=False,
        help_text="Only one prompt can be active. Activating this deactivates others."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.name} {'(active)' if self.is_active else ''}"

    def save(self, *args, **kwargs):
        if self.is_active:
            # Deactivate all others
            MasterPrompt.objects.exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)
        cache.delete('active_master_prompt')

    @classmethod
    def get_active(cls):
        """Return the active master prompt, cached."""
        cached = cache.get('active_master_prompt')
        if cached is None:
            cached = cls.objects.filter(is_active=True).first()
            if cached:
                cache.set('active_master_prompt', cached, timeout=300)
        return cached


class ResumeDraft(models.Model):
    class Status(models.TextChoices):
        PROCESSING = 'PROCESSING', 'Processing'
        DRAFT      = 'DRAFT',      'Draft'
        REVIEW     = 'REVIEW',     'Review Required'
        FINAL      = 'FINAL',      'Final'
        ERROR      = 'ERROR',      'Error'

    consultant = models.ForeignKey(
        ConsultantProfile, on_delete=models.CASCADE, related_name='resume_drafts'
    )
    job = models.ForeignKey(
        Job, on_delete=models.CASCADE, related_name='resume_drafts'
    )
    content = models.TextField(blank=True, help_text="Generated resume markdown")
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PROCESSING
    )
    error_message = models.TextField(blank=True, help_text="Error details if generation failed")
    llm_system_prompt = models.TextField(blank=True)
    llm_user_prompt = models.TextField(blank=True)
    llm_input_summary = models.JSONField(default=dict, blank=True)
    llm_request_payload = models.JSONField(default=dict, blank=True)
    ats_score = models.PositiveIntegerField(default=0)
    validation_errors = models.JSONField(default=list, blank=True)
    validation_warnings = models.JSONField(default=list, blank=True)
    tokens_used = models.PositiveIntegerField(default=0, help_text="Total tokens consumed")
    generation_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_drafts'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('consultant', 'job', 'version')
        ordering = ['-created_at']

    def __str__(self):
        return f"Draft v{self.version} — {self.consultant.user.username} → {self.job.title}"

    def save(self, *args, **kwargs):
        # Auto-increment version for same consultant + job
        skip_version = kwargs.pop('skip_version', False)
        if not self.pk and not skip_version:
            last = ResumeDraft.objects.filter(
                consultant=self.consultant, job=self.job
            ).order_by('-version').first()
            self.version = (last.version + 1) if last else 1
        super().save(*args, **kwargs)


# Keep backward compat alias for old code referencing Resume
Resume = ResumeDraft


class CoverLetter(models.Model):
    """AI-generated cover letter for a consultant + job pair."""
    consultant = models.ForeignKey(
        ConsultantProfile, on_delete=models.CASCADE, related_name='cover_letters'
    )
    job = models.ForeignKey(
        Job, on_delete=models.CASCADE, related_name='cover_letters'
    )
    content = models.TextField(blank=True, help_text="Generated cover letter text")
    version = models.PositiveIntegerField(default=1)
    tokens_used = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_cover_letters'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('consultant', 'job', 'version')

    def __str__(self):
        return f"Cover Letter v{self.version} — {self.consultant.user.username} → {self.job.title}"

    def save(self, *args, **kwargs):
        if not self.pk:
            last = CoverLetter.objects.filter(
                consultant=self.consultant, job=self.job
            ).order_by('-version').first()
            self.version = (last.version + 1) if last else 1
        super().save(*args, **kwargs)


# ─── Resume Template & Editor ────────────────────────────────────────────────

class ResumeTemplate(models.Model):
    FONT_CHOICES = [
        ('Georgia, serif',               'Georgia (Serif)'),
        ('"Times New Roman", serif',     'Times New Roman'),
        ('Garamond, Georgia, serif',     'Garamond'),
        ('"Palatino Linotype", serif',   'Palatino'),
        ('Arial, sans-serif',            'Arial'),
        ('"Helvetica Neue", Arial, sans-serif', 'Helvetica Neue'),
        ('Calibri, Arial, sans-serif',   'Calibri'),
        ('"Trebuchet MS", sans-serif',   'Trebuchet MS'),
        ('"Open Sans", sans-serif',      'Open Sans'),
    ]
    HEADER_STYLE_CHOICES = [
        ('underline', 'Underline'),
        ('bar',       'Filled Bar'),
        ('caps',      'All-Caps + Thin Rule'),
        ('plain',     'Plain (ATS Safe)'),
    ]

    name        = models.CharField(max_length=100)
    slug        = models.SlugField(unique=True)
    is_builtin  = models.BooleanField(default=False)
    created_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='resume_templates',
    )

    # Typography
    font_family    = models.CharField(max_length=120, default='Georgia, serif', choices=FONT_CHOICES)
    name_size      = models.PositiveSmallIntegerField(default=22)
    header_size    = models.PositiveSmallIntegerField(default=13)
    body_size      = models.PositiveSmallIntegerField(default=11)
    contact_size   = models.PositiveSmallIntegerField(default=10)

    # Colors
    accent_color   = models.CharField(max_length=7, default='#1e3a5f')
    name_color     = models.CharField(max_length=7, default='#111827')
    body_color     = models.CharField(max_length=7, default='#374151')

    # Layout (inches)
    margin_top     = models.FloatField(default=0.75)
    margin_bottom  = models.FloatField(default=0.75)
    margin_left    = models.FloatField(default=0.75)
    margin_right   = models.FloatField(default=0.75)
    line_height    = models.FloatField(default=1.3)
    para_spacing   = models.PositiveSmallIntegerField(default=5,  help_text='pt between paragraphs')
    section_spacing= models.PositiveSmallIntegerField(default=10, help_text='pt above section headers')

    # Style
    header_style   = models.CharField(max_length=20, default='underline', choices=HEADER_STYLE_CHOICES)
    show_dividers  = models.BooleanField(default=True)
    bullet_char    = models.CharField(max_length=5, default='•')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_builtin', 'name']

    def __str__(self):
        return self.name

    def to_dict(self):
        """Serialisable config dict for JS / export utils."""
        return {
            'id': self.pk, 'name': self.name, 'slug': self.slug,
            'is_builtin': self.is_builtin,
            'font_family': self.font_family,
            'name_size': self.name_size, 'header_size': self.header_size,
            'body_size': self.body_size, 'contact_size': self.contact_size,
            'accent_color': self.accent_color, 'name_color': self.name_color,
            'body_color': self.body_color,
            'margin_top': self.margin_top, 'margin_bottom': self.margin_bottom,
            'margin_left': self.margin_left, 'margin_right': self.margin_right,
            'line_height': self.line_height,
            'para_spacing': self.para_spacing, 'section_spacing': self.section_spacing,
            'header_style': self.header_style,
            'show_dividers': self.show_dividers, 'bullet_char': self.bullet_char,
        }


class ResumeEditorState(models.Model):
    """Persists parsed sections + selected template for a draft."""
    draft    = models.OneToOneField(ResumeDraft, on_delete=models.CASCADE, related_name='editor_state')
    template = models.ForeignKey(ResumeTemplate, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name='editor_states')
    sections_json = models.JSONField(default=dict, blank=True,
        help_text='Parsed sections: name, contact, summary, skills[], experience[], education[], certifications[]')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'EditorState for Draft #{self.draft_id}'


class LLMInputPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='llm_input_pref'
    )
    sections = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"LLM Input Prefs for {self.user.username}"

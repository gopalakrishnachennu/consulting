from django.db import models
from django.conf import settings
import uuid
from jobs.models import Job
from users.models import ConsultantProfile


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


class LLMInputPreference(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='llm_input_pref'
    )
    sections = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"LLM Input Prefs for {self.user.username}"

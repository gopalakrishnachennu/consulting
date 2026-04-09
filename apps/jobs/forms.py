from django import forms
from django.db.models import Q
from .models import Job
from companies.models import Company


class JobForm(forms.ModelForm):
    class Meta:
        model = Job
        fields = [
            'title',
            'company',
            'company_obj',
            'location',
            'description',
            'original_link',
            'salary_range',
            'job_type',
            'status',
            'job_source',
            'marketing_roles',
        ]
        widgets = {
            'marketing_roles': forms.CheckboxSelectMultiple(),
            'job_source': forms.TextInput(
                attrs={
                    'placeholder': 'e.g. LinkedIn, Indeed, referral',
                    'autocomplete': 'off',
                }
            ),
        }

    company_obj = forms.ModelChoiceField(
        queryset=Company.objects.all().order_by("name"),
        required=False,
        label="Company profile",
        help_text="Link this job to a structured company profile for analytics and blacklists.",
    )

    def _normalize_url(self, url: str) -> str:
        """Strip trailing slash for consistent URL comparison."""
        return (url or "").strip().rstrip("/")

    def clean_original_link(self):
        """
        Hard block: the job posting URL must be globally unique across the entire
        system (all statuses, archived or not). If an identical URL already exists,
        raise a ValidationError that names the conflicting job so the user can
        investigate rather than create a duplicate.

        For edits: only trigger if the URL has actually changed from the saved value,
        so existing (pre-validation) duplicates don't lock admins out of editing.
        """
        url = (self.cleaned_data.get("original_link") or "").strip()
        if not url:
            return url

        url_norm = self._normalize_url(url)

        # If editing and the URL hasn't changed from what's currently saved, skip check.
        if self.instance and self.instance.pk:
            current_norm = self._normalize_url(self.instance.original_link or "")
            if url_norm == current_norm:
                return url  # URL unchanged — no conflict possible with self

        exclude_pk = self.instance.pk if self.instance and self.instance.pk else None

        # Match with or without trailing slash, case-insensitive
        qs = Job.objects.filter(
            Q(original_link__iexact=url_norm)
            | Q(original_link__iexact=url_norm + "/")
        )
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)

        existing = qs.select_related("posted_by").first()
        if existing:
            raise forms.ValidationError(
                f"Duplicate URL — Job #{existing.pk} already uses this link: "
                f'"{existing.title}" at {existing.company} '
                f"(Status: {existing.get_status_display()}). "
                "Every job must have a unique posting URL. "
                "If this is a re-post, archive the old job first."
            )
        return url

class JobBulkUploadForm(forms.Form):
    csv_file = forms.FileField(label="Upload CSV File", help_text="Upload a CSV file with columns: title, company, location, description, requirements, salary_range")

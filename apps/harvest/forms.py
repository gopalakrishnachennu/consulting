import re

from django import forms

from .models import HarvestRoleCategory, JobBoardPlatform, JobDomain


class JobBoardPlatformForm(forms.ModelForm):
    class Meta:
        model = JobBoardPlatform
        fields = [
            "name", "slug", "url_patterns", "api_type", "fetch_endpoint_tmpl",
            "headers_json", "rate_limit_per_min", "requires_auth",
            "is_enabled", "title_in_list", "unknown_jd_budget_per_run",
            "support_tier", "color_hex", "notes",
        ]
        widgets = {
            "url_patterns": forms.Textarea(
                attrs={"rows": 3, "class": "font-mono text-sm",
                       "placeholder": '["myworkdayjobs.com", "wd1.myworkday.com"]'}
            ),
            "headers_json": forms.Textarea(
                attrs={"rows": 3, "class": "font-mono text-sm",
                       "placeholder": '{"Accept": "application/json"}'}
            ),
            "fetch_endpoint_tmpl": forms.Textarea(
                attrs={"rows": 2, "class": "font-mono text-sm",
                       "placeholder": "https://{tenant}.myworkdayjobs.com/wday/cxs/{tenant}/External/jobs"}
            ),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "color_hex": forms.TextInput(attrs={"type": "color", "class": "h-10 w-16 p-1 rounded cursor-pointer"}),
            "name": forms.TextInput(attrs={"placeholder": "Workday"}),
            "slug": forms.TextInput(attrs={"placeholder": "workday", "class": "font-mono"}),
            "rate_limit_per_min": forms.NumberInput(attrs={"min": 1, "max": 120}),
            "unknown_jd_budget_per_run": forms.NumberInput(attrs={"min": 0, "max": 100}),
        }
        help_texts = {
            "url_patterns": "JSON array of URL substrings that identify this platform.",
            "fetch_endpoint_tmpl": "Use {tenant} as a placeholder for the company's tenant/token.",
            "color_hex": "Badge colour shown in the company list.",
        }


class HarvestRoleCategoryForm(forms.ModelForm):
    include_phrases_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 12, "class": "font-mono text-sm"}),
        help_text="One phrase per line. Use specific multi-word phrases, not broad single keywords.",
    )
    exclude_phrases_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6, "class": "font-mono text-sm"}),
        help_text="One phrase per line. Category-specific negatives only.",
    )

    class Meta:
        model = HarvestRoleCategory
        fields = ["name", "slug", "is_active", "priority", "notes"]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "slug": forms.TextInput(attrs={"class": "font-mono"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["include_phrases_text"].initial = "\n".join(self.instance.include_phrases or [])
            self.fields["exclude_phrases_text"].initial = "\n".join(self.instance.exclude_phrases or [])

    @staticmethod
    def _phrases(value: str) -> list[str]:
        seen: set[str] = set()
        phrases: list[str] = []
        for line in (value or "").splitlines():
            phrase = " ".join(line.strip().lower().split())
            if not phrase or phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
        return phrases

    def clean_include_phrases_text(self):
        phrases = self._phrases(self.cleaned_data.get("include_phrases_text", ""))
        unsafe_single_terms = {"data", "engineer", "developer", "analyst", "manager", "specialist", "consultant"}
        bad = [p for p in phrases if p in unsafe_single_terms]
        if bad:
            raise forms.ValidationError(
                "Broad single-word phrases are unsafe here: %(phrases)s",
                params={"phrases": ", ".join(bad)},
            )
        return phrases

    def clean_exclude_phrases_text(self):
        return self._phrases(self.cleaned_data.get("exclude_phrases_text", ""))

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.include_phrases = self.cleaned_data["include_phrases_text"]
        obj.exclude_phrases = self.cleaned_data["exclude_phrases_text"]
        if commit:
            obj.save()
            self.save_m2m()
        return obj


class JobDomainForm(forms.ModelForm):
    """
    Form for creating/editing a JobDomain.
    Validates the regex pattern before save so a bad pattern
    can never reach the harvest engine.
    """

    class Meta:
        model = JobDomain
        fields = ["name", "slug", "regex_pattern", "top_category", "priority", "is_active", "notes"]
        widgets = {
            "regex_pattern": forms.Textarea(attrs={
                "rows": 3,
                "class": "font-mono text-sm w-full",
                "placeholder": r"\bsalesforce\b|\bsfdc\b",
            }),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "slug": forms.TextInput(attrs={"placeholder": "salesforce-developer"}),
            "name": forms.TextInput(attrs={"placeholder": "Salesforce Developer"}),
            "priority": forms.NumberInput(attrs={"min": 1, "max": 9999, "step": 10}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Slug may be left blank — it's auto-generated from the name on save.
        self.fields["slug"].required = False

    def clean(self):
        cleaned = super().clean()
        slug = (cleaned.get("slug") or "").strip()
        if not slug and cleaned.get("name"):
            from django.utils.text import slugify
            cleaned["slug"] = slugify(cleaned["name"])[:80]
        return cleaned

    def clean_regex_pattern(self):
        pattern = self.cleaned_data.get("regex_pattern", "").strip()
        if not pattern:
            raise forms.ValidationError("Regex pattern is required.")
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise forms.ValidationError(
                f"Invalid regex — Python says: {exc}. "
                "Fix the pattern and try again."
            )
        return pattern

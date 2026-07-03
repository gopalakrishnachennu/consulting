import re

from django import forms

from .models import HarvestRoleCategory, JobBoardPlatform, JobDomain, PlatformEngineConfig


class JobBoardPlatformForm(forms.ModelForm):
    config_auto_backfill = forms.BooleanField(
        required=False,
        label="Auto JD backfill",
        help_text="Queue detail-description fetches for this platform when list data is not enough.",
    )
    config_backfill_priority = forms.IntegerField(
        min_value=1,
        max_value=10,
        label="Backfill priority",
        help_text="1 = highest priority, 10 = lowest.",
    )
    config_fetch_cadence_hours = forms.IntegerField(
        min_value=0,
        max_value=720,
        label="Fetch cadence hours",
        help_text="Minimum hours between scheduled fetches for companies on this platform.",
    )
    config_inter_request_delay_ms = forms.IntegerField(
        min_value=0,
        max_value=60000,
        label="Inter-request delay ms",
        help_text="Delay enforced before each fetch. API boards can be lower; scraper boards should be slower.",
    )
    config_min_quality_score = forms.FloatField(
        min_value=0.0,
        max_value=1.0,
        label="Minimum quality score",
        help_text="Jobs below this floor are treated as low quality after enrichment.",
    )
    config_is_active = forms.BooleanField(
        required=False,
        label="Runtime config active",
        help_text="Disable only when this platform should ignore per-platform runtime rules.",
    )
    config_html_render_backend = forms.ChoiceField(
        choices=PlatformEngineConfig.HtmlRenderBackend.choices,
        required=False,
        label="HTML render backend",
        help_text=(
            "Used by the generic HTML fallback harvester. "
            "Obscura is useful for JS-heavy career pages."
        ),
    )

    class Meta:
        model = JobBoardPlatform
        fields = [
            "name", "slug", "url_patterns", "api_type", "fetch_endpoint_tmpl",
            "headers_json", "rate_limit_per_min", "requires_auth",
            "is_enabled", "title_in_list", "list_has_description", "unknown_jd_budget_per_run",
            "support_tier", "color_hex", "notes",
            "config_auto_backfill", "config_backfill_priority", "config_fetch_cadence_hours",
            "config_inter_request_delay_ms", "config_html_render_backend",
            "config_min_quality_score", "config_is_active",
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cfg = None
        if self.instance and self.instance.pk:
            try:
                cfg = self.instance.config
            except PlatformEngineConfig.DoesNotExist:
                cfg = None

        runtime_defaults = {
            "config_auto_backfill": False,
            "config_backfill_priority": 5,
            "config_fetch_cadence_hours": 24,
            "config_inter_request_delay_ms": 1500,
            "config_min_quality_score": 0.3,
            "config_is_active": True,
            "config_html_render_backend": PlatformEngineConfig.HtmlRenderBackend.REQUESTS,
        }
        if cfg:
            runtime_defaults.update({
                "config_auto_backfill": cfg.auto_backfill,
                "config_backfill_priority": cfg.backfill_priority,
                "config_fetch_cadence_hours": cfg.fetch_cadence_hours,
                "config_inter_request_delay_ms": cfg.inter_request_delay_ms,
                "config_html_render_backend": cfg.html_render_backend,
                "config_min_quality_score": cfg.min_quality_score,
                "config_is_active": cfg.is_active,
            })
        for field_name, value in runtime_defaults.items():
            self.fields[field_name].initial = value

    def clean_url_patterns(self):
        patterns = self.cleaned_data.get("url_patterns") or []
        if not isinstance(patterns, list):
            raise forms.ValidationError("Enter a JSON array of URL pattern strings.")

        normalized = []
        seen = set()
        for pattern in patterns:
            text = str(pattern or "").strip().lower()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)

        if not normalized:
            raise forms.ValidationError("Add at least one URL pattern.")
        return normalized

    def clean_headers_json(self):
        headers = self.cleaned_data.get("headers_json") or {}
        if not isinstance(headers, dict):
            raise forms.ValidationError("Enter a JSON object of HTTP headers.")
        return headers

    def save(self, commit=True):
        platform = super().save(commit=commit)
        if commit:
            cfg, _ = PlatformEngineConfig.objects.get_or_create(platform=platform)
            cfg.auto_backfill = self.cleaned_data["config_auto_backfill"]
            cfg.backfill_priority = self.cleaned_data["config_backfill_priority"]
            cfg.fetch_cadence_hours = self.cleaned_data["config_fetch_cadence_hours"]
            cfg.inter_request_delay_ms = self.cleaned_data["config_inter_request_delay_ms"]
            cfg.html_render_backend = (
                self.cleaned_data.get("config_html_render_backend")
                or PlatformEngineConfig.HtmlRenderBackend.REQUESTS
            )
            cfg.min_quality_score = self.cleaned_data["config_min_quality_score"]
            cfg.is_active = self.cleaned_data["config_is_active"]
            cfg.save(update_fields=[
                "auto_backfill",
                "backfill_priority",
                "fetch_cadence_hours",
                "inter_request_delay_ms",
                "html_render_backend",
                "min_quality_score",
                "is_active",
                "updated_at",
            ])
        return platform


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

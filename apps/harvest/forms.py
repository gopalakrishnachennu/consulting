from django import forms

from .models import JobBoardPlatform


class JobBoardPlatformForm(forms.ModelForm):
    class Meta:
        model = JobBoardPlatform
        fields = [
            "name", "slug", "url_patterns", "api_type", "fetch_endpoint_tmpl",
            "headers_json", "rate_limit_per_min", "requires_auth",
            "is_enabled", "color_hex", "notes",
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
        }
        help_texts = {
            "url_patterns": "JSON array of URL substrings that identify this platform.",
            "fetch_endpoint_tmpl": "Use {tenant} as a placeholder for the company's tenant/token.",
            "color_hex": "Badge colour shown in the company list.",
        }

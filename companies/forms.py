from django import forms

from .models import Company
from .services import normalize_company_name, normalize_domain


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = [
            "name",
            "alias",
            "industry",
            "size_band",
            "headcount_range",
            "hq_location",
            "locations",
            "relationship_status",
            "primary_contact_name",
            "primary_contact_email",
            "primary_contact_phone",
            "website",
            "linkedin_url",
            "career_site_url",
            "notes",
            "is_blacklisted",
            "blacklist_reason",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["website"].required = True
        self.fields["career_site_url"].required = True

    def clean_name(self):
        raw = self.cleaned_data.get("name", "")
        normalized = normalize_company_name(raw)
        return normalized or raw

    def clean_website(self):
        url = (self.cleaned_data.get("website") or "").strip()
        return url

    def save(self, commit=True):
        instance: Company = super().save(commit=False)
        # Keep domain in sync from website when possible
        if instance.website:
            instance.domain = normalize_domain(instance.website)
        elif instance.career_site_url and not instance.domain:
            instance.domain = normalize_domain(instance.career_site_url)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class CompanyCSVImportForm(forms.Form):
    csv_file = forms.FileField(
        label="Company CSV file",
        help_text="CSV with columns: name, website, linkedin_url, industry, alias, size_band, hq_location.",
    )


class CompanyDomainImportForm(forms.Form):
    domains = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 6}),
        label="Domains",
        help_text="One domain or URL per line, e.g. google.com or https://stripe.com.",
    )


class CompanyLinkedInImportForm(forms.Form):
    linkedin_urls = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 6}),
        label="LinkedIn company URLs",
        help_text="One URL per line, e.g. https://www.linkedin.com/company/stripe/.",
    )

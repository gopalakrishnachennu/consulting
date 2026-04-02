from django import forms
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
            'marketing_roles',
        ]
        widgets = {
            'marketing_roles': forms.CheckboxSelectMultiple(),
        }

    company_obj = forms.ModelChoiceField(
        queryset=Company.objects.all().order_by("name"),
        required=False,
        label="Company profile",
        help_text="Link this job to a structured company profile for analytics and blacklists.",
    )

class JobBulkUploadForm(forms.Form):
    csv_file = forms.FileField(label="Upload CSV File", help_text="Upload a CSV file with columns: title, company, location, description, requirements, salary_range")

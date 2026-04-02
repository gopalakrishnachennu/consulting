from django import forms
from jobs.models import Job


class DraftGenerateForm(forms.Form):
    """Simple form: select a job to generate a draft for. Consultant comes from URL."""
    job = forms.ModelChoiceField(
        queryset=Job.objects.filter(status='OPEN'),
        label="Select Job",
        empty_label="— Choose an open job —",
    )


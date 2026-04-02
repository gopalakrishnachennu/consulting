from django import forms
from .models import ApplicationSubmission, SubmissionResponse, Offer, OfferRound, EmailEvent

class ApplicationSubmissionForm(forms.ModelForm):
    class Meta:
        model = ApplicationSubmission
        fields = ['job', 'consultant', 'resume', 'status', 'proof_file', 'notes']
        widgets = {
            'job': forms.HiddenInput(),
            'consultant': forms.HiddenInput(),
            'resume': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make status optional for initial submission, defaults to model default
        self.fields['status'].required = False


class SubmissionResponseForm(forms.ModelForm):
    class Meta:
        model = SubmissionResponse
        fields = ['response_type', 'status', 'responded_at', 'notes']
        widgets = {
            'responded_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }


class OfferRoundForm(forms.ModelForm):
    class Meta:
        model = OfferRound
        fields = ['salary', 'currency', 'bonus_notes', 'notes']
        widgets = {
            'currency': forms.TextInput(attrs={'placeholder': 'e.g. USD'}),
            'bonus_notes': forms.TextInput(attrs={'placeholder': 'Bonus, equity, etc.'}),
        }


class OfferInitialForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = ['initial_salary', 'initial_currency', 'initial_notes']
        widgets = {
            'initial_notes': forms.Textarea(attrs={'rows': 2}),
        }


class OfferFinalTermsForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = ['final_salary', 'final_currency', 'final_terms', 'accepted_at']
        widgets = {
            'accepted_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'final_terms': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Summary of final accepted terms'}),
        }


class EmailEventReviewForm(forms.Form):
    submission_id = forms.IntegerField(required=True, min_value=1, label="Submission ID")
    new_status = forms.ChoiceField(
        required=True,
        choices=[(s[0], s[1]) for s in ApplicationSubmission.Status.choices if s[0] != ApplicationSubmission.Status.IN_PROGRESS],
        label="Set status to",
    )
    note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional note (will be added to the timeline)'}),
        label="Note",
    )

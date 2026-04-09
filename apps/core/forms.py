from django import forms
from .models import PlatformConfig, LLMConfig, BroadcastMessage, Organisation
from .security import encrypt_value, decrypt_value


class LLMConfigForm(forms.ModelForm):
    active_model = forms.ChoiceField(required=False)
    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True),
        help_text="Enter OpenAI API key (stored encrypted). Leave blank to keep existing."
    )

    class Meta:
        model = LLMConfig
        fields = [
            'active_model',
            'temperature',
            'max_output_tokens',
            'monthly_token_cap',
            'generation_enabled',
            'auto_disable_on_cap',
            'data_pipelines_connected',
        ]
        widgets = {
            'active_model': forms.Select(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields:
            if isinstance(self.fields[field].widget, (forms.TextInput, forms.URLInput, forms.EmailInput, forms.NumberInput, forms.Textarea, forms.Select)):
                self.fields[field].widget.attrs.update({'class': 'w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500'})
            elif isinstance(self.fields[field].widget, forms.CheckboxInput):
                self.fields[field].widget.attrs.update({'class': 'h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded'})

    def save(self, commit=True):
        instance = super().save(commit=False)
        api_key = self.cleaned_data.get('api_key')
        if api_key:
            instance.encrypted_api_key = encrypt_value(api_key)
        if commit:
            instance.save()
            self.save_m2m()
        return instance

class PlatformConfigForm(forms.ModelForm):
    email_imap_password = forms.CharField(
        required=False,
        widget=forms.TextInput(),
        help_text="IMAP app password or mailbox password. Visible here, stored encrypted in the database.",
        label="IMAP Password",
    )

    class Meta:
        model = PlatformConfig
        fields = '__all__'
        widgets = {
            'meta_description': forms.Textarea(attrs={'rows': 3}),
            'address': forms.Textarea(attrs={'rows': 3}),
            'maintenance_message': forms.Textarea(attrs={'rows': 3}),
            'pool_review_notify_emails': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Add basic styling
        for field in self.fields:
            widget = self.fields[field].widget
            if isinstance(widget, (forms.TextInput, forms.URLInput, forms.EmailInput, forms.NumberInput, forms.Textarea, forms.PasswordInput)):
                widget.attrs.update({'class': 'w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500'})
            elif isinstance(widget, forms.CheckboxInput):
                widget.attrs.update({'class': 'h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded'})

        instance: PlatformConfig = self.instance
        if getattr(instance, "email_imap_encrypted_password", ""):
            # Decrypt so the admin can see and re-use the exact value.
            self.initial.setdefault(
                "email_imap_password",
                decrypt_value(instance.email_imap_encrypted_password),
            )

    def save(self, commit=True):
        instance = super().save(commit=False)
        password = (self.cleaned_data.get('email_imap_password') or "").strip()
        if password:
            instance.email_imap_encrypted_password = encrypt_value(password)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class BroadcastForm(forms.ModelForm):
    """Admin broadcast: title, body, optional link, audience, optional org scope."""

    class Meta:
        model = BroadcastMessage
        fields = ['title', 'body', 'link', 'kind', 'audience', 'organisation']
        help_texts = {
            'audience': (
                'Employees only = internal staff. Consultants only = candidates. '
                '"Employees and consultants" sends to both (excludes admin-only accounts unless you pick another scope).'
            ),
        }
        widgets = {
            'title': forms.TextInput(attrs={'class': 'w-full px-3 py-2 border rounded-lg'}),
            'body': forms.Textarea(attrs={'rows': 5, 'class': 'w-full px-3 py-2 border rounded-lg'}),
            'link': forms.TextInput(
                attrs={'class': 'w-full px-3 py-2 border rounded-lg', 'placeholder': '/help/ or /jobs/…'}
            ),
            'kind': forms.Select(attrs={'class': 'w-full px-3 py-2 border rounded-lg'}),
            'audience': forms.Select(attrs={'class': 'w-full px-3 py-2 border rounded-lg'}),
            'organisation': forms.Select(attrs={'class': 'w-full px-3 py-2 border rounded-lg'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['organisation'].required = False
        self.fields['organisation'].queryset = Organisation.objects.order_by('name')
        self.fields['link'].required = False

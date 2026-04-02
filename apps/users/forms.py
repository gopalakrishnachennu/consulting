from django import forms
from django.contrib.auth import get_user_model
from .models import Experience, Education, Certification, ConsultantProfile, EmployeeProfile, MarketingRole, Department

User = get_user_model()


class ConsultantCreateForm(forms.Form):
    """Form for admins to create a new consultant user + profile."""
    # User fields
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    password = forms.CharField(
        widget=forms.PasswordInput,
        required=False,
        help_text="Leave blank to auto-generate from name + email.",
    )

    # Profile fields
    phone = forms.CharField(max_length=20, required=False)
    bio = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)
    base_resume_text = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 6}),
        required=False,
        help_text="Paste the consultant's base resume text here.",
    )
    skills = forms.CharField(
        required=False,
        help_text="Comma-separated list, e.g. Python, Django, AWS",
    )
    hourly_rate = forms.DecimalField(max_digits=10, decimal_places=2, required=False)
    match_jd_title_override = forms.BooleanField(
        required=False,
        label="Match JD title for most recent role",
        help_text="If checked, the most recent resume role title is replaced with the JD title.",
    )

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("A user with this username already exists.")
        return username

    def _generate_password(self):
        """Build password from firstname.lastname@domain or fallback."""
        first = self.cleaned_data.get('first_name', '').strip()
        last = self.cleaned_data.get('last_name', '').strip()
        email = self.cleaned_data.get('email', '').strip()

        domain_part = ''
        if email and '@' in email:
            domain_part = email.split('@')[1].split('.')[0]  # e.g. "example" from "a@example.com"

        if first and last and domain_part:
            return f"{first}.{last}@{domain_part}"
        elif first and last:
            return f"{first}.{last}@consultant"
        else:
            username = self.cleaned_data.get('username', 'user')
            return f"consultant_{username}"

    def save(self):
        data = self.cleaned_data
        password = data.get('password', '').strip()
        generated = False
        if not password:
            password = self._generate_password()
            generated = True

        user = User.objects.create_user(
            username=data['username'],
            email=data.get('email', ''),
            password=password,
            first_name=data.get('first_name', ''),
            last_name=data.get('last_name', ''),
            role=User.Role.CONSULTANT,
        )
        skills_raw = data.get('skills', '')
        skills_list = [s.strip() for s in skills_raw.split(',') if s.strip()] if skills_raw else []
        ConsultantProfile.objects.create(
            user=user,
            bio=data.get('bio', ''),
            base_resume_text=data.get('base_resume_text', ''),
            skills=skills_list,
            hourly_rate=data.get('hourly_rate'),
            phone=data.get('phone', ''),
            match_jd_title_override=data.get('match_jd_title_override'),
        )
        return user, password, generated

class EmployeeCreateForm(forms.Form):
    """Form for admins to create a new employee user + profile."""
    # User fields
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    password = forms.CharField(
        widget=forms.PasswordInput,
        required=False,
        help_text="Leave blank to auto-generate from name + email.",
    )

    # Profile fields
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False, empty_label="Select Department")
    company_name = forms.CharField(max_length=100, required=False)

    def clean_username(self):
        username = self.cleaned_data['username']
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("A user with this username already exists.")
        return username

    def _generate_password(self):
        """Build password from firstname.lastname@domain or fallback."""
        first = self.cleaned_data.get('first_name', '').strip()
        last = self.cleaned_data.get('last_name', '').strip()
        email = self.cleaned_data.get('email', '').strip()

        domain_part = ''
        if email and '@' in email:
            domain_part = email.split('@')[1].split('.')[0]

        if first and last and domain_part:
            return f"{first}.{last}@{domain_part}"
        elif first and last:
            return f"{first}.{last}@employee"
        else:
            username = self.cleaned_data.get('username', 'user')
            return f"employee_{username}"

    def save(self):
        data = self.cleaned_data
        password = data.get('password', '').strip()
        generated = False
        if not password:
            password = self._generate_password()
            generated = True

        user = User.objects.create_user(
            username=data['username'],
            email=data.get('email', ''),
            password=password,
            first_name=data.get('first_name', ''),
            last_name=data.get('last_name', ''),
            role=User.Role.EMPLOYEE,
        )
        EmployeeProfile.objects.create(
            user=user,
            department=data.get('department'),
            company_name=data.get('company_name', ''),
        )
        return user, password, generated




class ExperienceForm(forms.ModelForm):
    class Meta:
        model = Experience
        fields = ['title', 'company', 'start_date', 'end_date', 'is_current', 'description']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
            'description': forms.Textarea(attrs={'rows': 3}),
        }

class EducationForm(forms.ModelForm):
    class Meta:
        model = Education
        fields = ['institution', 'degree', 'field_of_study', 'start_date', 'end_date']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }

class CertificationForm(forms.ModelForm):
    class Meta:
        model = Certification
        fields = ['name', 'issuing_organization', 'issue_date', 'expiration_date', 'credential_id']
        widgets = {
            'issue_date': forms.DateInput(attrs={'type': 'date'}),
            'expiration_date': forms.DateInput(attrs={'type': 'date'}),
        }


TIMEZONE_CHOICES = [
    ("UTC", "UTC"),
    ("Europe/London", "UK / Europe – London"),
    ("Europe/Berlin", "Europe – Central (Berlin)"),
    ("Asia/Kolkata", "India – IST (Kolkata)"),
    ("Asia/Dubai", "Gulf – Dubai"),
    ("America/New_York", "US – Eastern (New York)"),
    ("America/Chicago", "US – Central (Chicago)"),
    ("America/Denver", "US – Mountain (Denver)"),
    ("America/Los_Angeles", "US – Pacific (Los Angeles)"),
    ("Australia/Sydney", "Australia – Sydney"),
]


class UserProfileForm(forms.ModelForm):
    """Edit basic user fields: name, email, and timezone."""

    timezone = forms.ChoiceField(choices=TIMEZONE_CHOICES, required=True, label="Time zone")

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'timezone']


class EmployeeProfileForm(forms.ModelForm):
    """Edit employee-specific fields: department, company_name."""
    class Meta:
        model = EmployeeProfile
        fields = ['department', 'company_name', 'can_manage_consultants']
        widgets = {
            'department': forms.Select(attrs={'class': 'w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500'}),
            'can_manage_consultants': forms.CheckboxInput(attrs={'class': 'h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded'}),
        }


class ConsultantProfileEditForm(forms.ModelForm):
    """Edit consultant-specific fields: bio, skills (as comma-separated), hourly_rate, phone."""
    skills_text = forms.CharField(
        required=False,
        label='Skills',
        help_text='Comma-separated list, e.g. Python, Django, AWS',
        widget=forms.TextInput(),
    )

    class Meta:
        model = ConsultantProfile
        fields = ['bio', 'base_resume_text', 'hourly_rate', 'phone', 'match_jd_title_override', 'marketing_roles', 'status']
        widgets = {
            'bio': forms.Textarea(attrs={'rows': 3}),
            'base_resume_text': forms.Textarea(attrs={'rows': 6}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['skills_text'].initial = ', '.join(self.instance.skills or [])
        
        # Marketing Roles: Admin only, as checkboxes
        is_admin = self.user and (self.user.is_superuser or self.user.role == User.Role.ADMIN)
        if is_admin:
            self.fields['marketing_roles'].widget = forms.CheckboxSelectMultiple()
            self.fields['marketing_roles'].queryset = MarketingRole.objects.all()
            # Status field is already in Meta.fields, so we just ensure it's available for admins
        else:
            if 'marketing_roles' in self.fields:
                del self.fields['marketing_roles']
            if 'status' in self.fields:
                del self.fields['status']

    def save(self, commit=True):
        instance = super().save(commit=False)
        raw = self.cleaned_data.get('skills_text', '')
        instance.skills = [s.strip() for s in raw.split(',') if s.strip()]
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class MarketingRoleForm(forms.ModelForm):
    """Form for admins to create/edit marketing roles."""
    class Meta:
        model = EmployeeProfile
        fields = ['company_name', 'department', 'can_manage_consultants']
        widgets = {
            'department': forms.Select(attrs={'class': 'w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500'}),
            'can_manage_consultants': forms.CheckboxInput(attrs={'class': 'h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded'}),
        }

import csv
from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q, Avg, Count
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.contrib import messages
from django.views import View as BaseView
from django.http import HttpResponse
from django.core.serializers.json import DjangoJSONEncoder
import json

from .models import (
    User,
    ConsultantProfile,
    Experience,
    Education,
    Certification,
    SavedJob,
    MarketingRole,
    EmployeeProfile,
    UserEmailNotificationPreferences,
)
from .forms import (
    ExperienceForm,
    EducationForm,
    CertificationForm,
    ConsultantCreateForm,
    UserProfileForm,
    EmployeeProfileForm,
    ConsultantProfileEditForm,
    MarketingRoleForm,
    EmployeeCreateForm,
    ConsultantOnboardingStep1Form,
    ConsultantOnboardingStep2Form,
    UserEmailNotificationPreferencesForm,
)
from jobs.models import Job
from jobs.services import match_jobs_for_consultant
from submissions.models import ApplicationSubmission, SubmissionResponse
from resumes.models import ResumeDraft
from interviews_app.models import Interview
from config.constants import (
    PAGINATION_CONSULTANTS,
    PAGINATION_SAVED_JOBS,
    DASHBOARD_RECENT_ITEMS,
    DASHBOARD_RECENT_JOBS,
    MSG_EXPERIENCE_ADDED,
    MSG_EXPERIENCE_UPDATED,
    MSG_EXPERIENCE_DELETED,
    MSG_EDUCATION_ADDED,
    MSG_EDUCATION_UPDATED,
    MSG_EDUCATION_DELETED,
    MSG_CERT_ADDED,
    MSG_CERT_UPDATED,
    MSG_CERT_DELETED,
    MSG_JOB_SAVED,
    MSG_JOB_UNSAVED,
    MSG_ONLY_CONSULTANTS_SAVE,
)
from core.models import PlatformConfig, LLMUsageLog
from .journey_utils import (
    compute_consultant_readiness,
    build_journey_steps,
    at_risk_submissions_queryset,
)

class ConsultantListView(LoginRequiredMixin, ListView):
    model = User
    template_name = 'users/consultant_list.html'
    context_object_name = 'consultants'
    paginate_by = PAGINATION_CONSULTANTS
    ordering = ['-date_joined']

    def get_queryset(self):
        qs = User.objects.filter(role=User.Role.CONSULTANT, consultant_profile__isnull=False)
        search_query = self.request.GET.get('search')
        role_filter = self.request.GET.get('role')
        status_filter = self.request.GET.get('status')

        if search_query:
            try:
                qs = qs.filter(
                    Q(username__icontains=search_query) |
                    Q(consultant_profile__bio__icontains=search_query) |
                    Q(consultant_profile__skills__icontains=search_query)
                )
            except Exception:
                qs = qs.filter(
                    Q(username__icontains=search_query) |
                    Q(consultant_profile__bio__icontains=search_query)
                )

        if role_filter:
            qs = qs.filter(consultant_profile__marketing_roles__slug=role_filter)

        if status_filter:
            qs = qs.filter(consultant_profile__status=status_filter)

        return qs.select_related('consultant_profile').distinct()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['marketing_roles'] = MarketingRole.objects.all()
        context['selected_role'] = self.request.GET.get('role', '')
        context['selected_status'] = self.request.GET.get('status', '')
        qd = self.request.GET.copy()
        qd.pop('page', None)
        context['pagination_query'] = qd.urlencode()
        # Status summary for header chips (Active / Bench / Placed / Inactive)
        status_counts = {
            'ACTIVE': 0,
            'BENCH': 0,
            'PLACED': 0,
            'INACTIVE': 0,
        }
        for row in self.object_list.values('consultant_profile__status').annotate(count=Count('id')):
            code = row.get('consultant_profile__status')
            if code in status_counts:
                status_counts[code] = row['count']
        context['status_summary'] = status_counts
        return context

    def get_template_names(self):
        if self.request.headers.get('HX-Request'):
            return ['users/_consultant_list_partial.html']
        return super().get_template_names()


def _get_consultant_list_queryset(request):
    """Shared queryset for consultant list and CSV export (search, role, status, min/max rate)."""
    qs = User.objects.filter(role=User.Role.CONSULTANT, consultant_profile__isnull=False)
    search_query = request.GET.get('search')
    role_filter = request.GET.get('role')
    status_filter = request.GET.get('status')
    if search_query:
        try:
            qs = qs.filter(
                Q(username__icontains=search_query)
                | Q(consultant_profile__bio__icontains=search_query)
                | Q(consultant_profile__skills__icontains=search_query)
            )
        except Exception:
            qs = qs.filter(
                Q(username__icontains=search_query)
                | Q(consultant_profile__bio__icontains=search_query)
            )
    if role_filter:
        qs = qs.filter(consultant_profile__marketing_roles__slug=role_filter)
    if status_filter:
        qs = qs.filter(consultant_profile__status=status_filter)
    return qs.select_related('consultant_profile').prefetch_related('consultant_profile__marketing_roles').distinct()


class ConsultantExportCSVView(LoginRequiredMixin, BaseView):
    """Export consultant list as CSV with same filters as list view."""

    def get(self, request, *args, **kwargs):
        qs = _get_consultant_list_queryset(request)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="consultants.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Username', 'Full Name', 'Email', 'Status', 'Hourly Rate', 'Phone',
            'Marketing Roles', 'Skills', 'Bio (first 200 chars)', 'Date Joined'
        ])
        for user in qs:
            profile = user.consultant_profile
            roles = ', '.join(r.name for r in profile.marketing_roles.all())
            skills = ', '.join(profile.skills) if isinstance(profile.skills, list) else (profile.skills or '')
            bio = (profile.bio or '')[:200]
            writer.writerow([
                user.username,
                user.get_full_name() or '',
                user.email or '',
                profile.get_status_display(),
                profile.hourly_rate or '',
                profile.phone or '',
                roles,
                skills,
                bio,
                user.date_joined.strftime('%Y-%m-%d %H:%M'),
            ])
        return response


def _get_employee_list_queryset(request):
    """Shared queryset for employee list and CSV export (search)."""
    qs = User.objects.filter(role=User.Role.EMPLOYEE)
    search_query = request.GET.get('search')
    if search_query:
        qs = qs.filter(
            Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(employee_profile__department__name__icontains=search_query)
        )
    return qs.select_related('employee_profile', 'employee_profile__department')


class EmployeeExportCSVView(LoginRequiredMixin, UserPassesTestMixin, BaseView):
    """Export employee list as CSV with same filters as list view (admin only)."""

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def get(self, request, *args, **kwargs):
        qs = _get_employee_list_queryset(request)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="employees.csv"'
        writer = csv.writer(response)
        writer.writerow(['Username', 'Full Name', 'Email', 'Department', 'Company', 'Can Manage Consultants', 'Date Joined'])
        for user in qs:
            profile = getattr(user, 'employee_profile', None)
            dept = profile.department.name if profile and profile.department else ''
            company = profile.company_name if profile else ''
            can_manage = getattr(profile, 'can_manage_consultants', False)
            writer.writerow([
                user.username,
                user.get_full_name() or '',
                user.email or '',
                dept,
                company,
                'Yes' if can_manage else 'No',
                user.date_joined.strftime('%Y-%m-%d %H:%M'),
            ])
        return response


class EmployeeListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = User
    template_name = 'users/employee_list.html'
    context_object_name = 'employees'
    paginate_by = 20

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def get_queryset(self):
        return _get_employee_list_queryset(self.request)

    def get_template_names(self):
        if self.request.headers.get('HX-Request'):
            return ['users/_employee_list_partial.html']
        return super().get_template_names()

class EmployeeDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = User
    template_name = 'users/employee_detail_v2.html'
    context_object_name = 'employee'

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def get_queryset(self):
        return User.objects.filter(role=User.Role.EMPLOYEE)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_admin'] = True
        return context



class EmployeeCreateView(LoginRequiredMixin, UserPassesTestMixin, BaseView):
    template_name = 'users/employee_create.html'

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def get(self, request):
        form = EmployeeCreateForm()
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = EmployeeCreateForm(request.POST)
        if form.is_valid():
            user, password, generated = form.save()
            if generated:
                msg = f'Employee "{user.get_full_name() or user.username}" created! Auto-generated password: {password}'
            else:
                msg = f'Employee "{user.get_full_name() or user.username}" created successfully!'
            messages.success(request, msg)
            return redirect('employee-detail', pk=user.pk)
        return render(request, self.template_name, {'form': form})

class EmployeeEditView(LoginRequiredMixin, UserPassesTestMixin, BaseView):
    """Admin can edit any employee's profile."""
    template_name = 'users/profile_form.html'

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def _get_employee(self):
        return get_object_or_404(User, pk=self.kwargs['pk'], role=User.Role.EMPLOYEE)

    def get(self, request, pk):
        employee = self._get_employee()
        # Ensure profile exists
        if not hasattr(employee, 'employee_profile'):
            EmployeeProfile.objects.create(user=employee)
            employee.refresh_from_db()

        user_form = UserProfileForm(instance=employee, prefix='user')
        profile_form = EmployeeProfileForm(instance=employee.employee_profile, prefix='profile')
        return render(request, self.template_name, {
            'form_title': f'Edit Employee: {employee.get_full_name() or employee.username}',
            'user_form': user_form,
            'profile_form': profile_form,
            'cancel_url': reverse_lazy('employee-detail', kwargs={'pk': pk}),
            'multi_form': True,
        })

    def post(self, request, pk):
        employee = self._get_employee()
        # Ensure profile exists
        if not hasattr(employee, 'employee_profile'):
             EmployeeProfile.objects.create(user=employee)
             employee.refresh_from_db()

        user_form = UserProfileForm(request.POST, instance=employee, prefix='user')
        profile_form = EmployeeProfileForm(request.POST, instance=employee.employee_profile, prefix='profile')
        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, 'Employee profile updated successfully.')
            return redirect('employee-detail', pk=pk)
        return render(request, self.template_name, {
            'form_title': f'Edit Employee: {employee.get_full_name() or employee.username}',
            'user_form': user_form,
            'profile_form': profile_form,
            'cancel_url': reverse_lazy('employee-detail', kwargs={'pk': pk}),
            'multi_form': True,
        })


class ConsultantEditView(LoginRequiredMixin, UserPassesTestMixin, BaseView):
    """Admin or consultant themselves can edit the profile."""
    template_name = 'users/profile_form.html'

    def test_func(self):
        u = self.request.user
        is_admin = u.is_superuser or u.role == 'ADMIN'
        is_owner = u.pk == self.kwargs.get('pk')
        return is_admin or is_owner

    def _get_consultant(self):
        return get_object_or_404(User, pk=self.kwargs['pk'], role=User.Role.CONSULTANT)

    def get(self, request, pk):
        consultant = self._get_consultant()
        user_form = UserProfileForm(instance=consultant, prefix='user')
        profile_form = ConsultantProfileEditForm(instance=consultant.consultant_profile, prefix='profile', user=request.user)
        return render(request, self.template_name, {
            'form_title': f'Edit Consultant: {consultant.get_full_name() or consultant.username}',
            'user_form': user_form,
            'profile_form': profile_form,
            'cancel_url': reverse_lazy('consultant-detail', kwargs={'pk': pk}),
            'multi_form': True,
        })

    def post(self, request, pk):
        consultant = self._get_consultant()
        user_form = UserProfileForm(request.POST, instance=consultant, prefix='user')
        profile_form = ConsultantProfileEditForm(request.POST, instance=consultant.consultant_profile, prefix='profile', user=request.user)
        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, 'Consultant profile updated successfully.')
            return redirect('consultant-detail', pk=pk)
        return render(request, self.template_name, {
            'form_title': f'Edit Consultant: {consultant.get_full_name() or consultant.username}',
            'user_form': user_form,
            'profile_form': profile_form,
            'cancel_url': reverse_lazy('consultant-detail', kwargs={'pk': pk}),
            'multi_form': True,
        })

class PublicConsultantProfileView(View):
    """
    Shareable public profile at /c/<slug>/ (e.g. /c/john-doe).
    Shown when PlatformConfig.enable_public_consultant_view is True.
    """
    template_name = 'users/consultant_public_profile.html'

    def get(self, request, slug):
        config = PlatformConfig.load()
        if not getattr(config, 'enable_public_consultant_view', True):
            return render(request, 'users/consultant_public_profile_disabled.html', status=404)
        profile = get_object_or_404(
            ConsultantProfile.objects.select_related('user').prefetch_related(
                'marketing_roles', 'experience', 'education', 'certifications'
            ),
            profile_slug=slug,
        )
        context = {
            'profile': profile,
            'consultant': profile.user,
            'experiences': profile.experience.all(),
            'educations': profile.education.all(),
            'certifications': profile.certifications.all(),
            'site_name': getattr(config, 'site_name', ''),
        }
        return render(request, self.template_name, context)


class ConsultantDetailView(LoginRequiredMixin, DetailView):
    model = User
    template_name = 'users/consultant_detail.html'
    context_object_name = 'consultant'

    def get_queryset(self):
        return User.objects.filter(role=User.Role.CONSULTANT, consultant_profile__isnull=False)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = self.object.consultant_profile
        context['experiences'] = profile.experience.all() if profile else []
        context['educations'] = profile.education.all() if profile else []
        context['certifications'] = profile.certifications.all() if profile else []
        
        context['is_own_profile'] = self.request.user == self.object
        context['is_admin'] = self.request.user.is_superuser or self.request.user.role == 'ADMIN'
        context['is_employee'] = self.request.user.role == 'EMPLOYEE'
        context['consultant_pk'] = self.object.pk
        context['matched_jobs'] = (
            match_jobs_for_consultant(profile, limit=8) if profile else []
        )

        # Resume Drafts (Admin/Employee only)
        if context['is_admin'] or context['is_employee']:
            context['resume_drafts'] = profile.resume_drafts.all() if profile else []
            from resumes.forms import DraftGenerateForm
            
            # Filter jobs: OPEN + Matches Consultant's Marketing Roles
            roles = profile.marketing_roles.all() if profile else []
            form = DraftGenerateForm()
            if roles:
                form.fields['job'].queryset = Job.objects.filter(
                    status='OPEN',
                    marketing_roles__in=roles
                ).distinct()
            else:
                form.fields['job'].queryset = Job.objects.none()
            
            context['draft_form'] = form
            if profile:
                context['claimed_job_ids'] = set(
                    ApplicationSubmission.objects.filter(consultant=profile).values_list('job_id', flat=True)
                )

            # LLM Input (latest draft)
            latest_draft = None
            if profile:
                latest_draft = profile.resume_drafts.order_by('-created_at').first()
            context['latest_draft'] = latest_draft
            if latest_draft:
                from resumes.models import MasterPrompt
                from resumes.services import get_system_prompt_text
                master = MasterPrompt.get_active()
                context['llm_system_prompt'] = latest_draft.llm_system_prompt or (master.system_prompt if master else "")
                context['llm_user_prompt'] = latest_draft.llm_user_prompt or ""
                context['active_master_prompt'] = master
                context['llm_input_summary'] = latest_draft.llm_input_summary or {}

        return context


# --- Mixin: Consultant owner OR admin can edit ---
class ConsultantOwnerMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        u = self.request.user
        is_admin = u.is_superuser or u.role == 'ADMIN'
        is_owner = u.role == User.Role.CONSULTANT and hasattr(u, 'consultant_profile')
        
        # Employee Permission Check
        is_permitted_employee = False
        if u.role == User.Role.EMPLOYEE and hasattr(u, 'employee_profile'):
             is_permitted_employee = u.employee_profile.can_manage_consultants

        return is_admin or is_owner or is_permitted_employee

    def get_profile(self):
        """Return the consultant profile being edited. Admins pass consultant_pk in URL."""
        u = self.request.user
        cpk = self.kwargs.get('consultant_pk')
        if cpk:
            return get_object_or_404(ConsultantProfile, user__pk=cpk)
        return u.consultant_profile

    def _redirect_pk(self):
        """Return the user PK to redirect to after save."""
        cpk = self.kwargs.get('consultant_pk')
        return int(cpk) if cpk else self.request.user.pk


# --- Experience CRUD ---
class ExperienceCreateView(ConsultantOwnerMixin, CreateView):
    model = Experience
    form_class = ExperienceForm
    template_name = 'users/profile_form.html'

    def form_valid(self, form):
        form.instance.consultant_profile = self.get_profile()
        messages.success(self.request, MSG_EXPERIENCE_ADDED)
        return super().form_valid(form)
    
    def get_success_url(self):
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_title'] = 'Add Experience'
        return context

class ExperienceUpdateView(ConsultantOwnerMixin, UpdateView):
    model = Experience
    form_class = ExperienceForm
    template_name = 'users/profile_form.html'
    
    def get_queryset(self):
        return Experience.objects.filter(consultant_profile=self.get_profile())

    def get_success_url(self):
        messages.success(self.request, MSG_EXPERIENCE_UPDATED)
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_title'] = 'Edit Experience'
        return context

class ExperienceDeleteView(ConsultantOwnerMixin, DeleteView):
    model = Experience
    template_name = 'users/profile_confirm_delete.html'
    
    def get_queryset(self):
        return Experience.objects.filter(consultant_profile=self.get_profile())

    def get_success_url(self):
        messages.success(self.request, MSG_EXPERIENCE_DELETED)
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})


# --- Education CRUD ---
class EducationCreateView(ConsultantOwnerMixin, CreateView):
    model = Education
    form_class = EducationForm
    template_name = 'users/profile_form.html'

    def form_valid(self, form):
        form.instance.consultant_profile = self.get_profile()
        messages.success(self.request, MSG_EDUCATION_ADDED)
        return super().form_valid(form)
    
    def get_success_url(self):
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_title'] = 'Add Education'
        return context

class EducationUpdateView(ConsultantOwnerMixin, UpdateView):
    model = Education
    form_class = EducationForm
    template_name = 'users/profile_form.html'
    
    def get_queryset(self):
        return Education.objects.filter(consultant_profile=self.get_profile())

    def get_success_url(self):
        messages.success(self.request, MSG_EDUCATION_UPDATED)
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_title'] = 'Edit Education'
        return context

class EducationDeleteView(ConsultantOwnerMixin, DeleteView):
    model = Education
    template_name = 'users/profile_confirm_delete.html'
    
    def get_queryset(self):
        return Education.objects.filter(consultant_profile=self.get_profile())

    def get_success_url(self):
        messages.success(self.request, MSG_EDUCATION_DELETED)
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})


# --- Certification CRUD ---
class CertificationCreateView(ConsultantOwnerMixin, CreateView):
    model = Certification
    form_class = CertificationForm
    template_name = 'users/profile_form.html'

    def form_valid(self, form):
        form.instance.consultant_profile = self.get_profile()
        messages.success(self.request, MSG_CERT_ADDED)
        return super().form_valid(form)
    
    def get_success_url(self):
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_title'] = 'Add Certification'
        return context

class CertificationUpdateView(ConsultantOwnerMixin, UpdateView):
    model = Certification
    form_class = CertificationForm
    template_name = 'users/profile_form.html'
    
    def get_queryset(self):
        return Certification.objects.filter(consultant_profile=self.get_profile())

    def get_success_url(self):
        messages.success(self.request, MSG_CERT_UPDATED)
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form_title'] = 'Edit Certification'
        return context

class CertificationDeleteView(ConsultantOwnerMixin, DeleteView):
    model = Certification
    template_name = 'users/profile_confirm_delete.html'
    
    def get_queryset(self):
        return Certification.objects.filter(consultant_profile=self.get_profile())

    def get_success_url(self):
        messages.success(self.request, MSG_CERT_DELETED)
        return reverse_lazy('consultant-detail', kwargs={'pk': self._redirect_pk()})


# --- Consultant Dashboard ---
class ConsultantDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'users/consultant_dashboard.html'

    def test_func(self):
        return self.request.user.role == User.Role.CONSULTANT

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        profile = user.consultant_profile
        
        # My Applications
        my_submissions = ApplicationSubmission.objects.filter(consultant=profile).select_related('job', 'resume')
        context['total_applications'] = my_submissions.count()
        context['pending_applications'] = my_submissions.filter(status='APPLIED').count()
        context['active_applications'] = my_submissions.exclude(status__in=['REJECTED', 'WITHDRAWN']).count()
        context['recent_submissions'] = my_submissions.order_by('-created_at')[:5]
        
        # Status Breakdown
        status_breakdown_qs = my_submissions.values('status').annotate(count=Count('status'))
        status_breakdown = list(status_breakdown_qs)
        context['status_breakdown'] = status_breakdown

        # Chart-friendly data for status breakdown
        status_display_map = dict(ApplicationSubmission.Status.choices)
        context['status_chart_labels'] = json.dumps(
            [status_display_map.get(row['status'], row['status']) for row in status_breakdown],
            cls=DjangoJSONEncoder,
        )
        context['status_chart_data'] = json.dumps(
            [row['count'] for row in status_breakdown],
            cls=DjangoJSONEncoder,
        )
        
        # Recommended Jobs (AI-style matching)
        context['recommended_jobs'] = match_jobs_for_consultant(profile, limit=5)

        # Recent Open Jobs (fallback / extra section)
        context['recent_jobs'] = Job.objects.filter(status='OPEN').order_by('-created_at')[:5]
        
        # Saved Jobs
        if hasattr(user, 'saved_jobs'):
            context['saved_jobs'] = user.saved_jobs.all()[:5]
        else:
            context['saved_jobs'] = []

        # Tracking: Drafts / In Progress / Submitted
        drafts_qs = ResumeDraft.objects.filter(
            consultant=profile,
            status__in=[ResumeDraft.Status.DRAFT, ResumeDraft.Status.FINAL],
        ).select_related('job')
        claimed_job_ids = my_submissions.values_list('job_id', flat=True)
        context['draft_tracking'] = drafts_qs.exclude(job_id__in=claimed_job_ids)

        context['in_progress_submissions'] = my_submissions.filter(
            status=ApplicationSubmission.Status.IN_PROGRESS
        )
        context['submitted_submissions'] = my_submissions.exclude(
            status=ApplicationSubmission.Status.IN_PROGRESS
        )

        # Pipeline snapshot counts
        context['count_draft'] = context['draft_tracking'].count()
        context['count_in_progress'] = context['in_progress_submissions'].count()
        context['count_submitted'] = my_submissions.exclude(status=ApplicationSubmission.Status.IN_PROGRESS).count()
        context['count_active'] = my_submissions.exclude(status__in=[ApplicationSubmission.Status.REJECTED, ApplicationSubmission.Status.WITHDRAWN]).count()
        context['count_interview'] = my_submissions.filter(status=ApplicationSubmission.Status.INTERVIEW).count()
        context['count_rejected'] = my_submissions.filter(status=ApplicationSubmission.Status.REJECTED).count()
        context['count_responses'] = SubmissionResponse.objects.filter(submission__consultant=profile).count()
        context['recent_interviews'] = Interview.objects.filter(consultant=profile).order_by('-scheduled_at')[:5]
        context['needs_onboarding'] = bool(profile and not profile.onboarding_completed_at)

        context['readiness_score'] = compute_consultant_readiness(profile, my_submissions)
        _at_risk = at_risk_submissions_queryset(profile)
        context['at_risk_count'] = _at_risk.count()
        context['at_risk_submissions_preview'] = _at_risk[:5]

        return context


class ConsultantJourneyView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Readiness score, journey checklist, and alerts for stale / dead job links."""

    template_name = 'users/consultant_journey.html'

    def test_func(self):
        return self.request.user.role == User.Role.CONSULTANT

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        profile = user.consultant_profile
        my_submissions = ApplicationSubmission.objects.filter(consultant=profile).select_related('job')
        context['readiness_score'] = compute_consultant_readiness(profile, my_submissions)
        context['journey_steps'] = build_journey_steps(profile, my_submissions)
        context['at_risk_submissions'] = at_risk_submissions_queryset(profile)[:20]
        return context


class ConsultantOnboardingView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Three-step wizard: bio/skills → availability → finish."""

    template_name = 'users/consultant_onboarding.html'

    def test_func(self):
        return self.request.user.role == User.Role.CONSULTANT

    def get(self, request):
        profile = request.user.consultant_profile
        if profile.onboarding_completed_at:
            messages.info(request, "You’ve already completed onboarding.")
            return redirect('consultant-dashboard')
        try:
            step = int(request.GET.get('step', 1))
        except ValueError:
            step = 1
        step = max(1, min(3, step))
        form1 = ConsultantOnboardingStep1Form(
            initial={
                'bio': profile.bio,
                'skills_text': ', '.join(profile.skills or []),
            }
        )
        form2 = ConsultantOnboardingStep2Form(instance=profile)
        return render(
            request,
            self.template_name,
            {'step': step, 'form1': form1, 'form2': form2},
        )

    def post(self, request):
        profile = request.user.consultant_profile
        if profile.onboarding_completed_at:
            return redirect('consultant-dashboard')
        try:
            step = int(request.POST.get('step', 1))
        except ValueError:
            step = 1
        if step == 1:
            form = ConsultantOnboardingStep1Form(request.POST)
            if form.is_valid():
                profile.bio = form.cleaned_data.get('bio') or ''
                raw = form.cleaned_data.get('skills_text', '')
                profile.skills = [s.strip() for s in raw.split(',') if s.strip()]
                profile.save()
                return redirect(f"{reverse('consultant-onboarding')}?step=2")
            form2 = ConsultantOnboardingStep2Form(instance=profile)
            return render(
                request,
                self.template_name,
                {'step': 1, 'form1': form, 'form2': form2},
            )
        if step == 2:
            form = ConsultantOnboardingStep2Form(request.POST, instance=profile)
            if form.is_valid():
                form.save()
                return redirect(f"{reverse('consultant-onboarding')}?step=3")
            form1 = ConsultantOnboardingStep1Form(
                initial={
                    'bio': profile.bio,
                    'skills_text': ', '.join(profile.skills or []),
                }
            )
            return render(
                request,
                self.template_name,
                {'step': 2, 'form1': form1, 'form2': form},
            )
        if step == 3:
            profile.onboarding_completed_at = timezone.now()
            profile.save(update_fields=['onboarding_completed_at'])
            messages.success(request, "Your profile setup is complete.")
            return redirect('consultant-dashboard')
        return redirect('consultant-onboarding')


# --- Saved Jobs ---
class SaveJobView(LoginRequiredMixin, BaseView):
    """Toggle save/unsave a job for a consultant."""
    def post(self, request, pk):
        if request.user.role != User.Role.CONSULTANT:
            messages.error(request, MSG_ONLY_CONSULTANTS_SAVE)
            return redirect('job-list')
        
        job = get_object_or_404(Job, pk=pk)
        saved, created = SavedJob.objects.get_or_create(user=request.user, job=job)
        
        if not created:
            saved.delete()
            messages.info(request, MSG_JOB_UNSAVED.format(title=job.title))
        else:
            messages.success(request, MSG_JOB_SAVED.format(title=job.title))
        
        next_url = request.POST.get('next', request.META.get('HTTP_REFERER', '/'))
        return redirect(next_url)


class SavedJobListView(LoginRequiredMixin, ListView):
    model = SavedJob
    template_name = 'users/saved_jobs.html'
    context_object_name = 'saved_jobs'
    paginate_by = 10

    def get_queryset(self):
        return SavedJob.objects.filter(user=self.request.user).select_related('job')


# --- Admin: Add Consultant ---
class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'


class ConsultantCreateView(AdminRequiredMixin, BaseView):
    template_name = 'users/consultant_create.html'

    def get(self, request):
        form = ConsultantCreateForm()
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        form = ConsultantCreateForm(request.POST)
        if form.is_valid():
            user, password, generated = form.save()
            if generated:
                msg = f'Consultant "{user.get_full_name() or user.username}" created! Auto-generated password: {password}'
            else:
                msg = f'Consultant "{user.get_full_name() or user.username}" created successfully!'
            messages.success(request, msg)
            return redirect('consultant-detail', pk=user.pk)
        return render(request, self.template_name, {'form': form})


# ─── Marketing Role CRUD (Admin only) ─────────────────────────────────
class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == "ADMIN"
        # return self.request.user.is_superuser or self.request.user.role == 'ADMIN'


class MarketingRoleListView(AdminRequiredMixin, ListView):
    model = MarketingRole
    template_name = 'users/marketing_role_list.html'
    context_object_name = 'roles'


class MarketingRoleCreateView(AdminRequiredMixin, CreateView):
    model = MarketingRole
    form_class = MarketingRoleForm
    template_name = 'users/marketing_role_form.html'
    success_url = reverse_lazy('marketing-role-list')

    def form_valid(self, form):
        messages.success(self.request, f'Marketing role "{form.cleaned_data["name"]}" created!')
        return super().form_valid(form)


class MarketingRoleUpdateView(AdminRequiredMixin, UpdateView):
    model = MarketingRole
    form_class = MarketingRoleForm
    template_name = 'users/marketing_role_form.html'
    success_url = reverse_lazy('marketing-role-list')

    def form_valid(self, form):
        messages.success(self.request, f'Marketing role "{form.cleaned_data["name"]}" updated!')
        return super().form_valid(form)


class MarketingRoleDeleteView(AdminRequiredMixin, DeleteView):
    model = MarketingRole
    template_name = 'users/marketing_role_confirm_delete.html'
    success_url = reverse_lazy('marketing-role-list')

    def form_valid(self, form):
        messages.success(self.request, f'Marketing role "{self.object.name}" deleted!')
        return super().form_valid(form)


class EmailNotificationPreferencesView(LoginRequiredMixin, UpdateView):
    """Per-user email and in-app (bell) notification toggles."""

    model = UserEmailNotificationPreferences
    form_class = UserEmailNotificationPreferencesForm
    template_name = 'users/email_notification_preferences.html'
    success_url = reverse_lazy('email-notification-preferences')

    def get_object(self, queryset=None):
        obj, _ = UserEmailNotificationPreferences.objects.get_or_create(user=self.request.user)
        return obj

    def form_valid(self, form):
        messages.success(self.request, 'Notification preferences saved.')
        return super().form_valid(form)


class SettingsView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'settings/dashboard.html'
    
    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        platform = PlatformConfig.load()
        logs = LLMUsageLog.objects.all()

        context.update(
            {
                "platform_config": platform,
                "maintenance_mode": platform.maintenance_mode,
                "enable_consultant_registration": platform.enable_consultant_registration,
                "enable_job_applications": platform.enable_job_applications,
                "llm_total_calls": logs.count(),
                "llm_success_calls": logs.filter(success=True).count(),
                "llm_failed_calls": logs.filter(success=False).count(),
            }
        )
        return context

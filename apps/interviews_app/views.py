import calendar
import csv
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import ListView, CreateView, UpdateView, DetailView, TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy
from django.utils import timezone
from django import forms
from django.http import HttpResponse
from django.contrib import messages
from django.db.models import Count

from .models import Interview
from submissions.models import ApplicationSubmission
from users.models import User
from core.models import PlatformConfig


def _get_user_timezone(user):
    """
    Return a tzinfo for the given user. Falls back to Django's default timezone.
    """
    from zoneinfo import ZoneInfo

    tz_name = getattr(user, 'timezone', None)
    if not tz_name:
        return timezone.get_default_timezone()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.get_default_timezone()


def _can_access_interviews(user):
    """Consultant, Admin, Employee (and superuser) can access interview list/calendar/detail/export."""
    if not user.is_authenticated:
        return False
    return (
        user.role == User.Role.CONSULTANT
        or user.role == User.Role.ADMIN
        or user.role == User.Role.EMPLOYEE
        or user.is_superuser
    )


class InterviewListAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Allow Consultant (own), Admin/Employee (all). No 403 – redirect others with message."""
    def test_func(self):
        return _can_access_interviews(self.request.user)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        messages.error(self.request, "Interviews are only available for consultants, admins, and employees.")
        if self.request.user.is_superuser or self.request.user.role == User.Role.ADMIN:
            return redirect("admin-dashboard")
        if self.request.user.role == User.Role.EMPLOYEE:
            return redirect("employee-dashboard")
        return redirect("home")


class ConsultantOnlyMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Only consultants (for create/edit their own)."""
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.role == User.Role.CONSULTANT

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        messages.error(self.request, "Only consultants can schedule or edit their own interviews.")
        if self.request.user.is_superuser or self.request.user.role == User.Role.ADMIN:
            return redirect("admin-dashboard")
        if self.request.user.role == User.Role.EMPLOYEE:
            return redirect("employee-dashboard")
        return redirect("home")


class InterviewForm(forms.ModelForm):
    class Meta:
        model = Interview
        # Keep only what the consultant really needs to choose.
        # Job title / company / location are always taken from the selected submission's job.
        fields = ['submission', 'round', 'scheduled_at', 'status', 'notes']
        widgets = {
            'scheduled_at': forms.DateTimeInput(
                format='%Y-%m-%d %H:%M',
                attrs={
                    'type': 'text',
                    'placeholder': 'Click to pick date & time',
                    'autocomplete': 'off',
                    'class': 'flatpickr-datetime',
                },
            ),
        }

    def __init__(self, *args, **kwargs):
        consultant = kwargs.pop('consultant', None)
        self.user_tz = kwargs.pop('user_tz', timezone.get_default_timezone())
        super().__init__(*args, **kwargs)
        if consultant is not None:
            self.fields['submission'].queryset = ApplicationSubmission.objects.filter(consultant=consultant)
        self.fields['scheduled_at'].input_formats = ['%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']
        self.fields['submission'].required = True

    def clean_scheduled_at(self):
        """
        Interpret the entered datetime as being in the user's local timezone,
        then convert and store it as UTC.
        """
        dt = self.cleaned_data.get('scheduled_at')
        if not dt:
            return dt

        user_tz = self.user_tz or timezone.get_default_timezone()

        # Treat naive value as user-local time.
        if timezone.is_naive(dt):
            local_dt = dt.replace(tzinfo=user_tz)
        else:
            local_dt = dt.astimezone(user_tz)

        utc_dt = local_dt.astimezone(timezone.utc)

        now_utc = timezone.now()
        is_edit = self.instance and self.instance.pk

        # Allow keeping the same timestamp on edit without complaining, but block new past times.
        if not is_edit or (self.instance and utc_dt != self.instance.scheduled_at):
            if utc_dt < now_utc:
                raise forms.ValidationError("You can't schedule an interview in the past.")

        max_future = now_utc + timedelta(days=180)
        if utc_dt > max_future:
            raise forms.ValidationError("Date is too far out. Interviews can be scheduled up to 6 months ahead.")

        return utc_dt

    def clean(self):
        cleaned = super().clean()
        submission = cleaned.get('submission')
        scheduled_at = cleaned.get('scheduled_at')
        if submission and scheduled_at:
            dup_qs = Interview.objects.filter(
                submission=submission,
                scheduled_at=scheduled_at,
            )
            if self.instance and self.instance.pk:
                dup_qs = dup_qs.exclude(pk=self.instance.pk)
            if dup_qs.exists():
                raise forms.ValidationError("An interview for this submission is already scheduled at this exact time.")
        return cleaned


def _get_interview_list_queryset(request):
    """
    Consultant: only their interviews. Admin/Employee: all interviews (candidate list).
    """
    if not _can_access_interviews(request.user):
        return Interview.objects.none()
    if request.user.role == User.Role.CONSULTANT:
        profile = request.user.consultant_profile
        qs = Interview.objects.filter(consultant=profile)
    else:
        qs = Interview.objects.all()
    qs = qs.select_related('submission', 'consultant', 'consultant__user').order_by('-scheduled_at')
    status = request.GET.get('status')
    when = request.GET.get('when')
    if status:
        qs = qs.filter(status=status)
    if when == 'upcoming':
        qs = qs.filter(scheduled_at__gte=timezone.now())
    elif when == 'past':
        qs = qs.filter(scheduled_at__lt=timezone.now())
    return qs


class InterviewListView(InterviewListAccessMixin, ListView):
    model = Interview
    template_name = 'interviews/interview_list.html'
    context_object_name = 'interviews'

    def get_queryset(self):
        return _get_interview_list_queryset(self.request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['selected_status'] = self.request.GET.get('status', '')
        context['selected_when'] = self.request.GET.get('when', 'all')

        # Status summary + totals for the CURRENT visible list
        page_obj = context.get('page_obj')
        interviews = context.get('interviews')
        if hasattr(interviews, 'object_list'):
            items = list(interviews.object_list)
        else:
            items = list(interviews or [])

        summary = {code: 0 for code, _ in Interview.Status.choices}
        for obj in items:
            if obj.status in summary:
                summary[obj.status] += 1

        context['status_summary'] = summary
        if page_obj is not None and hasattr(page_obj, 'paginator'):
            context['total_interviews'] = page_obj.paginator.count
        else:
            context['total_interviews'] = len(items)

        context['show_candidate_column'] = (
            self.request.user.is_superuser
            or self.request.user.role == User.Role.ADMIN
            or self.request.user.role == User.Role.EMPLOYEE
        )
        return context


class InterviewExportCSVView(InterviewListAccessMixin, View):
    """Export interview list as CSV. Consultant: own; Admin/Employee: all (with Candidate column)."""

    def get(self, request, *args, **kwargs):
        qs = _get_interview_list_queryset(request)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="interviews.csv"'
        writer = csv.writer(response)
        show_candidate = _can_access_interviews(request.user) and request.user.role != User.Role.CONSULTANT
        headers = ['Scheduled At', 'Job Title', 'Company', 'Location', 'Round', 'Status', 'Notes', 'Created At']
        if show_candidate:
            headers.insert(1, 'Candidate')
        writer.writerow(headers)
        for obj in qs:
            row = [
                obj.scheduled_at.strftime('%Y-%m-%d %H:%M') if obj.scheduled_at else '',
                obj.job_title or '',
                obj.company or '',
                obj.location or '',
                obj.get_round_display(),
                obj.get_status_display(),
                (obj.notes or '')[:500],
                obj.created_at.strftime('%Y-%m-%d %H:%M') if obj.created_at else '',
            ]
            if show_candidate:
                candidate_name = obj.consultant.user.get_full_name() or obj.consultant.user.username
                row.insert(1, candidate_name)
            writer.writerow(row)
        return response


class InterviewCreateView(ConsultantOnlyMixin, CreateView):
    model = Interview
    form_class = InterviewForm
    template_name = 'interviews/interview_form.html'
    success_url = reverse_lazy('interview-list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['consultant'] = self.request.user.consultant_profile
        kwargs['user_tz'] = _get_user_timezone(self.request.user)
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        job_id = self.request.GET.get('job')
        if job_id and self.request.user.role == User.Role.CONSULTANT:
            profile = self.request.user.consultant_profile
            sub = (
                ApplicationSubmission.objects.filter(consultant=profile, job_id=job_id)
                .select_related('job')
                .first()
            )
            if sub:
                initial['submission'] = sub
        return initial

    def form_valid(self, form):
        interview = form.save(commit=False)
        interview.consultant = self.request.user.consultant_profile
        if interview.submission:
            job = interview.submission.job
            interview.job_title = job.title
            interview.company = job.company
            interview.location = job.location
        interview.save()
        return redirect(self.success_url)


class InterviewUpdateView(ConsultantOnlyMixin, UpdateView):
    model = Interview
    form_class = InterviewForm
    template_name = 'interviews/interview_form.html'
    success_url = reverse_lazy('interview-list')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['consultant'] = self.request.user.consultant_profile
        kwargs['user_tz'] = _get_user_timezone(self.request.user)
        return kwargs

    def get_queryset(self):
        """
        Consultants may only edit their own interviews, even if they can
        see the global calendar.
        """
        return Interview.objects.filter(consultant=self.request.user.consultant_profile)

    def form_valid(self, form):
        interview = form.save(commit=False)
        if interview.submission:
            job = interview.submission.job
            interview.job_title = job.title
            interview.company = job.company
            interview.location = job.location
        interview.save()
        return redirect(self.success_url)


class InterviewDetailView(InterviewListAccessMixin, DetailView):
    model = Interview
    template_name = 'interviews/interview_detail.html'
    context_object_name = 'interview'

    def get_queryset(self):
        qs = Interview.objects.select_related('submission', 'consultant', 'consultant__user')
        config = PlatformConfig.load()
        # Without the global flag, consultants may only see their own interviews.
        if self.request.user.role == User.Role.CONSULTANT and not config.enable_consultant_global_interview_calendar:
            qs = qs.filter(consultant=self.request.user.consultant_profile)
        return qs


class InterviewCalendarView(InterviewListAccessMixin, TemplateView):
    template_name = 'interviews/interview_calendar.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user_tz = _get_user_timezone(self.request.user)
        now = timezone.now().astimezone(user_tz)
        config = PlatformConfig.load()

        # Base queryset: by default consultants see only their own interviews.
        # If the global calendar flag is enabled, consultants see all interviews like admins.
        if self.request.user.role == User.Role.CONSULTANT and not config.enable_consultant_global_interview_calendar:
            profile = self.request.user.consultant_profile
            base_qs = Interview.objects.filter(consultant=profile)
        else:
            base_qs = Interview.objects.all()

        month = int(self.request.GET.get('month', now.month))
        year = int(self.request.GET.get('year', now.year))
        company_slug = self.request.GET.get('company', '').strip()

        if company_slug:
            base_qs = base_qs.filter(company=company_slug)

        # Build a local-date calendar view:
        # 1) get all interviews for this calendar month (in UTC),
        # 2) convert to the user's timezone,
        # 3) bucket by local date.
        cal = calendar.Calendar(firstweekday=0)
        month_days = cal.monthdatescalendar(year, month)

        # Determine the local month range
        first_of_month = datetime(year, month, 1, 0, 0, tzinfo=user_tz)
        last_day = max(day.day for week in month_days for day in week if day.month == month)
        end_of_month = datetime(year, month, last_day, 23, 59, 59, tzinfo=user_tz)

        start_utc = first_of_month.astimezone(dt_timezone.utc)
        end_utc = end_of_month.astimezone(dt_timezone.utc)

        month_qs = (
            base_qs.filter(scheduled_at__gte=start_utc, scheduled_at__lte=end_utc)
            .select_related('consultant', 'consultant__user')
            .order_by('scheduled_at')
        )

        # Bucket by local date
        by_local_date = {}
        for interview in month_qs:
            local_dt = timezone.localtime(interview.scheduled_at, user_tz)
            local_date = local_dt.date()
            by_local_date.setdefault(local_date, []).append(interview)

        weeks = []
        for week in month_days:
            week_days = []
            for day in week:
                week_days.append({'date': day, 'items': by_local_date.get(day, [])})
            weeks.append(week_days)

        companies = list(
            Interview.objects.values_list('company', flat=True).distinct().order_by('company')
        )
        if self.request.user.role == User.Role.CONSULTANT:
            companies = list(
                Interview.objects.filter(consultant=self.request.user.consultant_profile)
                .values_list('company', flat=True).distinct().order_by('company')
            )

        # Position of \"now\" line within today's cell (0-100%), only for the visible month/year
        if year == now.year and month == now.month:
            seconds_since_midnight = (
                now.hour * 3600 + now.minute * 60 + now.second
            )
            context['now_percent'] = round(seconds_since_midnight / 86400 * 100, 2)
        else:
            context['now_percent'] = None

        context['month'] = month
        context['year'] = year
        context['year_range'] = range(timezone.now().year - 2, timezone.now().year + 5)
        context['month_name'] = calendar.month_name[month]
        context['weeks'] = weeks
        context['today'] = now.date()
        # Who can see candidate names on the calendar?
        # - Admin / Employee: always
        # - Consultant: only if the global calendar flag is enabled
        context['show_candidate'] = (
            self.request.user.role != User.Role.CONSULTANT
            or config.enable_consultant_global_interview_calendar
        )
        context['companies'] = companies
        context['selected_company'] = company_slug
        return context

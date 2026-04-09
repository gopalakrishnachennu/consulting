import csv
import re
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import CreateView, ListView, UpdateView, View, DetailView, TemplateView
from django.db.models import Q, Count, Max
from django.db import IntegrityError
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django.utils import timezone
from datetime import timedelta
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from .models import (
    ApplicationSubmission, Offer, OfferRound, record_submission_status_change, EmailEvent,
    Placement, Timesheet, Commission,
)
from .forms import (
    ApplicationSubmissionForm,
    SubmissionResponseForm,
    OfferRoundForm,
    OfferFinalTermsForm,
    OfferInitialForm,
    EmailEventReviewForm,
    PlacementForm,
    TimesheetForm,
    TimesheetApprovalForm,
    CommissionForm,
)
from resumes.models import Resume, ResumeDraft
from users.models import User, ConsultantProfile
from jobs.services import ensure_parsed_jd
from companies.models import Company, CompanyDoNotSubmit
from config.constants import (
    PAGINATION_SUBMISSIONS, MAX_UPLOAD_SIZE, MAX_UPLOAD_SIZE_MB,
    MSG_SUBMISSION_SUCCESS, MSG_SUBMISSION_MISMATCH, MSG_SUBMISSION_SELF_ONLY, MSG_FILE_TOO_LARGE,
)
from core.notification_utils import notify_submission_pipeline_event

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _strip_markdown(md: str) -> str:
    if not md:
        return ""
    text = md
    # Remove code blocks
    text = re.sub(r"```[\s\S]*?```", " ", text)
    # Remove inline code
    text = re.sub(r"`[^`]*`", " ", text)
    # Remove markdown links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove emphasis markers
    text = re.sub(r"[*_>#\-]{1,}", " ", text)
    return _norm(text)


def _tokenize_words(text: str) -> set[str]:
    text = _norm(text)
    return set(re.findall(r"[a-z0-9][a-z0-9\+\.\#\-]{1,}", text))


def _resume_structure_checks(resume_md: str) -> dict:
    """Heuristic ATS-style checks."""
    md = resume_md or ""
    low = md.lower()
    checks = {}
    checks["has_summary"] = "summary" in low or "professional summary" in low
    checks["has_skills_section"] = "skills" in low
    checks["has_experience_section"] = "experience" in low or "work experience" in low
    checks["has_education_section"] = "education" in low
    checks["has_projects_section"] = "projects" in low
    checks["has_certifications_section"] = "certification" in low or "certifications" in low
    checks["has_metrics_numbers"] = bool(re.search(r"\b\d{1,3}%\b|\b\d{4}\b|\b\d+\b", low))
    checks["has_tables"] = "|" in md and ("---" in md or "\n|" in md)
    checks["too_long"] = len(_strip_markdown(md)) > 9000
    checks["too_short"] = len(_strip_markdown(md)) < 800
    return checks

class SubmissionCreateView(LoginRequiredMixin, CreateView):
    model = ApplicationSubmission
    form_class = ApplicationSubmissionForm
    template_name = 'submissions/submission_form.html'
    success_url = reverse_lazy('submission-list')

    def get_initial(self):
        initial = super().get_initial()
        resume_id = self.request.GET.get('resume_id')
        if resume_id:
            resume = get_object_or_404(Resume, pk=resume_id)
            initial['resume'] = resume
            initial['job'] = resume.job
            initial['consultant'] = resume.consultant
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        resume_id = self.request.GET.get('resume_id')
        if resume_id:
            context['resume'] = get_object_or_404(Resume, pk=resume_id)
        return context

    def form_valid(self, form):
        job = form.cleaned_data['job']
        consultant = form.cleaned_data['consultant']
        resume = form.cleaned_data['resume']
        
        # 1. Consistency Check
        if not resume or resume.job != job or resume.consultant != consultant:
            messages.error(self.request, MSG_SUBMISSION_MISMATCH)
            return self.form_invalid(form)
            
        # 2. Permission Check
        if self.request.user.role == 'CONSULTANT' and consultant.user != self.request.user:
            messages.error(self.request, MSG_SUBMISSION_SELF_ONLY)
            return self.form_invalid(form)

        # 2b. Company blacklist / Do-Not-Submit checks
        company = getattr(job, "company_obj", None)
        if company and company.is_blacklisted:
            messages.error(self.request, "This company is blacklisted. Submissions are disabled by admin.")
            return self.form_invalid(form)
        if company:
            today = timezone.now().date()
            if CompanyDoNotSubmit.objects.filter(
                company=company,
                consultant=consultant,
            ).filter(
                Q(until__isnull=True) | Q(until__gte=today)
            ).exists():
                messages.error(
                    self.request,
                    "This consultant has a Do-Not-Submit restriction for this company.",
                )
                return self.form_invalid(form)

        # 3. File Validation (Basic)
        proof_file = form.cleaned_data.get('proof_file')
        if proof_file:
            if proof_file.size > MAX_UPLOAD_SIZE:
                form.add_error('proof_file', MSG_FILE_TOO_LARGE.format(max_mb=MAX_UPLOAD_SIZE_MB))
                return self.form_invalid(form)
            if not form.instance.submitted_at:
                form.instance.submitted_at = timezone.now()

        form.instance.submitted_by = self.request.user
        if proof_file and form.instance.status == ApplicationSubmission.Status.IN_PROGRESS:
            form.instance.status = ApplicationSubmission.Status.APPLIED
        messages.success(self.request, MSG_SUBMISSION_SUCCESS)
        try:
            response = super().form_valid(form)
        except IntegrityError:
            messages.error(
                self.request,
                "A submission for this consultant and job already exists. "
                "Please update the existing submission instead of creating a new one."
            )
            return self.form_invalid(form)
        record_submission_status_change(form.instance, form.instance.status, from_status=ApplicationSubmission.Status.IN_PROGRESS if proof_file else None)
        return response


class SubmissionQuickSubmitView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Pick consultant + job, then jump to submission create with the latest resume draft."""

    template_name = 'submissions/quick_submit.html'

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE, User.Role.CONSULTANT)

    def get(self, request):
        from jobs.models import Job

        jobs = Job.objects.filter(status=Job.Status.OPEN).order_by('-created_at')[:500]
        if request.user.role == User.Role.CONSULTANT:
            consultants = ConsultantProfile.objects.filter(
                pk=request.user.consultant_profile.pk
            ).select_related('user')
        else:
            consultants = (
                ConsultantProfile.objects.filter(status=ConsultantProfile.Status.ACTIVE)
                .select_related('user')
                .order_by('user__first_name', 'user__last_name')[:500]
            )
        return render(
            request,
            self.template_name,
            {'jobs': jobs, 'consultants': consultants},
        )

    def post(self, request):
        from jobs.models import Job

        job_id = request.POST.get('job_id')
        consultant_id = request.POST.get('consultant_id')
        if not job_id or not consultant_id:
            messages.error(request, "Choose both a job and a consultant.")
            return redirect('submission-quick-submit')
        job = get_object_or_404(Job, pk=job_id)
        if request.user.role == User.Role.CONSULTANT:
            consultant = request.user.consultant_profile
            if str(consultant.pk) != str(consultant_id):
                messages.error(request, MSG_SUBMISSION_SELF_ONLY)
                return redirect('submission-quick-submit')
        else:
            consultant = get_object_or_404(ConsultantProfile, pk=consultant_id)
        draft = (
            ResumeDraft.objects.filter(consultant=consultant, job=job)
            .exclude(status=ResumeDraft.Status.ERROR)
            .order_by('-version')
            .first()
        )
        if not draft or not (draft.content or '').strip():
            messages.error(
                request,
                "No resume draft with content found for this job. Generate a draft from the consultant profile first.",
            )
            return redirect('consultant-detail', pk=consultant.user.pk)
        return redirect(f"{reverse('submission-create')}?resume_id={draft.pk}")


class SubmissionListView(LoginRequiredMixin, ListView):
    model = ApplicationSubmission
    template_name = 'submissions/submission_list.html'
    context_object_name = 'submissions'
    paginate_by = 10

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status_choices'] = ApplicationSubmission.Status.choices
        # For header stats & total count, reuse the shared queryset (respecting role, filters).
        qs_all = _get_submission_queryset(self.request)
        status_counts = qs_all.values('status').annotate(count=Count('status'))
        summary = {code: 0 for code, _ in ApplicationSubmission.Status.choices}
        for row in status_counts:
            code = row['status']
            if code in summary:
                summary[code] = row['count']
        context['status_summary'] = summary
        context['total_submissions'] = qs_all.count()
        qd = self.request.GET.copy()
        qd.pop('page', None)
        context['pagination_query'] = qd.urlencode()
        return context

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        status = self.request.GET.get('status')
        search = self.request.GET.get('search')
        if user.role == User.Role.CONSULTANT:
            # Consultant sees their own submissions
            qs = qs.filter(consultant=user.consultant_profile)
        elif user.role in (User.Role.EMPLOYEE, User.Role.ADMIN) or user.is_superuser:
            # Employee/Admin/superuser see all submissions
            qs = qs
        else:
            return qs.none()

        if status:
            qs = qs.filter(status=status)
        if search:
            qs = qs.filter(Q(job__title__icontains=search) | Q(job__company__icontains=search))
        return qs


def _get_submission_queryset(request):
    """Shared queryset logic for list and CSV export (role, status, search)."""
    qs = ApplicationSubmission.objects.select_related('job', 'consultant__user')
    user = request.user
    if user.role == User.Role.CONSULTANT:
        qs = qs.filter(consultant=user.consultant_profile)
    elif user.role not in (User.Role.EMPLOYEE, User.Role.ADMIN) and not user.is_superuser:
        return ApplicationSubmission.objects.none()
    status = request.GET.get('status')
    search = request.GET.get('search')
    if status:
        qs = qs.filter(status=status)
    if search:
        qs = qs.filter(Q(job__title__icontains=search) | Q(job__company__icontains=search))
    return qs.order_by('-created_at')


class SubmissionExportCSVView(LoginRequiredMixin, View):
    """Export applications (submissions) as CSV with same filters as list view."""

    def get(self, request, *args, **kwargs):
        qs = _get_submission_queryset(request)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="applications.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Job Title', 'Company', 'Consultant', 'Status', 'Submitted At', 'Created At', 'Updated At'
        ])
        for sub in qs:
            writer.writerow([
                sub.job.title,
                sub.job.company,
                sub.consultant.user.get_full_name() or sub.consultant.user.username,
                sub.get_status_display(),
                sub.submitted_at.strftime('%Y-%m-%d %H:%M') if sub.submitted_at else '',
                sub.created_at.strftime('%Y-%m-%d %H:%M'),
                sub.updated_at.strftime('%Y-%m-%d %H:%M'),
            ])
        return response


class SubmissionBulkStatusView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Allow employee/admin to set status on multiple submissions at once."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.EMPLOYEE, User.Role.ADMIN)

    def post(self, request, *args, **kwargs):
        ids = request.POST.getlist('submission_ids')
        # Support both 'status' and 'new_status' so bulk form works (dropdown name is new_status to avoid conflict with filter)
        new_status = (request.POST.get('new_status') or request.POST.get('status') or '').strip()
        valid_statuses = {c[0] for c in ApplicationSubmission.Status.choices}
        next_query = (request.POST.get('next_query') or '').strip()

        def redirect_back():
            url = reverse('submission-list')
            if next_query:
                url += '?' + next_query
            return redirect(url)

        if not new_status or new_status not in valid_statuses:
            messages.error(request, 'Please select a valid status.')
            return redirect_back()

        if not ids:
            messages.warning(request, 'No applications selected. Select one or more applications using the checkboxes.')
            return redirect_back()

        for pk in ids:
            sub = get_object_or_404(ApplicationSubmission, pk=pk)
            old = sub.status
            sub.status = new_status
            sub.save(update_fields=['status', 'updated_at'])
            record_submission_status_change(sub, new_status, from_status=old)
            notify_submission_pipeline_event(
                sub,
                actor=request.user,
                old_status=old,
                new_status=new_status,
            )
        status_label = dict(ApplicationSubmission.Status.choices).get(new_status, new_status)
        messages.success(request, f'Updated {len(ids)} application(s) to {status_label}.')
        return redirect_back()


class SubmissionInlineStatusView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Lightweight endpoint for inline status changes from the list view.
    Intended for employee/admin workflows; responds with JSON.
    """

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.EMPLOYEE, User.Role.ADMIN)

    def post(self, request, pk, *args, **kwargs):
        submission = get_object_or_404(ApplicationSubmission, pk=pk)
        new_status = (request.POST.get('status') or '').strip()
        valid_statuses = {code for code, _ in ApplicationSubmission.Status.choices}

        if not new_status or new_status not in valid_statuses:
            return JsonResponse({'ok': False, 'error': 'Invalid status.'}, status=400)

        old_status = submission.status
        submission.status = new_status
        submission.save(update_fields=['status', 'updated_at'])
        record_submission_status_change(submission, new_status, from_status=old_status)
        notify_submission_pipeline_event(
            submission,
            actor=request.user,
            old_status=old_status,
            new_status=new_status,
        )
        label = submission.get_status_display()
        messages.success(request, f'Updated status to {label}.')
        return JsonResponse({'ok': True, 'status': new_status, 'label': label})


class SubmissionUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = ApplicationSubmission
    fields = ['status', 'notes', 'proof_file']
    template_name = 'submissions/submission_form.html'
    success_url = reverse_lazy('submission-list')
    
    def test_func(self):
        obj = self.get_object()
        u = self.request.user
        return (
            u == obj.consultant.user
            or u.role in (User.Role.EMPLOYEE, User.Role.ADMIN)
            or u.is_superuser
        )
    template_name = 'submissions/submission_form.html'
    success_url = reverse_lazy('submission-list')

    def form_valid(self, form):
        proof_file = form.cleaned_data.get('proof_file')
        if proof_file and not form.instance.submitted_at:
            form.instance.submitted_at = timezone.now()
        old_status = ApplicationSubmission.objects.filter(pk=form.instance.pk).values_list('status', flat=True).first()
        messages.success(self.request, "Submission updated successfully!")
        response = super().form_valid(form)
        if old_status is not None and old_status != form.instance.status:
            record_submission_status_change(form.instance, form.instance.status, from_status=old_status)
            notify_submission_pipeline_event(
                form.instance,
                actor=self.request.user,
                old_status=old_status,
                new_status=form.instance.status,
            )
        return response


class SubmissionDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = ApplicationSubmission
    template_name = 'submissions/submission_detail.html'
    context_object_name = 'submission'

    def test_func(self):
        obj = self.get_object()
        u = self.request.user
        if u.is_superuser or u.role in (User.Role.EMPLOYEE, User.Role.ADMIN):
            return True
        return (
            u.role == User.Role.CONSULTANT
            and hasattr(u, 'consultant_profile')
            and obj.consultant == u.consultant_profile
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['response_form'] = SubmissionResponseForm()
        # Application timeline: merge created, submitted, status history, interviews
        sub = self.object
        events = []
        if sub.created_at:
            events.append((sub.created_at, 'Application created', ''))
        if sub.submitted_at:
            events.append((sub.submitted_at, 'Proof submitted', ''))
        status_display = dict(ApplicationSubmission.Status.choices)
        for h in sub.status_history.all():
            label = "Status: " + status_display.get(h.to_status, h.to_status)
            events.append((h.created_at, label, h.note or ''))
        try:
            from interviews_app.models import Interview
            for i in sub.interviews.all().order_by('scheduled_at'):
                events.append((i.scheduled_at, f"Interview: {i.get_round_display()}", i.get_status_display()))
        except Exception:
            pass
        events.sort(key=lambda x: x[0])
        context['timeline_events'] = events
        # Offer negotiation (when status=OFFER)
        if sub.status == ApplicationSubmission.Status.OFFER:
            offer, _ = Offer.objects.get_or_create(submission=sub)
            context['offer'] = offer
            context['offer_rounds'] = offer.rounds.all()
            context['offer_round_form'] = OfferRoundForm()
            context['offer_final_form'] = OfferFinalTermsForm(instance=offer)
            context['offer_initial_form'] = OfferInitialForm(instance=offer)
        else:
            context['offer'] = None
        context['can_mark_rejected'] = (
            self.request.user.role == User.Role.CONSULTANT
            and hasattr(self.request.user, 'consultant_profile')
            and sub.consultant == self.request.user.consultant_profile
            and sub.status not in (ApplicationSubmission.Status.REJECTED, ApplicationSubmission.Status.OFFER, ApplicationSubmission.Status.PLACED)
        )
        context['can_place'] = (
            self.request.user.is_superuser
            or self.request.user.role in (User.Role.ADMIN, User.Role.EMPLOYEE)
        )
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        action = request.POST.get('action')
        can_edit_offer = request.user.is_superuser or request.user.role in (User.Role.EMPLOYEE, User.Role.ADMIN)

        if action == 'add_round' and can_edit_offer and self.object.status == ApplicationSubmission.Status.OFFER:
            offer, _ = Offer.objects.get_or_create(submission=self.object)
            form = OfferRoundForm(request.POST)
            if form.is_valid():
                round_obj = form.save(commit=False)
                round_obj.offer = offer
                round_obj.round_number = (offer.rounds.aggregate(m=Max('round_number'))['m'] or 0) + 1
                round_obj.save()
                messages.success(request, "Negotiation round added.")
                return redirect('submission-detail', pk=self.object.pk)
            context = self.get_context_data()
            context['offer_round_form'] = form
            return self.render_to_response(context)

        if action == 'set_initial' and can_edit_offer and self.object.status == ApplicationSubmission.Status.OFFER:
            offer, _ = Offer.objects.get_or_create(submission=self.object)
            form = OfferInitialForm(request.POST, instance=offer)
            if form.is_valid():
                form.save()
                messages.success(request, "Initial offer saved.")
                return redirect('submission-detail', pk=self.object.pk)
            context = self.get_context_data()
            context['offer_initial_form'] = form
            return self.render_to_response(context)

        if action == 'set_final' and can_edit_offer and self.object.status == ApplicationSubmission.Status.OFFER:
            offer, _ = Offer.objects.get_or_create(submission=self.object)
            form = OfferFinalTermsForm(request.POST, instance=offer)
            if form.is_valid():
                form.save()
                messages.success(request, "Final terms updated.")
                return redirect('submission-detail', pk=self.object.pk)
            context = self.get_context_data()
            context['offer_final_form'] = form
            return self.render_to_response(context)

        form = SubmissionResponseForm(request.POST)
        if form.is_valid():
            response = form.save(commit=False)
            response.submission = self.object
            response.created_by = request.user
            response.save()
            messages.success(request, "Response added.")
            return redirect('submission-detail', pk=self.object.pk)
        context = self.get_context_data()
        context['response_form'] = form
        return self.render_to_response(context)


class SubmissionMarkRejectedView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Consultant marks a submission as REJECTED (when they see it in email)."""

    def test_func(self):
        sub = get_object_or_404(ApplicationSubmission, pk=self.kwargs.get('pk'))
        u = self.request.user
        return (
            u.role == User.Role.CONSULTANT
            and hasattr(u, 'consultant_profile')
            and sub.consultant == u.consultant_profile
        )

    def post(self, request, pk, *args, **kwargs):
        sub = get_object_or_404(ApplicationSubmission, pk=pk)
        if sub.status == ApplicationSubmission.Status.OFFER:
            messages.error(request, "This submission is already marked as Offer; cannot mark rejected.")
            return redirect('submission-detail', pk=sub.pk)
        if sub.status != ApplicationSubmission.Status.REJECTED:
            old = sub.status
            sub.status = ApplicationSubmission.Status.REJECTED
            sub.save(update_fields=['status', 'updated_at'])
            record_submission_status_change(sub, ApplicationSubmission.Status.REJECTED, from_status=old, note="Marked rejected by consultant (email).")
            messages.success(request, "Marked as Rejected. Opening rejection analysis…")
        return redirect('submission-rejection-analysis', pk=sub.pk)


class RejectionAnalysisView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    """
    Rejection Analyzer: rules-based comparison of Resume vs JD.
    Designed to be low-token: no LLM calls.
    """
    model = ApplicationSubmission
    template_name = 'submissions/rejection_analysis.html'
    context_object_name = 'submission'

    def test_func(self):
        sub = self.get_object()
        u = self.request.user
        if u.is_superuser or u.role in (User.Role.EMPLOYEE, User.Role.ADMIN):
            return True
        return u.role == User.Role.CONSULTANT and hasattr(u, 'consultant_profile') and sub.consultant == u.consultant_profile

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sub = self.object
        job = sub.job
        consultant = sub.consultant

        # Ensure parsed_jd exists using rules-first parser (no AI)
        ensure_parsed_jd(job, actor=self.request.user)
        parsed = job.parsed_jd or {}
        required = [s.strip().lower() for s in (parsed.get('required_skills') or []) if isinstance(s, str) and s.strip()]
        required = list(dict.fromkeys(required))  # dedupe keep order

        resume_md = getattr(sub.resume, 'content', '') if sub.resume else ''
        resume_text = _strip_markdown(resume_md)
        resume_tokens = _tokenize_words(resume_text)

        # Coverage
        matched = []
        missing = []
        for s in required:
            # phrase match first, token fallback
            if (" " in s and s in resume_text) or (s in resume_tokens):
                matched.append(s)
            else:
                missing.append(s)

        consultant_skills = []
        try:
            consultant_skills = [x.strip().lower() for x in (consultant.skills or []) if isinstance(x, str) and x.strip()]
        except Exception:
            consultant_skills = []

        # Skills the consultant has but didn't highlight in resume
        not_highlighted = []
        for s in consultant_skills[:200]:
            if (" " in s and s in resume_text) or (s in resume_tokens):
                continue
            # only show if also in JD required list (high impact)
            if s in missing:
                not_highlighted.append(s)

        checks = _resume_structure_checks(resume_md)

        coverage_pct = round((len(matched) / len(required)) * 100) if required else None
        context.update(
            {
                "job_required_skills": required,
                "skills_matched": matched,
                "skills_missing": missing,
                "skills_missing_but_in_profile": not_highlighted,
                "coverage_pct": coverage_pct,
                "parsed_jd_source": parsed.get("source") or job.parsed_jd_status or "",
                "resume_checks": checks,
                "resume_char_count": len(resume_text),
            }
        )

        # Micro-level suggestions (rules-based)
        suggestions = []
        if required and coverage_pct is not None and coverage_pct < 60:
            suggestions.append(("Keyword coverage", f"Only {coverage_pct}% of JD skills appear in the resume. Add missing skills into a dedicated Skills section and weave them into Experience bullets."))
        if context["skills_missing_but_in_profile"]:
            suggestions.append(("Hidden strengths", f"You already have these skills in your profile but not in the resume: {', '.join(context['skills_missing_but_in_profile'][:12])}. Add them explicitly (ATS)."))
        if checks.get("too_short"):
            suggestions.append(("Resume depth", "Resume looks short. Add 2–4 strong bullets per recent role with metrics (impact, scale, tools)."))
        if checks.get("too_long"):
            suggestions.append(("Resume focus", "Resume looks long. Prioritize the most relevant experience and move older/irrelevant bullets to a short 'Other Experience' section."))
        if not checks.get("has_skills_section"):
            suggestions.append(("Structure", "Add a clear 'Skills' section near the top. ATS systems often weight this section heavily."))
        if not checks.get("has_experience_section"):
            suggestions.append(("Structure", "Add an 'Experience' section with role/company/date and bullet achievements."))
        if checks.get("has_tables"):
            suggestions.append(("ATS formatting", "Avoid tables/pipes in resumes; ATS can mis-read columns. Use simple headings and bullet lists."))
        if not checks.get("has_metrics_numbers"):
            suggestions.append(("Impact metrics", "Add numbers: %, $, time saved, scale, users, throughput. Metrics increase interview conversion."))
        if missing:
            suggestions.append(("Top missing skills", f"Highest-impact missing keywords to add: {', '.join(missing[:12])}"))

        context["suggestions"] = suggestions

        # Provide a "rewrite recipe" checklist
        rewrite_steps = [
            "Copy the JD 'required skills' list and ensure at least 70% appear verbatim in your resume (where truthful).",
            "For each missing skill, add 1 bullet in Experience showing how you used it (tool + action + result).",
            "Move the most relevant 2 projects/achievements to the top under Summary/Skills.",
            "Use consistent headings: Summary, Skills, Experience, Education, Certifications.",
            "Avoid graphics/tables; keep formatting simple for ATS.",
        ]
        context["rewrite_steps"] = rewrite_steps
        return context


class SubmissionClaimView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Create an IN_PROGRESS submission for a draft (claim job)."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def post(self, request, draft_id):
        draft = get_object_or_404(ResumeDraft, pk=draft_id)
        consultant = draft.consultant

        # Company blacklist / Do-Not-Submit checks
        job = draft.job
        company = getattr(job, "company_obj", None)
        if company and company.is_blacklisted:
            messages.error(request, "This company is blacklisted. Submissions are disabled by admin.")
            return redirect('consultant-detail', pk=consultant.user.pk)
        if company:
            today = timezone.now().date()
            if CompanyDoNotSubmit.objects.filter(
                company=company,
                consultant=consultant,
            ).filter(
                Q(until__isnull=True) | Q(until__gte=today)
            ).exists():
                messages.error(
                    request,
                    "This consultant has a Do-Not-Submit restriction for this company.",
                )
                return redirect('consultant-detail', pk=consultant.user.pk)

        submission, created = ApplicationSubmission.objects.get_or_create(
            job=draft.job,
            consultant=consultant,
            defaults={
                'resume': draft,
                'status': ApplicationSubmission.Status.IN_PROGRESS,
                'submitted_by': request.user,
            },
        )

        if not created:
            if submission.status != ApplicationSubmission.Status.IN_PROGRESS:
                messages.warning(
                    request,
                    f"Submission already exists for {draft.job.title} and is marked as {submission.get_status_display()}."
                )
            else:
                if submission.resume != draft:
                    submission.resume = draft
                    submission.save(update_fields=['resume', 'updated_at'])
                messages.info(request, f"{draft.job.title} is already claimed.")
        else:
            messages.success(request, f"Claimed {draft.job.title} for application.")

        return redirect('consultant-detail', pk=consultant.user.pk)


class EmailEventListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """
    Simple review UI for inbound email events.
    """
    model = EmailEvent
    template_name = 'submissions/email_event_list.html'
    context_object_name = 'events'
    paginate_by = 50

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get_queryset(self):
        qs = super().get_queryset()
        status = self.request.GET.get('status')
        action = self.request.GET.get('action')
        if status:
            qs = qs.filter(detected_status=status)
        if action:
            qs = qs.filter(applied_action=action)
        return qs.select_related('matched_submission', 'matched_submission__consultant__user')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = EmailEvent.objects.all()
        context['stats'] = {
            'auto_updated': qs.filter(applied_action=EmailEvent.AppliedAction.AUTO_UPDATED).count(),
            'needs_review': qs.filter(applied_action=EmailEvent.AppliedAction.NEEDS_REVIEW).count(),
        }
        return context


class EmailEventPollNowView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Admin/employee-only endpoint that runs the IMAP poller once and redirects
    back to the email event list with a summary message.
    """

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def post(self, request, *args, **kwargs):
        from core.email_ingest import fetch_unseen_and_process

        result = fetch_unseen_and_process(dry_run=False, max_messages=20)
        reason = result.get("reason")
        if reason:
            if reason == "disabled":
                messages.warning(request, "Email ingestion is disabled in Platform Configuration.")
            elif reason == "missing_credentials":
                messages.error(request, "IMAP credentials are incomplete. Please configure Email Ingestion settings first.")
            else:
                messages.error(request, f"Email poller skipped: {reason}.")
        else:
            messages.success(
                request,
                f"Email poll complete: processed {result['processed']} messages "
                f"(auto-updated={result['auto_updated']}, needs_review={result['needs_review']})."
            )
        return redirect('email-event-list')


class EmailEventDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    model = EmailEvent
    template_name = 'submissions/email_event_detail.html'
    context_object_name = 'event'

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'] = EmailEventReviewForm()
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = EmailEventReviewForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data()
            context['form'] = form
            return self.render_to_response(context)

        submission_id = form.cleaned_data['submission_id']
        new_status = form.cleaned_data['new_status']
        note = (form.cleaned_data.get('note') or '').strip()

        submission = get_object_or_404(ApplicationSubmission, pk=submission_id)
        old = submission.status
        if old != new_status:
            submission.status = new_status
            submission.save(update_fields=['status', 'updated_at'])
            record_submission_status_change(
                submission,
                new_status,
                from_status=old,
                note=(note or f"Email review applied from EmailEvent #{self.object.pk}.")[:500],
            )

        self.object.matched_submission = submission
        self.object.applied_action = EmailEvent.AppliedAction.MANUAL_UPDATED
        self.object.save(update_fields=['matched_submission', 'applied_action'])

        messages.success(request, f"Applied status {dict(ApplicationSubmission.Status.choices).get(new_status, new_status)} to submission #{submission.pk}.")
        return redirect('email-event-list')


# ─────────────────────────────────────────────────────────────
# Phase 1: Placement, Timesheet, Commission views
# ─────────────────────────────────────────────────────────────

class _StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Shortcut: admin or employee only."""
    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)


# ── Placement views ──

class PlacementListView(_StaffRequiredMixin, ListView):
    model = Placement
    template_name = 'submissions/placement_list.html'
    context_object_name = 'placements'
    paginate_by = 20

    def get_queryset(self):
        qs = super().get_queryset().select_related(
            'submission__job', 'submission__consultant__user', 'created_by'
        )
        status = self.request.GET.get('status')
        ptype = self.request.GET.get('type')
        search = self.request.GET.get('search')
        if status:
            qs = qs.filter(status=status)
        if ptype:
            qs = qs.filter(placement_type=ptype)
        if search:
            qs = qs.filter(
                Q(submission__job__title__icontains=search) |
                Q(submission__job__company__icontains=search) |
                Q(submission__consultant__user__first_name__icontains=search) |
                Q(submission__consultant__user__last_name__icontains=search)
            )
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status_choices'] = Placement.PlacementStatus.choices
        context['type_choices'] = Placement.PlacementType.choices
        # Revenue summary
        all_placements = Placement.objects.all()
        context['total_placements'] = all_placements.count()
        context['active_placements'] = all_placements.filter(status=Placement.PlacementStatus.ACTIVE).count()
        return context


class PlacementDetailView(_StaffRequiredMixin, DetailView):
    model = Placement
    template_name = 'submissions/placement_detail.html'
    context_object_name = 'placement'

    def get_queryset(self):
        return super().get_queryset().select_related(
            'submission__job__company_obj', 'submission__consultant__user', 'created_by'
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        placement = self.object
        context['timesheets'] = placement.timesheets.all()[:20]
        context['commissions'] = placement.commissions.select_related('employee').all()
        context['timesheet_form'] = TimesheetForm()
        context['commission_form'] = CommissionForm()
        # Calculate total revenue
        context['total_billed'] = sum(
            (ts.bill_amount or 0) for ts in placement.timesheets.filter(status=Timesheet.TimesheetStatus.APPROVED)
        )
        context['total_paid'] = sum(
            (ts.pay_amount or 0) for ts in placement.timesheets.filter(status=Timesheet.TimesheetStatus.APPROVED)
        )
        context['total_margin'] = context['total_billed'] - context['total_paid']
        context['total_hours'] = sum(
            ts.hours_worked for ts in placement.timesheets.filter(status=Timesheet.TimesheetStatus.APPROVED)
        )
        return context


class PlacementCreateView(_StaffRequiredMixin, CreateView):
    model = Placement
    form_class = PlacementForm
    template_name = 'submissions/placement_form.html'

    def dispatch(self, request, *args, **kwargs):
        self.submission = get_object_or_404(ApplicationSubmission, pk=kwargs['submission_pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['submission'] = self.submission
        return context

    def form_valid(self, form):
        form.instance.submission = self.submission
        form.instance.created_by = self.request.user
        # Set submission status to PLACED if not already
        if self.submission.status != ApplicationSubmission.Status.PLACED:
            old = self.submission.status
            self.submission.status = ApplicationSubmission.Status.PLACED
            self.submission.save(update_fields=['status', 'updated_at'])
            record_submission_status_change(self.submission, ApplicationSubmission.Status.PLACED, from_status=old, note='Placement created.')
        messages.success(self.request, "Placement created successfully!")
        response = super().form_valid(form)
        return response

    def get_success_url(self):
        return reverse('placement-detail', kwargs={'pk': self.object.pk})


class PlacementUpdateView(_StaffRequiredMixin, UpdateView):
    model = Placement
    form_class = PlacementForm
    template_name = 'submissions/placement_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['submission'] = self.object.submission
        return context

    def form_valid(self, form):
        messages.success(self.request, "Placement updated successfully!")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('placement-detail', kwargs={'pk': self.object.pk})


# ── Timesheet views ──

class TimesheetCreateView(_StaffRequiredMixin, View):
    """Create a timesheet for a placement (POST from placement detail)."""

    def post(self, request, placement_pk):
        placement = get_object_or_404(Placement, pk=placement_pk)
        form = TimesheetForm(request.POST)
        if form.is_valid():
            ts = form.save(commit=False)
            ts.placement = placement
            ts.submitted_by = request.user
            ts.save()
            messages.success(request, f"Timesheet for week ending {ts.week_ending} added.")
        else:
            for err in form.errors.values():
                messages.error(request, err[0])
        return redirect('placement-detail', pk=placement.pk)


class TimesheetApproveView(_StaffRequiredMixin, View):
    """Approve or reject a timesheet."""

    def post(self, request, pk):
        ts = get_object_or_404(Timesheet, pk=pk)
        action = request.POST.get('action')
        if action == 'approve':
            ts.status = Timesheet.TimesheetStatus.APPROVED
            ts.approved_by = request.user
            ts.approved_at = timezone.now()
            ts.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
            messages.success(request, "Timesheet approved.")
        elif action == 'reject':
            ts.status = Timesheet.TimesheetStatus.REJECTED
            ts.save(update_fields=['status', 'updated_at'])
            messages.warning(request, "Timesheet rejected.")
        return redirect('placement-detail', pk=ts.placement.pk)


class TimesheetListView(_StaffRequiredMixin, ListView):
    """All timesheets across all placements (for payroll overview)."""
    model = Timesheet
    template_name = 'submissions/timesheet_list.html'
    context_object_name = 'timesheets'
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().select_related(
            'placement__submission__consultant__user',
            'placement__submission__job',
            'submitted_by', 'approved_by'
        )
        status = self.request.GET.get('status')
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status_choices'] = Timesheet.TimesheetStatus.choices
        pending = Timesheet.objects.filter(status=Timesheet.TimesheetStatus.SUBMITTED).count()
        context['pending_count'] = pending
        return context


# ── Commission views ──

class CommissionCreateView(_StaffRequiredMixin, View):
    """Create a commission for a placement (POST from placement detail)."""

    def post(self, request, placement_pk):
        placement = get_object_or_404(Placement, pk=placement_pk)
        form = CommissionForm(request.POST)
        if form.is_valid():
            comm = form.save(commit=False)
            comm.placement = placement
            comm.save()
            messages.success(request, f"Commission of ${comm.commission_amount} added for {comm.employee.get_full_name()}.")
        else:
            for err in form.errors.values():
                messages.error(request, err[0])
        return redirect('placement-detail', pk=placement.pk)


class CommissionListView(_StaffRequiredMixin, ListView):
    """All commissions across all placements."""
    model = Commission
    template_name = 'submissions/commission_list.html'
    context_object_name = 'commissions'
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().select_related(
            'placement__submission__consultant__user',
            'placement__submission__job',
            'employee'
        )
        status = self.request.GET.get('status')
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['status_choices'] = Commission.CommissionStatus.choices
        from django.db.models import Sum
        totals = Commission.objects.aggregate(
            total_pending=Sum('commission_amount', filter=Q(status=Commission.CommissionStatus.PENDING)),
            total_approved=Sum('commission_amount', filter=Q(status=Commission.CommissionStatus.APPROVED)),
            total_paid=Sum('commission_amount', filter=Q(status=Commission.CommissionStatus.PAID)),
        )
        context['total_pending'] = totals['total_pending'] or 0
        context['total_approved'] = totals['total_approved'] or 0
        context['total_paid'] = totals['total_paid'] or 0
        return context


class CommissionUpdateView(_StaffRequiredMixin, UpdateView):
    """Update commission status (approve/mark paid)."""
    model = Commission
    fields = ['status', 'paid_date', 'notes']
    template_name = 'submissions/commission_form.html'

    def form_valid(self, form):
        messages.success(self.request, "Commission updated.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('commission-list')


def _kanban_queryset(request):
    qs = ApplicationSubmission.objects.select_related('job', 'consultant__user')
    user = request.user
    if user.role == User.Role.CONSULTANT:
        qs = qs.filter(consultant=user.consultant_profile)
    elif user.role not in (User.Role.EMPLOYEE, User.Role.ADMIN) and not user.is_superuser:
        return ApplicationSubmission.objects.none()
    return qs.order_by('-updated_at')


def _kanban_context(request):
    qs = _kanban_queryset(request)
    kanban_columns = []
    for code, label in ApplicationSubmission.Status.choices:
        kanban_columns.append(
            {
                'code': code,
                'label': label,
                'items': list(qs.filter(status=code)[:200]),
            }
        )
    return {'kanban_columns': kanban_columns}


def _user_can_move_submission(user, submission):
    if user.is_superuser or user.role in (User.Role.EMPLOYEE, User.Role.ADMIN):
        return True
    if user.role == User.Role.CONSULTANT and getattr(user, 'consultant_profile', None):
        return submission.consultant_id == user.consultant_profile.pk
    return False


class SubmissionKanbanView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'submissions/submission_kanban.html'

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.CONSULTANT, User.Role.EMPLOYEE, User.Role.ADMIN)

    def get_template_names(self):
        if self.request.headers.get('HX-Request'):
            return ['submissions/partials/kanban_board.html']
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_kanban_context(self.request))
        return context


class SubmissionKanbanMoveView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST: move a submission to a new status (drag-and-drop target)."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in (User.Role.CONSULTANT, User.Role.EMPLOYEE, User.Role.ADMIN)

    def post(self, request, *args, **kwargs):
        pk = request.POST.get('submission_id')
        new_status = (request.POST.get('status') or '').strip()
        if not pk:
            return JsonResponse({'ok': False, 'error': 'Missing submission.'}, status=400)
        submission = get_object_or_404(ApplicationSubmission, pk=pk)
        if not _user_can_move_submission(request.user, submission):
            return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
        valid = {c[0] for c in ApplicationSubmission.Status.choices}
        if new_status not in valid:
            return JsonResponse({'ok': False, 'error': 'Invalid status.'}, status=400)
        old = submission.status
        if old == new_status:
            if request.headers.get('HX-Request'):
                return render(request, 'submissions/partials/kanban_board.html', _kanban_context(request))
            return redirect('submission-kanban')
        submission.status = new_status
        submission.save(update_fields=['status', 'updated_at'])
        record_submission_status_change(submission, new_status, from_status=old)
        notify_submission_pipeline_event(
            submission,
            actor=request.user,
            old_status=old,
            new_status=new_status,
        )
        messages.success(request, f"Moved to {submission.get_status_display()}.")
        if request.headers.get('HX-Request'):
            return render(request, 'submissions/partials/kanban_board.html', _kanban_context(request))
        return redirect('submission-kanban')


# ─── Phase 4: Follow-Up Reminders ────────────────────────────────────
class FollowUpReminderCreateView(_StaffRequiredMixin, View):
    """Create a follow-up reminder for a submission."""

    def post(self, request, pk):
        from .forms import FollowUpReminderForm
        from .models import FollowUpReminder
        submission = get_object_or_404(ApplicationSubmission, pk=pk)
        form = FollowUpReminderForm(request.POST)
        if form.is_valid():
            reminder = form.save(commit=False)
            reminder.submission = submission
            reminder.created_by = request.user
            reminder.save()
            messages.success(request, "Follow-up reminder created.")
        else:
            messages.error(request, "Invalid reminder data.")
        return redirect('submission-detail', pk=pk)


class FollowUpReminderDismissView(_StaffRequiredMixin, View):
    """Dismiss a follow-up reminder."""

    def post(self, request, pk):
        from .models import FollowUpReminder
        reminder = get_object_or_404(FollowUpReminder, pk=pk)
        reminder.status = FollowUpReminder.ReminderStatus.DISMISSED
        reminder.save(update_fields=['status'])
        messages.success(request, "Reminder dismissed.")
        return redirect('submission-detail', pk=reminder.submission_id)


class StaleSubmissionsView(_StaffRequiredMixin, ListView):
    """List submissions that haven't been updated in >14 days and are still active."""
    template_name = 'submissions/stale_submissions.html'
    context_object_name = 'submissions'
    paginate_by = 25

    def get_queryset(self):
        cutoff = timezone.now() - timedelta(days=14)
        return ApplicationSubmission.objects.filter(
            updated_at__lt=cutoff,
            status__in=[
                ApplicationSubmission.Status.APPLIED,
                ApplicationSubmission.Status.IN_PROGRESS,
                ApplicationSubmission.Status.INTERVIEW,
            ],
            is_archived=False,
        ).select_related('job', 'consultant__user').order_by('updated_at')


# ─── Phase 5: Soft-Delete / Archive ──────────────────────────────────
class SubmissionArchiveView(_StaffRequiredMixin, View):
    """Soft-delete (archive) a submission."""

    def post(self, request, pk):
        sub = get_object_or_404(ApplicationSubmission, pk=pk)
        sub.is_archived = True
        sub.archived_at = timezone.now()
        sub.save(update_fields=['is_archived', 'archived_at'])
        messages.success(request, "Submission archived.")
        return redirect('submission-list')


class SubmissionRestoreView(_StaffRequiredMixin, View):
    """Restore an archived submission."""

    def post(self, request, pk):
        sub = get_object_or_404(ApplicationSubmission, pk=pk)
        sub.is_archived = False
        sub.archived_at = None
        sub.save(update_fields=['is_archived', 'archived_at'])
        messages.success(request, "Submission restored.")
        return redirect('submission-list')


class ArchivedSubmissionsView(_StaffRequiredMixin, ListView):
    """List all archived submissions."""
    template_name = 'submissions/archived_list.html'
    context_object_name = 'submissions'
    paginate_by = 25

    def get_queryset(self):
        return ApplicationSubmission.objects.filter(
            is_archived=True
        ).select_related('job', 'consultant__user').order_by('-archived_at')


# ─── Phase 5: GDPR Data Export ───────────────────────────────────────
class GDPRExportView(LoginRequiredMixin, View):
    """Export all data for a consultant (GDPR compliance)."""

    def get(self, request, pk):
        user = request.user
        # Only admin/employee or the consultant themselves
        if not (user.is_superuser or user.role in ('ADMIN', 'EMPLOYEE') or user.pk == pk):
            messages.error(request, "Permission denied.")
            return redirect('home')

        from users.models import ConsultantProfile
        try:
            profile = ConsultantProfile.objects.get(user__pk=pk)
        except ConsultantProfile.DoesNotExist:
            messages.error(request, "Consultant not found.")
            return redirect('home')

        import json as json_module
        data = {
            'user': {
                'username': profile.user.username,
                'email': profile.user.email,
                'first_name': profile.user.first_name,
                'last_name': profile.user.last_name,
                'date_joined': str(profile.user.date_joined),
            },
            'profile': {
                'bio': profile.bio or '',
                'phone': profile.phone or '',
                'skills': profile.skills or [],
                'status': profile.status,
            },
            'submissions': list(
                ApplicationSubmission.objects.filter(consultant=profile).values(
                    'id', 'job__title', 'job__company', 'status', 'created_at', 'notes'
                )
            ),
            'placements': list(
                Placement.objects.filter(submission__consultant=profile).values(
                    'id', 'placement_type', 'status', 'start_date', 'end_date',
                )
            ),
            'resumes': list(
                ResumeDraft.objects.filter(consultant=profile).values(
                    'id', 'job__title', 'version', 'status', 'ats_score', 'created_at'
                )
            ),
        }

        response = HttpResponse(
            json_module.dumps(data, indent=2, default=str),
            content_type='application/json'
        )
        response['Content-Disposition'] = f'attachment; filename="gdpr_export_{profile.user.username}.json"'
        return response


# ─── Phase 5: Win/Loss Analysis ──────────────────────────────────────
class WinLossAnalysisView(_StaffRequiredMixin, TemplateView):
    """Win/loss analysis dashboard."""
    template_name = 'submissions/win_loss_analysis.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.db.models import Count, Q, F

        total = ApplicationSubmission.objects.filter(is_archived=False).count()
        placed = ApplicationSubmission.objects.filter(status=ApplicationSubmission.Status.PLACED, is_archived=False).count()
        rejected = ApplicationSubmission.objects.filter(status=ApplicationSubmission.Status.REJECTED, is_archived=False).count()
        withdrawn = ApplicationSubmission.objects.filter(status=ApplicationSubmission.Status.WITHDRAWN, is_archived=False).count()
        active = total - placed - rejected - withdrawn

        context['total'] = total
        context['placed'] = placed
        context['rejected'] = rejected
        context['withdrawn'] = withdrawn
        context['active'] = active
        context['win_rate'] = round((placed / total * 100), 1) if total else 0
        context['loss_rate'] = round((rejected / total * 100), 1) if total else 0

        # By company
        context['by_company'] = (
            ApplicationSubmission.objects.filter(is_archived=False)
            .values('job__company')
            .annotate(
                total=Count('id'),
                wins=Count('id', filter=Q(status=ApplicationSubmission.Status.PLACED)),
                losses=Count('id', filter=Q(status=ApplicationSubmission.Status.REJECTED)),
            )
            .order_by('-total')[:15]
        )

        # By job source (Phase 5)
        context['by_source'] = (
            ApplicationSubmission.objects.filter(is_archived=False, job__job_source__gt='')
            .values('job__job_source')
            .annotate(
                total=Count('id'),
                wins=Count('id', filter=Q(status=ApplicationSubmission.Status.PLACED)),
                losses=Count('id', filter=Q(status=ApplicationSubmission.Status.REJECTED)),
            )
            .order_by('-total')[:10]
        )

        return context


# ──────────────────────────────────────────────────────────────────────────────
# Consultant Workflow Pipeline -- Lock-based assignment dashboard
# ──────────────────────────────────────────────────────────────────────────────

from .models import ConsultantLock, WorkflowConsultantStar
from jobs.models import Job


def _workflow_build_qs(q: str, wf_filter: str, sort: str, consultant_pk=None) -> str:
    from urllib.parse import urlencode
    p = {}
    if q:
        p['q'] = q
    if wf_filter and wf_filter != 'all':
        p['filter'] = wf_filter
    if sort and sort != 'name':
        p['sort'] = sort
    if consultant_pk is not None:
        p['consultant'] = str(consultant_pk)
    return urlencode(p)


def _get_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')


def _audit(request, action, target_model='', target_id='', details=None):
    try:
        from core.models import AuditLog
        AuditLog.objects.create(
            actor=request.user,
            action=action,
            target_model=target_model,
            target_id=str(target_id),
            details=details or {},
            ip_address=_get_ip(request),
        )
    except Exception:
        pass


class WorkflowDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """Main consultant workflow pipeline dashboard for employees."""
    template_name = 'submissions/workflow.html'

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, 'role', None) in ('ADMIN', 'EMPLOYEE')

    def dispatch(self, request, *args, **kwargs):
        if request.GET.get('clear'):
            for k in ('workflow_q', 'workflow_filter', 'workflow_sort', 'workflow_consultant_pk'):
                request.session.pop(k, None)
            return HttpResponseRedirect(reverse('workflow-dashboard'))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        req = self.request
        sess = req.session

        ConsultantLock.objects.filter(expires_at__lt=timezone.now()).delete()

        consultants = list(
            ConsultantProfile.objects
            .filter(user__is_active=True)
            .select_related('user', 'active_lock', 'active_lock__locked_by')
            .prefetch_related('marketing_roles')
            .order_by('user__first_name', 'user__last_name')
        )

        from django.conf import settings as dj_settings
        from .workflow_utils import get_pipeline_workflow_bulk

        pipeline_by_pk = get_pipeline_workflow_bulk(consultants)

        if 'q' in req.GET:
            q_raw = (req.GET.get('q') or '').strip()
        else:
            q_raw = (sess.get('workflow_q') or '').strip()

        if 'filter' in req.GET:
            wf_filter = (req.GET.get('filter') or 'all').strip()
        else:
            wf_filter = (sess.get('workflow_filter') or 'all').strip()

        valid_sorts = ('name', 'pending', 'submitted')
        if 'sort' in req.GET:
            sort = (req.GET.get('sort') or 'name').strip()
        else:
            sort = (sess.get('workflow_sort') or 'name').strip()
        if sort not in valid_sorts:
            sort = 'name'

        if 'consultant' in req.GET:
            sel_param = req.GET.get('consultant') or None
        else:
            sel_param = sess.get('workflow_consultant_pk')

        starred_ids = set(
            WorkflowConsultantStar.objects.filter(user=req.user).values_list('consultant_id', flat=True)
        )

        now = timezone.now()
        consultant_rows = []
        for c in consultants:
            lock = getattr(c, 'active_lock', None)
            if lock and lock.expires_at <= now:
                lock = None
            pipe = pipeline_by_pk.get(
                c.pk,
                {
                    'assigned': 0,
                    'drafting': 0,
                    'submitted': 0,
                    'stale_assigned': False,
                    'stale_drafting': False,
                    'stale_assigned_days': None,
                    'stale_drafting_days': None,
                },
            )
            consultant_rows.append({
                'consultant': c,
                'lock': lock,
                'is_locked_by_me': bool(lock and lock.locked_by_id == req.user.pk),
                'is_locked_by_other': bool(lock and lock.locked_by_id != req.user.pk),
                'pipeline': pipe,
                'starred': c.pk in starred_ids,
            })

        summary = {'need_work': 0, 'in_draft': 0, 'has_submitted': 0}
        for c in consultants:
            p = pipeline_by_pk.get(c.pk, {})
            a, d, s = p.get('assigned', 0), p.get('drafting', 0), p.get('submitted', 0)
            if a + d > 0:
                summary['need_work'] += 1
            if d > 0:
                summary['in_draft'] += 1
            if s > 0:
                summary['has_submitted'] += 1

        if q_raw:
            ql = q_raw.lower()
            consultant_rows = [
                row for row in consultant_rows
                if ql in (row['consultant'].user.get_full_name() or '').lower()
                or ql in (row['consultant'].user.username or '').lower()
                or ql in (row['consultant'].user.email or '').lower()
            ]

        if wf_filter == 'needs_assigned':
            consultant_rows = [r for r in consultant_rows if r['pipeline']['assigned'] > 0]
        elif wf_filter == 'needs_draft':
            consultant_rows = [r for r in consultant_rows if r['pipeline']['drafting'] > 0]
        elif wf_filter == 'has_submitted':
            consultant_rows = [r for r in consultant_rows if r['pipeline']['submitted'] > 0]
        elif wf_filter == 'needs_work':
            consultant_rows = [
                r for r in consultant_rows
                if r['pipeline']['assigned'] > 0 or r['pipeline']['drafting'] > 0
            ]

        def _row_key(r):
            star = 0 if r['starred'] else 1
            name = (r['consultant'].user.get_full_name() or '').lower()
            pend = r['pipeline']['assigned'] + r['pipeline']['drafting']
            subm = r['pipeline']['submitted']
            if sort == 'pending':
                return (star, -pend, name)
            if sort == 'submitted':
                return (star, -subm, name)
            return (star, name)

        consultant_rows.sort(key=_row_key)

        effective_sel = None
        if sel_param:
            for r in consultant_rows:
                if str(r['consultant'].pk) == str(sel_param):
                    effective_sel = str(r['consultant'].pk)
                    break

        sess['workflow_q'] = q_raw
        sess['workflow_filter'] = wf_filter
        sess['workflow_sort'] = sort
        sess['workflow_consultant_pk'] = effective_sel

        wfqs = {
            'all': _workflow_build_qs(q_raw, 'all', sort),
            'needs_work': _workflow_build_qs(q_raw, 'needs_work', sort),
            'needs_assigned': _workflow_build_qs(q_raw, 'needs_assigned', sort),
            'needs_draft': _workflow_build_qs(q_raw, 'needs_draft', sort),
            'has_submitted': _workflow_build_qs(q_raw, 'has_submitted', sort),
            'sort_name': _workflow_build_qs(q_raw, wf_filter, 'name'),
            'sort_pending': _workflow_build_qs(q_raw, wf_filter, 'pending'),
            'sort_submitted': _workflow_build_qs(q_raw, wf_filter, 'submitted'),
        }

        for r in consultant_rows:
            r['href_qs'] = _workflow_build_qs(q_raw, wf_filter, sort, r['consultant'].pk)

        ctx['consultant_rows'] = consultant_rows
        ctx['workflow_q'] = q_raw
        ctx['workflow_filter'] = wf_filter
        ctx['workflow_sort'] = sort
        ctx['workflow_qs'] = wfqs
        ctx['workflow_summary'] = summary
        ctx['workflow_total_count'] = len(consultants)
        ctx['workflow_stale_days'] = int(getattr(dj_settings, 'WORKFLOW_STALE_DAYS', 7))
        ctx['workflow_next_path'] = req.get_full_path()
        ctx['selected_pk'] = effective_sel

        return ctx


class WorkflowStarToggleView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST: toggle starred consultant for the workflow sidebar."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, 'role', None) in ('ADMIN', 'EMPLOYEE')

    def post(self, request, pk):
        consultant = get_object_or_404(ConsultantProfile.objects.select_related('user'), pk=pk)
        star, created = WorkflowConsultantStar.objects.get_or_create(
            user=request.user,
            consultant=consultant,
        )
        if not created:
            star.delete()
        next_url = request.POST.get('next') or reverse('workflow-dashboard')
        if next_url and not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = reverse('workflow-dashboard')
        return redirect(next_url)


class WorkflowPanelView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    HTMX fragment: the 3-column pipeline for a selected consultant.
    Loaded when user clicks a consultant card on the dashboard.
    """
    template_name = 'submissions/workflow_panel.html'

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, 'role', None) in ('ADMIN', 'EMPLOYEE')

    def get(self, request, pk):
        consultant = get_object_or_404(
            ConsultantProfile.objects.select_related('user', 'active_lock', 'active_lock__locked_by')
            .prefetch_related('marketing_roles'),
            pk=pk
        )
        now = timezone.now()

        # Resolve lock
        lock = getattr(consultant, 'active_lock', None)
        if lock and lock.expires_at <= now:
            lock.delete()
            lock = None

        is_locked_by_me = bool(lock and lock.locked_by_id == request.user.pk)
        is_locked_by_other = bool(lock and lock.locked_by_id != request.user.pk)
        is_manager = request.user.is_superuser or getattr(request.user, 'role', '') == 'ADMIN'
        can_act = is_locked_by_me  # Only the lock holder can take actions

        # Jobs matching consultant's marketing roles (OPEN only)
        role_ids = list(consultant.marketing_roles.values_list('id', flat=True))
        if role_ids:
            matched_jobs_qs = (
                Job.objects
                .filter(status=Job.Status.OPEN, is_archived=False, marketing_roles__in=role_ids)
                .distinct()
                .select_related('company_obj')
            )
        else:
            matched_jobs_qs = Job.objects.none()

        matched_job_ids = list(matched_jobs_qs.values_list('id', flat=True))

        # Latest draft per job for this consultant (all jobs, not just matched -- for column 3)
        all_drafts = (
            Resume.objects
            .filter(consultant=consultant)
            .select_related('job')
            .order_by('job_id', '-version')
        )
        drafts_by_job = {}
        for d in all_drafts:
            if d.job_id not in drafts_by_job:
                drafts_by_job[d.job_id] = d

        # All submissions for this consultant
        all_subs = (
            ApplicationSubmission.objects
            .filter(consultant=consultant, is_archived=False)
            .select_related('job', 'resume', 'job__company_obj')
            .order_by('-updated_at')
        )
        subs_by_job = {}
        for s in all_subs:
            if s.job_id not in subs_by_job:
                subs_by_job[s.job_id] = s

        # Categorise
        ACTIVE_STATUSES = {
            ApplicationSubmission.Status.APPLIED,
            ApplicationSubmission.Status.INTERVIEW,
            ApplicationSubmission.Status.OFFER,
            ApplicationSubmission.Status.PLACED,
            ApplicationSubmission.Status.REJECTED,
            ApplicationSubmission.Status.WITHDRAWN,
        }

        column1 = []  # Assigned, no draft, no submission
        column2 = []  # Draft exists or IN_PROGRESS submission -- not yet applied
        column3 = []  # Applied or beyond

        for job in matched_jobs_qs:
            sub = subs_by_job.get(job.pk)
            draft = drafts_by_job.get(job.pk)
            if sub and sub.status in ACTIVE_STATUSES:
                column3.append({'job': job, 'sub': sub, 'draft': draft})
            elif sub or draft:
                column2.append({'job': job, 'sub': sub, 'draft': draft})
            else:
                column1.append({'job': job, 'sub': None, 'draft': None})

        # Also add column3 for jobs NOT in matched_jobs but with active submissions
        # (e.g. job was closed/filled after they started)
        for job_id, sub in subs_by_job.items():
            if job_id not in matched_job_ids and sub.status in ACTIVE_STATUSES:
                column3.append({'job': sub.job, 'sub': sub, 'draft': drafts_by_job.get(job_id)})

        return render(request, self.template_name, {
            'consultant': consultant,
            'lock': lock,
            'is_locked_by_me': is_locked_by_me,
            'is_locked_by_other': is_locked_by_other,
            'can_act': can_act,
            'is_manager': is_manager,
            'column1': column1,
            'column2': column2,
            'column3': column3,
        })


class ConsultantClaimView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Atomically claim (lock) a consultant profile. POST only."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, 'role', None) in ('ADMIN', 'EMPLOYEE')

    def post(self, request, pk):
        from django.db import transaction
        consultant = get_object_or_404(ConsultantProfile.objects.select_related('user'), pk=pk)
        now = timezone.now()

        try:
            with transaction.atomic():
                try:
                    lock = ConsultantLock.objects.select_for_update(nowait=True).get(
                        consultant=consultant
                    )
                    if lock.expires_at > now:
                        if lock.locked_by_id == request.user.pk:
                            lock.extend()
                            messages.success(request, f"Lock extended -- {consultant.user.get_full_name()} is yours for 2 more hours.")
                        else:
                            messages.error(
                                request,
                                f"{consultant.user.get_full_name()} is locked by "
                                f"{lock.locked_by.get_full_name()}. Cannot claim."
                            )
                            return redirect(f"{reverse('workflow-dashboard')}?consultant={pk}")
                    else:
                        # Expired -- take it over
                        lock.locked_by = request.user
                        lock.locked_at = now
                        lock.expires_at = now + timedelta(hours=2)
                        lock.last_heartbeat_at = now
                        lock.save()
                        messages.success(request, f"Claimed {consultant.user.get_full_name()} (previous lock expired).")
                except ConsultantLock.DoesNotExist:
                    ConsultantLock.objects.create(
                        consultant=consultant,
                        locked_by=request.user,
                        expires_at=now + timedelta(hours=2),
                    )
                    messages.success(request, f"You've claimed {consultant.user.get_full_name()}. Lock active for 2 hours.")
        except Exception:
            messages.error(request, "Could not claim this consultant -- someone else may have just claimed them. Try again.")
            return redirect(f"{reverse('workflow-dashboard')}?consultant={pk}")

        _audit(request, 'consultant_claim', 'ConsultantProfile', pk, {
            'consultant_name': consultant.user.get_full_name()
        })
        return redirect(f"{reverse('workflow-dashboard')}?consultant={pk}")


class ConsultantReleaseView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Release own lock on a consultant profile. POST only."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, 'role', None) in ('ADMIN', 'EMPLOYEE')

    def post(self, request, pk):
        consultant = get_object_or_404(ConsultantProfile.objects.select_related('user'), pk=pk)
        lock = getattr(consultant, 'active_lock', None)
        if not lock:
            messages.warning(request, "No active lock found.")
        elif lock.locked_by_id != request.user.pk and not (
            request.user.is_superuser or getattr(request.user, 'role', '') == 'ADMIN'
        ):
            messages.error(request, "You can only release your own lock.")
        else:
            name = consultant.user.get_full_name()
            lock.delete()
            _audit(request, 'consultant_release', 'ConsultantProfile', pk, {
                'consultant_name': name
            })
            messages.success(request, f"Released {name}.")
        return redirect(f"{reverse('workflow-dashboard')}?consultant={pk}")


class LockHeartbeatView(LoginRequiredMixin, View):
    """
    HTMX endpoint -- called every ~4 min by the browser to extend the lock.
    Returns a small HTML fragment with the updated countdown.
    """

    def post(self, request, pk):
        try:
            lock = ConsultantLock.objects.get(consultant_id=pk, locked_by=request.user)
            if not lock.is_expired():
                lock.extend()
                secs = lock.time_remaining_seconds()
                hrs = secs // 3600
                mins = (secs % 3600) // 60
                return HttpResponse(
                    f'<span id="lock-timer" '
                    f'hx-post="{reverse("workflow-heartbeat", args=[pk])}" '
                    f'hx-trigger="every 240s" hx-swap="outerHTML">'
                    f'{hrs}h {mins}m remaining</span>',
                    content_type='text/html'
                )
        except ConsultantLock.DoesNotExist:
            pass
        return HttpResponse(
            '<span id="lock-timer" class="text-red-600">Lock expired</span>',
            content_type='text/html'
        )


class LockOverrideView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Manager/Admin force-take a locked consultant profile."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, 'role', '') == 'ADMIN'

    def post(self, request, pk):
        from django.db import transaction
        consultant = get_object_or_404(ConsultantProfile.objects.select_related('user'), pk=pk)
        now = timezone.now()
        with transaction.atomic():
            ConsultantLock.objects.filter(consultant=consultant).delete()
            ConsultantLock.objects.create(
                consultant=consultant,
                locked_by=request.user,
                expires_at=now + timedelta(hours=2),
            )
        name = consultant.user.get_full_name()
        _audit(request, 'consultant_lock_override', 'ConsultantProfile', pk, {
            'consultant_name': name,
            'reason': request.POST.get('reason', ''),
        })
        messages.success(request, f"Override successful -- you now hold the lock for {name}.")
        return redirect(f"{reverse('workflow-dashboard')}?consultant={pk}")


class MarkExternalApplicationView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Mark a job as applied externally by the consultant.
    Creates an ApplicationSubmission with status=APPLIED and a note.
    Only allowed if caller holds the lock on this consultant.
    """

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, 'role', None) in ('ADMIN', 'EMPLOYEE')

    def post(self, request, consultant_pk, job_pk):
        consultant = get_object_or_404(ConsultantProfile, pk=consultant_pk)
        job = get_object_or_404(Job, pk=job_pk)

        # Enforce lock
        lock = getattr(consultant, 'active_lock', None)
        is_manager = request.user.is_superuser or getattr(request.user, 'role', '') == 'ADMIN'
        if not lock or (lock.locked_by_id != request.user.pk and not is_manager):
            messages.error(request, "You must hold the lock on this consultant to mark external applications.")
            return redirect(f"{reverse('workflow-dashboard')}?consultant={consultant_pk}")

        note = request.POST.get('note', '').strip() or 'Applied externally by consultant'
        try:
            sub, created = ApplicationSubmission.objects.get_or_create(
                job=job,
                consultant=consultant,
                defaults={
                    'status': ApplicationSubmission.Status.APPLIED,
                    'submitted_by': request.user,
                    'notes': note,
                }
            )
            if not created:
                if sub.status == ApplicationSubmission.Status.IN_PROGRESS:
                    sub.status = ApplicationSubmission.Status.APPLIED
                    sub.notes = note
                    sub.submitted_by = request.user
                    sub.save(update_fields=['status', 'notes', 'submitted_by', 'updated_at'])
                    messages.success(request, f"Marked '{job.title}' as externally applied.")
                else:
                    messages.warning(request, f"'{job.title}' already has a submission (status: {sub.get_status_display()}).")
            else:
                messages.success(request, f"Marked '{job.title}' as externally applied.")
            _audit(request, 'mark_applied_external', 'ApplicationSubmission', sub.pk, {
                'job': job.title, 'consultant': consultant.user.get_full_name()
            })
        except Exception as e:
            messages.error(request, f"Could not mark as applied: {e}")

        return redirect(f"{reverse('workflow-dashboard')}?consultant={consultant_pk}")

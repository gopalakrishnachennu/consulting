import csv
import re
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import CreateView, ListView, UpdateView, View, DetailView
from django.db.models import Q, Count, Max
from django.db import IntegrityError
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
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
from users.models import User
from jobs.services import ensure_parsed_jd
from companies.models import Company, CompanyDoNotSubmit
from config.constants import (
    PAGINATION_SUBMISSIONS, MAX_UPLOAD_SIZE, MAX_UPLOAD_SIZE_MB,
    MSG_SUBMISSION_SUCCESS, MSG_SUBMISSION_MISMATCH, MSG_SUBMISSION_SELF_ONLY, MSG_FILE_TOO_LARGE,
)

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

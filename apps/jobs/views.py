from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy
from django.contrib import messages
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models import Q, Count
from django.http import HttpResponse
from django.conf import settings
import re
import json
import csv
import io
import logging

from .models import Job
from config.pagination import PAGE_SIZE_OPTIONS, get_page_size, build_pagination_window

logger = logging.getLogger(__name__)
from .forms import JobForm, JobBulkUploadForm


def _get_require_pool_staging() -> bool:
    """Whether new jobs go to the vetting pool first (PlatformConfig)."""
    try:
        from core.models import PlatformConfig

        return bool(getattr(PlatformConfig.load(), "require_pool_staging", True))
    except Exception:
        return True
from companies.models import Company
from users.models import User, MarketingRole
from core.feature_flags import feature_enabled_for
from .services import (
    JDParserService,
    match_consultants_for_job,
    ranked_consultants_for_job,
    find_potential_duplicate_jobs,
    ensure_parsed_jd,
    validate_job_quality,
)
from submissions.models import ApplicationSubmission


def apply_job_list_filters(qs, request):
    """
    Shared filters for job list, HTMX partial, CSV export, and summary counts.
    Query params: status, search, role, job_type, location, possibly_filled, link_live.
    """
    status = request.GET.get('status')
    search_query = request.GET.get('search')
    role_filter = request.GET.get('role')
    job_type = request.GET.get('job_type')
    location = request.GET.get('location')

    if status and status in dict(Job.Status.choices):
        qs = qs.filter(status=status)

    if search_query:
        qs = qs.filter(
            Q(title__icontains=search_query)
            | Q(company__icontains=search_query)
            | Q(description__icontains=search_query)
        )
    if role_filter:
        qs = qs.filter(marketing_roles__slug=role_filter)

    if job_type and job_type in dict(Job.JobType.choices):
        qs = qs.filter(job_type=job_type)

    if location:
        qs = qs.filter(location__icontains=location)

    possibly_filled = request.GET.get('possibly_filled')
    if possibly_filled == '1':
        qs = qs.filter(possibly_filled=True)
    elif possibly_filled == '0':
        qs = qs.filter(possibly_filled=False)

    link_live = request.GET.get('link_live')
    if link_live == '1':
        qs = qs.filter(original_link_is_live=True)
    elif link_live == '0':
        qs = qs.filter(original_link_is_live=False)

    return qs.distinct()


class EmployeeRequiredMixin(UserPassesTestMixin):
    """Staff-only; set employee_feature_key to gate with a FeatureFlag (e.g. employee_job_pool)."""

    employee_feature_key = None

    def test_func(self):
        u = self.request.user
        if not (u.is_superuser or u.role in (User.Role.EMPLOYEE, User.Role.ADMIN)):
            return False
        key = getattr(self, 'employee_feature_key', None)
        if key:
            return feature_enabled_for(u, key)
        return True

class JobListView(LoginRequiredMixin, ListView):
    model = Job
    template_name = 'jobs/job_list.html'
    context_object_name = 'jobs'
    ordering = ['-created_at']

    def get_paginate_by(self, queryset):
        return get_page_size(self.request, default=100)

    def get_queryset(self):
        qs = apply_job_list_filters(super().get_queryset(), self.request)
        return qs.annotate(application_count=Count('submissions'))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Status summary and totals computed from the full filtered queryset,
        # not just the current page.
        full_qs = _get_job_list_queryset(self.request)
        status_counts = full_qs.values('status').annotate(count=Count('status'))
        summary = {'OPEN': 0, 'CLOSED': 0, 'DRAFT': 0, 'POOL': 0}
        for row in status_counts:
            code = row['status']
            if code in summary:
                summary[code] = row['count']
        # Also add global pool count (across all non-archived pool jobs) for the badge
        summary['POOL'] = Job.objects.filter(status=Job.Status.POOL, is_archived=False).count()

        context['status_summary'] = summary
        context['total_jobs'] = full_qs.count()

        context['marketing_roles'] = MarketingRole.objects.all()
        context['selected_role'] = self.request.GET.get('role', '')
        context['selected_status'] = self.request.GET.get('status', '')
        context['selected_job_type'] = self.request.GET.get('job_type', '')
        context['selected_location'] = self.request.GET.get('location', '')
        context['selected_possibly_filled'] = self.request.GET.get('possibly_filled', '')
        context['selected_link_live'] = self.request.GET.get('link_live', '')
        qd = self.request.GET.copy()
        qd.pop('page', None)
        context['pagination_query'] = qd.urlencode()
        context['page_size'] = get_page_size(self.request, default=100)
        context['page_size_options'] = PAGE_SIZE_OPTIONS
        if context.get('is_paginated'):
            context['pagination_pages'] = build_pagination_window(context['page_obj'])
        return context

    def get_template_names(self):
        if self.request.headers.get('HX-Request'):
            return ['jobs/_job_list_partial.html']
        return super().get_template_names()


def _get_job_list_queryset(request):
    """Shared queryset for job list and CSV export (same filters as JobListView)."""
    qs = Job.objects.select_related('posted_by').prefetch_related('marketing_roles').order_by('-created_at')
    return apply_job_list_filters(qs, request)


class JobExportCSVView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Export job list as CSV with same filters as list view."""

    def test_func(self):
        u = self.request.user
        if not (u.is_superuser or u.role in (User.Role.EMPLOYEE, User.Role.ADMIN)):
            return False
        return feature_enabled_for(u, 'employee_csv_export')

    def get(self, request, *args, **kwargs):
        qs = _get_job_list_queryset(request)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="jobs.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Title',
            'Company',
            'Location',
            'Job Type',
            'Status',
            'Posted By',
            'Marketing Roles',
            'Link live',
            'Possibly filled',
            'Created At',
            'Updated At',
        ])
        for job in qs:
            roles = ', '.join(r.name for r in job.marketing_roles.all())
            writer.writerow([
                job.title,
                job.company,
                job.location or '',
                job.get_job_type_display(),
                job.get_status_display(),
                job.posted_by.get_full_name() or job.posted_by.username,
                roles,
                'yes' if job.original_link_is_live else 'no',
                'yes' if job.possibly_filled else 'no',
                job.created_at.strftime('%Y-%m-%d %H:%M'),
                job.updated_at.strftime('%Y-%m-%d %H:%M'),
            ])
        return response


class JobDetailView(LoginRequiredMixin, DetailView):
    model = Job
    template_name = 'jobs/job_detail.html'
    context_object_name = 'job'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        job = context.get('job')
        text = (job.description or "") if job else ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Collapse multiple blank lines to a single line break to avoid huge gaps
        text = re.sub(r"\n{2,}", "\n", text)
        context['description_collapsed'] = text
        if job:
            context['application_count'] = job.submissions.count()
        if job and job.parsed_jd:
            context['parsed_jd_json'] = json.dumps(job.parsed_jd, indent=2)
        else:
            context['parsed_jd_json'] = ""

        # Consultant ↔ job match scores (ranked, with % and raw score)
        if job:
            if getattr(self.request.user, 'role', None) == User.Role.CONSULTANT:
                if feature_enabled_for(self.request.user, 'consultant_job_matching'):
                    context['consultant_match_rankings'] = ranked_consultants_for_job(job, limit=25)
                    context['matched_consultants'] = match_consultants_for_job(job, limit=6)
                else:
                    context['consultant_match_rankings'] = []
                    context['matched_consultants'] = []
            else:
                context['consultant_match_rankings'] = ranked_consultants_for_job(job, limit=25)
                context['matched_consultants'] = match_consultants_for_job(job, limit=6)
        # Consultant: their application for this job (so they can "Schedule interview" from this job)
        if job and getattr(self.request.user, 'role', None) == User.Role.CONSULTANT and hasattr(self.request.user, 'consultant_profile'):
            context['consultant_submission_for_job'] = ApplicationSubmission.objects.filter(
                consultant=self.request.user.consultant_profile, job=job
            ).select_related('job').first()
        else:
            context['consultant_submission_for_job'] = None
        return context


class JobParseJDView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        ok, err = JDParserService.parse_job(job, actor=request.user)
        if ok:
            messages.success(request, "JD parsed and saved.")
        else:
            messages.error(request, f"JD parse failed: {err}")
        return redirect('job-detail', pk=pk)

class JobCreateView(LoginRequiredMixin, EmployeeRequiredMixin, CreateView):
    model = Job
    form_class = JobForm
    template_name = 'jobs/job_form.html'
    success_url = reverse_lazy('job-list')

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Status is set in form_valid from platform config — hide to avoid confusion.
        form.fields.pop("status", None)
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["require_pool_staging"] = _get_require_pool_staging()
        return context

    def get_initial(self):
        initial = super().get_initial()
        company_id = self.request.GET.get("company_id")
        if company_id:
            try:
                company = Company.objects.get(pk=company_id)
                initial["company_obj"] = company
                initial["company"] = company.name
            except Company.DoesNotExist:
                pass
        return initial

    def form_valid(self, form):
        form.instance.posted_by = self.request.user
        use_pool = _get_require_pool_staging()
        form.instance.status = Job.Status.POOL if use_pool else Job.Status.OPEN
        company_obj = form.cleaned_data.get("company_obj")
        if company_obj:
            form.instance.company_obj = company_obj
            form.instance.company = company_obj.name
        # Duplicate detection (informational warning — does not block)
        dups = find_potential_duplicate_jobs(
            title=form.cleaned_data.get("title", ""),
            company=form.cleaned_data.get("company", "") or (company_obj.name if company_obj else ""),
            description=form.cleaned_data.get("description", ""),
        )
        if dups:
            top = dups[0]
            messages.warning(
                self.request,
                f"Possible duplicate detected: \"{top['job'].title}\" at {top['job'].company} (score {top['overall_score']:.0%}). Review before approving.",
            )
        resp = super().form_valid(form)
        # Rules-first JD parse (no AI tokens)
        ensure_parsed_jd(self.object, actor=self.request.user)
        # Kick off async validation scoring
        try:
            from .tasks import run_job_validation
            run_job_validation.delay(self.object.pk)
        except Exception:
            logger.exception("run_job_validation task dispatch failed")
        if use_pool:
            messages.success(
                self.request,
                f"Job \"{self.object.title}\" added to the Job Pool for review. "
                "It will be visible to consultants once approved."
            )
        else:
            messages.success(self.request, f"Job \"{self.object.title}\" posted and is now live.")
            try:
                from .notify import notify_new_open_job_to_consultants
                notify_new_open_job_to_consultants(self.object)
            except Exception:
                logger.exception("notify_new_open_job_to_consultants failed")
        return resp

    def get_success_url(self):
        if self.object and self.object.status == Job.Status.POOL:
            return reverse_lazy('job-pool')
        return reverse_lazy('job-list')

class JobUpdateView(LoginRequiredMixin, EmployeeRequiredMixin, UpdateView):
    model = Job
    form_class = JobForm
    template_name = 'jobs/job_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["require_pool_staging"] = _get_require_pool_staging()
        return context

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_superuser or self.request.user.role == User.Role.ADMIN:
            return qs
        return qs.filter(posted_by=self.request.user)

    def form_valid(self, form):
        old = self.get_object()
        old_status = old.status
        obj = form.save(commit=False)
        company_obj = form.cleaned_data.get("company_obj")
        if company_obj:
            obj.company_obj = company_obj
            obj.company = company_obj.name
        obj.last_edited_by = self.request.user
        obj.last_edited_at = timezone.now()
        obj.save()
        form.save_m2m()
        # Duplicate detection (warning only)
        dups = find_potential_duplicate_jobs(
            title=obj.title,
            company=obj.company,
            description=obj.description,
            exclude_job_id=obj.pk,
        )
        if dups:
            top = dups[0]
            messages.warning(
                self.request,
                f"Possible duplicate job detected (top match: '{top['job'].title}' at {top['job'].company}, score {top['overall_score']}).",
            )
        # Refresh parse after edits (rules-first)
        ensure_parsed_jd(obj, actor=self.request.user)
        messages.success(self.request, "Job updated successfully!")
        try:
            from .notify import notify_job_closed_to_applicants, notify_new_open_job_to_consultants

            if old_status != Job.Status.CLOSED and obj.status == Job.Status.CLOSED:
                notify_job_closed_to_applicants(obj)
            elif old_status != Job.Status.OPEN and obj.status == Job.Status.OPEN:
                notify_new_open_job_to_consultants(obj)
        except Exception:
            logger.exception("job status notification failed")
        return redirect('job-detail', pk=obj.pk)

class JobDeleteView(LoginRequiredMixin, EmployeeRequiredMixin, DeleteView):
    model = Job
    template_name = 'jobs/job_confirm_delete.html'
    success_url = reverse_lazy('job-list')

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_superuser or self.request.user.role == User.Role.ADMIN:
            return qs
        return qs.filter(posted_by=self.request.user)
    
    def delete(self, request, *args, **kwargs):
        messages.success(self.request, "Job deleted successfully!")
        return super().delete(request, *args, **kwargs)

from django.db import transaction


def _bulk_upload_max_bytes() -> int:
    mb = getattr(settings, "JOB_BULK_UPLOAD_MAX_MB", 50)
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        mb = 50
    return max(1, mb) * 1024 * 1024


def _bulk_cell(row: dict, *keys: str) -> str:
    """First non-empty stripped value among candidate CSV column names."""
    for key in keys:
        if key not in row:
            continue
        raw = row.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if s:
            return s
    return ""


def _bulk_csv_headers_ok(fieldnames) -> bool:
    if not fieldnames:
        return False
    fn = {str(h).strip() for h in fieldnames if h is not None and str(h).strip()}
    if not fn:
        return False
    title_ok = bool(fn & {"title", "job.title"})
    company_ok = bool(fn & {"company", "job.company_name", "company.name"})
    loc_ok = bool(fn & {"location", "job.location"})
    desc_ok = bool(fn & {"description", "job.description"})
    return title_ok and company_ok and loc_ok and desc_ok


def _bulk_existing_job_for_posting_url(url: str):
    url_norm = (url or "").strip().rstrip("/")
    if not url_norm:
        return None
    return (
        Job.objects.filter(
            Q(original_link__iexact=url_norm) | Q(original_link__iexact=url_norm + "/")
        )
        .first()
    )


class JobBulkUploadView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_bulk_ops'
    def get(self, request):
        form = JobBulkUploadForm()
        return render(request, 'jobs/job_bulk_upload.html', {'form': form})

    def post(self, request):
        form = JobBulkUploadForm(request.POST, request.FILES)
        if form.is_valid():
            csv_file = request.FILES['csv_file']
            
            # 1. File validation
            if not csv_file.name.endswith('.csv'):
                messages.error(request, "Please upload a CSV file.")
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            max_bytes = _bulk_upload_max_bytes()
            if csv_file.size > max_bytes:
                messages.error(
                    request,
                    f"Uploaded file is too large ({csv_file.size / (1024 * 1024):.1f} MB). "
                    f"Maximum is {max_bytes // (1024 * 1024)} MB (JOB_BULK_UPLOAD_MAX_MB).",
                )
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            try:
                decoded_file = csv_file.read().decode('utf-8')
            except UnicodeDecodeError:
                messages.error(request, "File encoding error. Please ensure the file is UTF-8 encoded.")
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            io_string = io.StringIO(decoded_file)
            reader = csv.DictReader(io_string)

            # Determine target status from PlatformConfig
            try:
                from core.models import PlatformConfig
                _cfg = PlatformConfig.load()
                bulk_target_status = Job.Status.POOL if getattr(_cfg, 'require_pool_staging', True) else Job.Status.OPEN
            except Exception:
                bulk_target_status = Job.Status.POOL

            # 2. Header validation (supports scrape-style column names)
            if not _bulk_csv_headers_ok(reader.fieldnames):
                messages.error(
                    request,
                    "Missing required columns. Need title or job.title; company, job.company_name, or "
                    "company.name; location or job.location; description or job.description. "
                    f"Found headers: {list(reader.fieldnames)[:40]}{'…' if len(reader.fieldnames or []) > 40 else ''}",
                )
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            jobs_created = 0
            errors = []

            try:
                for i, row in enumerate(reader, start=1):
                    title = _bulk_cell(row, "title", "job.title")
                    company = _bulk_cell(row, "company", "job.company_name", "company.name")
                    if not title or not company:
                        errors.append(f"Row {i}: Missing title or company.")
                        continue

                    description = _bulk_cell(row, "description", "job.description")
                    location = _bulk_cell(row, "location", "job.location")
                    salary_range = _bulk_cell(row, "salary_range", "job.salary")
                    original_link = _bulk_cell(row, "original_link", "job.url")

                    if original_link:
                        url_hit = _bulk_existing_job_for_posting_url(original_link)
                        if url_hit:
                            errors.append(
                                f"Row {i}: Duplicate posting URL (existing job #{url_hit.pk} "
                                f"\"{url_hit.title}\"). Skipped."
                            )
                            continue

                    dups = find_potential_duplicate_jobs(
                        title=title,
                        company=company,
                        description=description,
                    )
                    if dups:
                        errors.append(
                            f"Row {i}: Possible duplicate of job #{dups[0]['job'].id} "
                            f"({dups[0]['job'].title} at {dups[0]['job'].company}). Skipped."
                        )
                        continue

                    try:
                        with transaction.atomic():
                            job = Job.objects.create(
                                title=title,
                                company=company,
                                location=location,
                                description=description,
                                salary_range=salary_range,
                                original_link=original_link,
                                posted_by=request.user,
                                status=bulk_target_status,
                            )
                            ensure_parsed_jd(job, actor=request.user)
                            try:
                                from .tasks import run_job_validation

                                run_job_validation.delay(job.pk)
                            except Exception:
                                logger.exception(
                                    "run_job_validation task dispatch failed for bulk job %s",
                                    job.pk,
                                )
                            jobs_created += 1
                    except Exception as row_exc:
                        logger.exception("Bulk upload row %s failed", i)
                        errors.append(f"Row {i}: {row_exc}")

            except Exception as e:
                logger.error(f"Bulk upload error: {e}")
                messages.error(request, "An unexpected error occurred during processing.")
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            if jobs_created > 0:
                messages.success(request, f"Successfully uploaded {jobs_created} jobs!")
            
            if errors:
                head = "; ".join(errors[:5])
                tail = f" …and {len(errors) - 5} more" if len(errors) > 5 else ""
                messages.warning(request, f"Some rows were skipped: {head}{tail}")

            return redirect('job-list')
        
        return render(request, 'jobs/job_bulk_upload.html', {'form': form})


class JobDuplicateCheckView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """HTMX: possible duplicate jobs while editing the create/update form."""

    def get(self, request, *args, **kwargs):
        title = request.GET.get("title", "").strip()
        company = request.GET.get("company", "").strip()
        description = request.GET.get("description", "")
        salary = request.GET.get("salary_range", "").strip()
        exclude = request.GET.get("exclude")
        exclude_id = int(exclude) if exclude and str(exclude).isdigit() else None
        has_title = bool(title)
        has_company = bool(company)
        if not has_title or not has_company:
            return render(
                request,
                "jobs/job_duplicate_fragment.html",
                {"dups": [], "has_title": has_title, "has_company": has_company},
            )
        dups = find_potential_duplicate_jobs(
            title=title,
            company=company,
            description=description,
            exclude_job_id=exclude_id,
            limit=5,
        )
        # Annotate each dup with percentage scores and salary comparison
        salary_norm = salary.strip().lower()
        for row in dups:
            job_salary = (row["job"].salary_range or "").strip().lower()
            row["salary_match"] = bool(salary_norm and job_salary and salary_norm == job_salary)
            # Convert 0.0–1.0 floats → 0–100 ints for template display
            row["overall_pct"] = round(row["overall_score"] * 100)
            row["title_pct"] = round(row["title_score"] * 100)
            row["desc_pct"] = round(row["desc_score"] * 100)
        return render(
            request,
            "jobs/job_duplicate_fragment.html",
            {
                "dups": dups,
                "has_title": has_title,
                "has_company": has_company,
                "input_salary": salary,
            },
        )


class JobUrlCheckView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """
    HTMX: real-time URL uniqueness check while entering the Job Posting URL.
    Returns a fragment showing whether the URL is already in the system.
    Called on blur/keyup of the URL field in job_form.html.
    """

    def get(self, request, *args, **kwargs):
        url = request.GET.get("url", "").strip()
        exclude = request.GET.get("exclude", "")
        exclude_id = int(exclude) if exclude and str(exclude).isdigit() else None

        if not url:
            return render(request, "jobs/job_url_check_fragment.html", {"state": "empty"})

        # Normalize: strip trailing slash for comparison
        url_norm = url.rstrip("/")

        qs = Job.objects.filter(
            Q(original_link__iexact=url_norm)
            | Q(original_link__iexact=url_norm + "/")
        )
        if exclude_id:
            qs = qs.exclude(pk=exclude_id)

        existing = qs.select_related("posted_by").first()
        if existing:
            return render(request, "jobs/job_url_check_fragment.html", {
                "state": "duplicate",
                "existing": existing,
            })

        return render(request, "jobs/job_url_check_fragment.html", {"state": "ok"})


# ─── Phase 5: Job Archive / Restore ──────────────────────────────────
class JobArchiveView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """Soft-delete (archive) a job."""

    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        job.is_archived = True
        job.archived_at = timezone.now()
        job.archived_by = request.user
        job.save(update_fields=['is_archived', 'archived_at', 'archived_by'])
        messages.success(request, f"Job '{job.title}' archived.")
        return redirect('job-list')


class JobRestoreView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """Restore an archived job."""

    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        job.is_archived = False
        job.archived_at = None
        job.archived_by = None
        job.save(update_fields=['is_archived', 'archived_at', 'archived_by'])
        messages.success(request, f"Job '{job.title}' restored.")
        return redirect('job-list')


class ArchivedJobsView(LoginRequiredMixin, EmployeeRequiredMixin, ListView):
    """List archived jobs."""
    template_name = 'jobs/archived_list.html'
    context_object_name = 'jobs'
    paginate_by = 25

    def get_queryset(self):
        return Job.objects.filter(is_archived=True).order_by('-archived_at')


# ─── Job Pool / Validation Pipeline ──────────────────────────────────────────

class JobPoolView(LoginRequiredMixin, EmployeeRequiredMixin, ListView):
    employee_feature_key = 'employee_job_pool'
    """Dashboard for jobs awaiting vetting before going live."""
    template_name = 'jobs/job_pool.html'
    context_object_name = 'jobs'
    
    def get_paginate_by(self, queryset):
        return get_page_size(self.request, default=100)

    def _apply_pool_filters(self, qs):
        """Apply shared non-tab filters for pool listing and counts."""
        req = self.request.GET
        q = (req.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(company__icontains=q)
                | Q(location__icontains=q)
                | Q(description__icontains=q)
            )
        posted_by = req.get('posted_by')
        if posted_by and posted_by.isdigit():
            qs = qs.filter(posted_by_id=int(posted_by))
        company = (req.get('company') or '').strip()
        if company:
            qs = qs.filter(company__icontains=company)
        job_type = req.get('job_type')
        if job_type and job_type in dict(Job.JobType.choices):
            qs = qs.filter(job_type=job_type)
        job_source = (req.get('job_source') or '').strip()
        if job_source:
            qs = qs.filter(job_source__icontains=job_source)
        df = parse_date(req.get('date_from') or '')
        if df:
            qs = qs.filter(created_at__date__gte=df)
        dt = parse_date(req.get('date_to') or '')
        if dt:
            qs = qs.filter(created_at__date__lte=dt)
        return qs

    def get_queryset(self):
        qs = Job.objects.filter(status=Job.Status.POOL, is_archived=False)
        qs = self._apply_pool_filters(qs)
        tab = self.request.GET.get('tab', 'all')
        if tab == 'high':
            qs = qs.filter(validation_score__gte=80)
        elif tab == 'review':
            qs = qs.filter(validation_score__gte=50, validation_score__lt=80)
        elif tab == 'flagged':
            qs = qs.filter(validation_score__lt=50)
        elif tab == 'unscored':
            qs = qs.filter(validation_score__isnull=True)

        return qs.select_related('posted_by', 'company_obj').prefetch_related('marketing_roles').order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pool_qs = self._apply_pool_filters(Job.objects.filter(status=Job.Status.POOL, is_archived=False))
        context['tab'] = self.request.GET.get('tab', 'all')
        context['count_all'] = pool_qs.count()
        context['count_high'] = pool_qs.filter(validation_score__gte=80).count()
        context['count_review'] = pool_qs.filter(validation_score__gte=50, validation_score__lt=80).count()
        context['count_flagged'] = pool_qs.filter(validation_score__lt=50).count()
        context['count_unscored'] = pool_qs.filter(validation_score__isnull=True).count()
        qd = self.request.GET.copy()
        qd.pop('page', None)
        context['pagination_query'] = qd.urlencode()
        qd_filters = self.request.GET.copy()
        qd_filters.pop('page', None)
        qd_filters.pop('tab', None)
        context['pool_filter_query'] = qd_filters.urlencode()

        req = self.request.GET
        context['filter_q'] = (req.get('q') or '').strip()
        context['filter_posted_by'] = req.get('posted_by') or ''
        context['filter_company'] = (req.get('company') or '').strip()
        context['filter_job_type'] = req.get('job_type') or ''
        context['filter_job_source'] = (req.get('job_source') or '').strip()
        context['filter_date_from'] = req.get('date_from') or ''
        context['filter_date_to'] = req.get('date_to') or ''

        poster_ids = (
            Job.objects.filter(status=Job.Status.POOL, is_archived=False)
            .values_list('posted_by_id', flat=True)
            .distinct()
        )
        context['pool_posters'] = User.objects.filter(pk__in=poster_ids).order_by(
            'first_name', 'last_name', 'username'
        )
        context['job_type_choices'] = Job.JobType.choices
        context['page_size'] = get_page_size(self.request, default=100)
        context['page_size_options'] = PAGE_SIZE_OPTIONS
        if context.get('is_paginated'):
            context['pagination_pages'] = build_pagination_window(context['page_obj'])
        return context


class JobPoolRevalidateView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Re-run validation scoring for a job (pool or live)."""
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk, is_archived=False)
        try:
            from .tasks import run_job_validation
            run_job_validation.delay(job.pk)
            messages.info(request, f"Validation re-queued for \"{job.title}\".")
        except Exception:
            # Fallback: run inline
            from .services import validate_job_quality
            result = validate_job_quality(job)
            job.validation_score = result['score']
            job.validation_result = result
            job.validation_run_at = timezone.now()
            job.save(update_fields=['validation_score', 'validation_result', 'validation_run_at'])
            messages.success(request, f"Validation complete — score {result['score']}/100.")

        if request.POST.get('redirect_to') == 'job-detail':
            return redirect('job-detail', pk=job.pk)
        return redirect('job-pool')


class JobApproveView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Move a POOL job to OPEN (approve it)."""
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        if job.status != Job.Status.POOL:
            messages.warning(request, f"\"{job.title}\" is not in the pool (status: {job.get_status_display()}).")
            return redirect('job-pool')
        job.status = Job.Status.OPEN
        job.validated_by = request.user
        job.validation_run_at = timezone.now()
        job.save(update_fields=['status', 'validated_by', 'validation_run_at'])
        try:
            from .notify import notify_new_open_job_to_consultants, notify_job_pool_status
            notify_new_open_job_to_consultants(job)
            notify_job_pool_status(job, approved=True, actor=request.user)
        except Exception:
            logger.exception("Approval notifications failed for job %s", pk)
        # Kick off semantic matching in background
        try:
            from .tasks import generate_job_matches_task
            generate_job_matches_task.delay(job.pk, notify=True)
        except Exception:
            logger.exception("Match task dispatch failed for job %s", pk)
        messages.success(request, f"✓ \"{job.title}\" approved and is now Live.")
        return redirect(request.POST.get('next') or 'job-pool')


class JobRejectView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Reject a POOL job — moves to CLOSED and records reason."""
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        if job.status != Job.Status.POOL:
            messages.warning(request, f"\"{job.title}\" is not in the pool.")
            return redirect('job-pool')
        reason = request.POST.get('rejection_reason', '').strip()
        if not reason:
            messages.error(request, "Please provide a rejection reason.")
            return redirect('job-pool')
        job.status = Job.Status.CLOSED
        job.rejection_reason = reason
        job.rejected_by = request.user
        job.rejected_at = timezone.now()
        job.save(update_fields=['status', 'rejection_reason', 'rejected_by', 'rejected_at'])
        try:
            from .notify import notify_job_pool_status
            notify_job_pool_status(job, approved=False, actor=request.user)
        except Exception:
            logger.exception("Rejection notification failed for job %s", pk)
        messages.success(request, f"✗ \"{job.title}\" rejected and closed.")
        return redirect('job-pool')


class JobBulkApproveView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_bulk_ops'
    """Bulk-approve multiple POOL jobs at once."""
    def post(self, request):
        job_ids = request.POST.getlist('job_ids')
        if not job_ids:
            messages.warning(request, "No jobs selected.")
            return redirect('job-pool')
        approved = 0
        skipped = 0
        now = timezone.now()
        for jid in job_ids:
            try:
                job = Job.objects.get(pk=jid, status=Job.Status.POOL)
                # Skip blacklisted companies
                if job.company_obj and getattr(job.company_obj, 'is_blacklisted', False):
                    skipped += 1
                    continue
                job.status = Job.Status.OPEN
                job.validated_by = request.user
                job.validation_run_at = now
                job.save(update_fields=['status', 'validated_by', 'validation_run_at'])
                try:
                    from .notify import notify_new_open_job_to_consultants
                    notify_new_open_job_to_consultants(job)
                except Exception:
                    pass
                approved += 1
            except Job.DoesNotExist:
                skipped += 1

        parts = []
        if approved:
            parts.append(f"{approved} job{'s' if approved != 1 else ''} approved and live")
        if skipped:
            parts.append(f"{skipped} skipped (blacklisted or not in pool)")
        messages.success(request, ". ".join(parts) + ".")
        return redirect('job-pool')


class JobPoolRefreshLinksView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Manual trigger from UI: refresh posting URL health in batch."""

    def post(self, request):
        try:
            # Force recheck by clearing the "last checked" timestamp for all pool/open jobs.
            Job.objects.filter(
                status__in=[Job.Status.POOL, Job.Status.OPEN],
                is_archived=False,
                original_link__isnull=False,
            ).exclude(original_link="").update(original_link_last_checked_at=None)

            from .tasks import validate_job_urls_task

            # Run async when Celery is up; fallback inline when queue unavailable.
            try:
                r = validate_job_urls_task.delay(5000)
                from urllib.parse import urlencode

                from django.urls import reverse

                q = urlencode({"tp": r.id, "tpl": "Job posting URL check"})
                messages.success(
                    request,
                    "Started link status refresh in background. Watch the progress bar at the bottom.",
                )
                return redirect(f"{reverse('job-pool')}?{q}")
            except Exception:
                result = validate_job_urls_task.apply(kwargs={"batch_size": 5000}).get()
                messages.success(
                    request,
                    f"Link status refresh completed now. Checked {result.get('processed', 0)} jobs.",
                )
        except Exception:
            logger.exception("Manual pool link refresh failed")
            messages.error(request, "Could not refresh link statuses right now. Please try again.")
        return redirect('job-pool')


# ─── Jobs Command Center — unified pipeline hub ──────────────────────────────

class JobsPipelineView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """
    Unified pipeline hub: Harvesting (raw) | Vetting (pool) | Live | Archived
    Replaces separate /harvest/raw-jobs/, /jobs/pool/, /jobs/ views with one
    tabbed command center.
    """
    template_name = 'jobs/pipeline.html'

    def get(self, request):
        tab = request.GET.get('tab', 'pool')
        q = (request.GET.get('q') or '').strip()

        # ── Summary stats (always computed) ─────────────────────────────────
        from harvest.models import RawJob, FetchBatch
        raw_total = RawJob.objects.count()
        pool_total = Job.objects.filter(status=Job.Status.POOL, is_archived=False).count()
        live_total = Job.objects.filter(status=Job.Status.OPEN, is_archived=False).count()
        archived_total = Job.objects.filter(is_archived=True).count()

        # Last harvest batch info
        last_batch = FetchBatch.objects.order_by('-created_at').first()

        # ── Tab-specific data ────────────────────────────────────────────────
        tab_jobs = None
        tab_raw = None

        if tab == 'raw':
            qs = RawJob.objects.select_related('company', 'platform').order_by('-created_at')
            if q:
                qs = qs.filter(Q(title__icontains=q) | Q(company__name__icontains=q))
            tab_raw = qs[:200]

        elif tab == 'pool':
            score_tab = request.GET.get('score', 'all')
            qs = Job.objects.filter(status=Job.Status.POOL, is_archived=False)
            if q:
                qs = qs.filter(Q(title__icontains=q) | Q(company__icontains=q))
            pool_high = qs.filter(validation_score__gte=80).count()
            pool_review = qs.filter(validation_score__gte=50, validation_score__lt=80).count()
            pool_flagged = qs.filter(validation_score__lt=50).count()
            pool_unscored = qs.filter(validation_score__isnull=True).count()
            if score_tab == 'high':
                qs = qs.filter(validation_score__gte=80)
            elif score_tab == 'review':
                qs = qs.filter(validation_score__gte=50, validation_score__lt=80)
            elif score_tab == 'flagged':
                qs = qs.filter(validation_score__lt=50)
            elif score_tab == 'unscored':
                qs = qs.filter(validation_score__isnull=True)
            tab_jobs = qs.select_related('posted_by', 'company_obj').order_by('-created_at')[:200]
            pool_extra = {
                'score_tab': score_tab,
                'pool_high': pool_high,
                'pool_review': pool_review,
                'pool_flagged': pool_flagged,
                'pool_unscored': pool_unscored,
            }

        elif tab == 'live':
            qs = Job.objects.filter(status=Job.Status.OPEN, is_archived=False)
            if q:
                qs = qs.filter(Q(title__icontains=q) | Q(company__icontains=q))
            tab_jobs = qs.select_related('posted_by', 'company_obj').annotate(
                sub_count=Count('submissions')
            ).order_by('-created_at')[:200]

        elif tab == 'archived':
            qs = Job.objects.filter(is_archived=True)
            if q:
                qs = qs.filter(Q(title__icontains=q) | Q(company__icontains=q))
            tab_jobs = qs.select_related('posted_by').order_by('-archived_at')[:200]

        ctx = {
            'tab': tab,
            'q': q,
            'raw_total': raw_total,
            'pool_total': pool_total,
            'live_total': live_total,
            'archived_total': archived_total,
            'last_batch': last_batch,
            'tab_jobs': tab_jobs,
            'tab_raw': tab_raw,
        }
        if tab == 'pool':
            ctx.update(pool_extra)

        return render(request, self.template_name, ctx)

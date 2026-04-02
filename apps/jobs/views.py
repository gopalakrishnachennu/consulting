from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q, Count
from django.http import HttpResponse
import re
import json
import csv
import io
from .models import Job
from .forms import JobForm, JobBulkUploadForm
from companies.models import Company
from users.models import User, MarketingRole
from .services import JDParserService, match_consultants_for_job, find_potential_duplicate_jobs, ensure_parsed_jd
from submissions.models import ApplicationSubmission

class EmployeeRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role in (User.Role.EMPLOYEE, User.Role.ADMIN)

class JobListView(LoginRequiredMixin, ListView):
    model = Job
    template_name = 'jobs/job_list.html'
    context_object_name = 'jobs'
    paginate_by = 10
    ordering = ['-created_at']

    def get_queryset(self):
        qs = super().get_queryset()
        status = self.request.GET.get('status')
        search_query = self.request.GET.get('search')
        role_filter = self.request.GET.get('role')
        job_type = self.request.GET.get('job_type')
        location = self.request.GET.get('location')
        
        if status and status in dict(Job.Status.choices):
            qs = qs.filter(status=status)
        
        if search_query:
            qs = qs.filter(
                Q(title__icontains=search_query) | 
                Q(company__icontains=search_query) |
                Q(description__icontains=search_query)
            )
        if role_filter:
            qs = qs.filter(marketing_roles__slug=role_filter)

        if job_type and job_type in dict(Job.JobType.choices):
            qs = qs.filter(job_type=job_type)

        if location:
            qs = qs.filter(location__icontains=location)
        possibly_filled = self.request.GET.get('possibly_filled')
        if possibly_filled == '1':
            qs = qs.filter(possibly_filled=True)
        elif possibly_filled == '0':
            qs = qs.filter(possibly_filled=False)
        return qs.annotate(application_count=Count('submissions'))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Status summary and totals computed from the full filtered queryset,
        # not just the current page.
        full_qs = _get_job_list_queryset(self.request)
        status_counts = full_qs.values('status').annotate(count=Count('status'))
        summary = {'OPEN': 0, 'CLOSED': 0, 'DRAFT': 0}
        for row in status_counts:
            code = row['status']
            if code in summary:
                summary[code] = row['count']

        context['status_summary'] = summary
        context['total_jobs'] = full_qs.count()

        context['marketing_roles'] = MarketingRole.objects.all()
        context['selected_role'] = self.request.GET.get('role', '')
        context['selected_status'] = self.request.GET.get('status', '')
        context['selected_job_type'] = self.request.GET.get('job_type', '')
        context['selected_location'] = self.request.GET.get('location', '')
        context['selected_possibly_filled'] = self.request.GET.get('possibly_filled', '')
        qd = self.request.GET.copy()
        qd.pop('page', None)
        context['pagination_query'] = qd.urlencode()
        return context

    def get_template_names(self):
        if self.request.headers.get('HX-Request'):
            return ['jobs/_job_list_partial.html']
        return super().get_template_names()


def _get_job_list_queryset(request):
    """Shared queryset for job list and CSV export (status, search, role, job_type, location)."""
    qs = Job.objects.select_related('posted_by').prefetch_related('marketing_roles').order_by('-created_at')
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
    return qs.distinct()


class JobExportCSVView(LoginRequiredMixin, View):
    """Export job list as CSV with same filters as list view."""

    def get(self, request, *args, **kwargs):
        qs = _get_job_list_queryset(request)
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="jobs.csv"'
        writer = csv.writer(response)
        writer.writerow([
            'Title', 'Company', 'Location', 'Job Type', 'Status', 'Posted By', 'Marketing Roles', 'Created At', 'Updated At'
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

        # AI-style matching: top consultants for this job
        if job:
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
        company_obj = form.cleaned_data.get("company_obj")
        if company_obj:
            form.instance.company_obj = company_obj
            # Keep legacy text field in sync
            form.instance.company = company_obj.name
        # Duplicate detection (rules-based warning only; does not block)
        dups = find_potential_duplicate_jobs(
            title=form.cleaned_data.get("title", ""),
            company=form.cleaned_data.get("company", "") or (company_obj.name if company_obj else ""),
            description=form.cleaned_data.get("description", ""),
        )
        if dups:
            top = dups[0]
            messages.warning(
                self.request,
                f"Possible duplicate job detected (top match: “{top['job'].title}” at {top['job'].company}, score {top['overall_score']}). Review before generating resumes.",
            )
        messages.success(self.request, "Job posted successfully!")
        resp = super().form_valid(form)
        # Rules-first parse (no AI tokens) to enable auto-match
        ensure_parsed_jd(self.object, actor=self.request.user)
        return resp

class JobUpdateView(LoginRequiredMixin, EmployeeRequiredMixin, UpdateView):
    model = Job
    form_class = JobForm
    template_name = 'jobs/job_form.html'
    
    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.user.is_superuser or self.request.user.role == User.Role.ADMIN:
            return qs
        return qs.filter(posted_by=self.request.user)

    def form_valid(self, form):
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
                f"Possible duplicate job detected (top match: “{top['job'].title}” at {top['job'].company}, score {top['overall_score']}).",
            )
        # Refresh parse after edits (rules-first)
        ensure_parsed_jd(obj, actor=self.request.user)
        messages.success(self.request, "Job updated successfully!")
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
import logging

logger = logging.getLogger(__name__)


class JobBulkUploadView(LoginRequiredMixin, EmployeeRequiredMixin, View):
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
            
            if csv_file.multiple_chunks():
                 messages.error(request, "Uploaded file is too large (%.2f MB)." % (csv_file.size / (1000 * 1000),))
                 return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            try:
                decoded_file = csv_file.read().decode('utf-8')
            except UnicodeDecodeError:
                messages.error(request, "File encoding error. Please ensure the file is UTF-8 encoded.")
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            io_string = io.StringIO(decoded_file)
            reader = csv.DictReader(io_string)
            
            # 2. Header validation
            required_headers = {'title', 'company', 'location', 'description'}
            if not reader.fieldnames or not required_headers.issubset(set(reader.fieldnames)):
                messages.error(request, f"Missing required columns. Found: {reader.fieldnames}. Required: {required_headers}")
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            jobs_created = 0
            errors = []
            
            try:
                with transaction.atomic():
                    for i, row in enumerate(reader, start=1):
                        title = row.get('title', '').strip()
                        company = row.get('company', '').strip()
                        
                        if not title or not company:
                            errors.append(f"Row {i}: Missing title or company.")
                            continue
                            
                        description = row.get('description', '').strip()
                        dups = find_potential_duplicate_jobs(
                            title=title,
                            company=company,
                            description=description,
                        )
                        if dups:
                            errors.append(
                                f"Row {i}: Possible duplicate of job #{dups[0]['job'].id} ({dups[0]['job'].title} at {dups[0]['job'].company}). Skipped."
                            )
                            continue

                        job = Job.objects.create(
                            title=title,
                            company=company,
                            location=row.get('location', '').strip(),
                            description=description,
                            salary_range=row.get('salary_range', ''),
                            posted_by=request.user,
                            status='OPEN' # Default to OPEN
                        )
                        ensure_parsed_jd(job, actor=request.user)
                        jobs_created += 1
                    
                    if errors:
                        # deciding whether to rollback or partial success. 
                        # For bulk, usually all or nothing is safer, or at least warn.
                        # user didn't specify. Let's rollback if ANY error for safety? 
                        # Or maybe just report errors. Let's report errors but keep successes for now 
                        # unless it's critical. Actually, `transaction.atomic` wraps the block. 
                        # If we don't raise exception, it commits.
                        # User wants robustness. Let's valid rows go through but warn about others?
                        pass 

            except Exception as e:
                logger.error(f"Bulk upload error: {e}")
                messages.error(request, "An unexpected error occurred during processing.")
                return render(request, 'jobs/job_bulk_upload.html', {'form': form})

            if jobs_created > 0:
                messages.success(request, f"Successfully uploaded {jobs_created} jobs!")
            
            if errors:
                messages.warning(request, f"Some rows were skipped: {'; '.join(errors[:5])}...")

            return redirect('job-list')
        
        return render(request, 'jobs/job_bulk_upload.html', {'form': form})

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse, reverse_lazy
from django.contrib import messages
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.template.loader import render_to_string
import re
import json
import csv
import io
import logging
from urllib.parse import urlencode

from .models import Job, PipelineEvent
from .dual_classification.audit import build_job_dual_classification_audit
from .dual_classification.effective import effective_raw_job_classification
from config.pagination import PAGE_SIZE_OPTIONS, get_page_size, build_pagination_window
from .classification_workspace import (
    ClassificationDetailV2View,
    ClassificationQueueV2View,
    ClassificationSettingsRequiredMixin,
    ClassificationSettingsV2View,
    ClassificationWorkspaceRequiredMixin,
)

logger = logging.getLogger(__name__)
from .forms import JobForm, JobBulkUploadForm


def _canonical_job_country(raw_value: str) -> tuple[str, str]:
    """Collapse dirty country/location strings into a canonical country code + label."""
    from harvest.enrichments import infer_country_from_location
    from harvest.location_resolver import COUNTRY_CODE_TO_NAME, _code_for_country

    raw = (raw_value or "").strip()
    if not raw:
        return "", ""

    code = _code_for_country(raw)
    inferred = ""
    if not code:
        inferred = infer_country_from_location(raw, "", "")
        code = _code_for_country(inferred)
    if code:
        return code, COUNTRY_CODE_TO_NAME.get(code, inferred or raw or code)
    return "", raw[:100]


def _build_job_country_options():
    canonical: dict[str, str] = {}
    for raw_country in (
        Job.objects.exclude(country="").exclude(country__isnull=True)
        .values_list("country", flat=True)
        .distinct()
    ):
        code, label = _canonical_job_country(raw_country)
        if code:
            canonical[code] = label
    return [
        {"value": code, "label": canonical[code]}
        for code in sorted(canonical, key=lambda item: canonical[item])
    ]


def _jobs_pipeline_pool_url() -> str:
    return f"{reverse('jobs-pipeline')}?tab=pool"


def _normalize_pool_search_by(raw_value: str, default: str = "title") -> str:
    value = (raw_value or default).strip().lower()
    if value not in {"title", "company", "all"}:
        return default
    return value


def _pool_filter_query_pairs(request) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key in (
        "q",
        "search_by",
        "posted_by",
        "company",
        "job_type",
        "job_source",
        "date_from",
        "date_to",
        "page_size",
    ):
        value = (request.GET.get(key) or "").strip()
        if value:
            pairs.append((key, value))
    return pairs


def _apply_job_pool_filters(request, qs, *, search_by: str | None = None):
    req = request.GET
    q = (req.get("q") or "").strip()
    search_mode = _normalize_pool_search_by(search_by or req.get("search_by") or "title")
    if q:
        if search_mode == "company":
            qs = qs.filter(company__icontains=q)
        elif search_mode == "all":
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(company__icontains=q)
                | Q(location__icontains=q)
                | Q(description__icontains=q)
            )
        else:
            qs = qs.filter(title__icontains=q)
    posted_by = req.get("posted_by")
    if posted_by and posted_by.isdigit():
        qs = qs.filter(posted_by_id=int(posted_by))
    company = (req.get("company") or "").strip()
    if company:
        qs = qs.filter(company__icontains=company)
    job_type = req.get("job_type")
    if job_type and job_type in dict(Job.JobType.choices):
        qs = qs.filter(job_type=job_type)
    job_source = (req.get("job_source") or "").strip()
    if job_source:
        qs = qs.filter(job_source__icontains=job_source)
    df = parse_date(req.get("date_from") or "")
    if df:
        qs = qs.filter(created_at__date__gte=df)
    dt = parse_date(req.get("date_to") or "")
    if dt:
        qs = qs.filter(created_at__date__lte=dt)
    return qs


def _build_job_pool_redirect_query(request) -> str:
    query: list[tuple[str, str]] = [("tab", "pool")]
    legacy_tab = (request.GET.get("tab") or "all").strip().lower()
    score = (request.GET.get("score") or "").strip().lower()
    if not score:
        score = legacy_tab if legacy_tab in {"high", "review", "flagged", "unscored"} else "all"
    if score:
        query.append(("score", score))

    search_by = (request.GET.get("search_by") or "").strip()
    if search_by:
        normalized_search_by = _normalize_pool_search_by(search_by)
        if normalized_search_by:
            query.append(("search_by", normalized_search_by))
    elif (request.GET.get("q") or "").strip():
        query.append(("search_by", "all"))

    lane = (request.GET.get("lane") or "").strip().lower()
    if lane:
        query.append(("lane", lane))
    gate = (request.GET.get("gate") or "").strip().upper()
    if gate:
        query.append(("gate", gate))

    for key in ("q", "posted_by", "company", "job_type", "job_source", "date_from", "date_to", "page_size", "page"):
        value = (request.GET.get(key) or "").strip()
        if value:
            query.append((key, value))
    return urlencode(query)


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
    archive_identity_conflict,
    JDParserService,
    match_consultants_for_job,
    ranked_consultants_for_job,
    find_potential_duplicate_jobs,
    ensure_parsed_jd,
    validate_job_quality,
)
from .routing import effective_routing_profile, persist_routing_profile
from submissions.models import ApplicationSubmission


def _rawjob_effective_confidence(raw_job) -> float | None:
    snapshot = getattr(raw_job, "classification_snapshot", None)
    if snapshot and getattr(snapshot, "final_confidence", None):
        return float(snapshot.final_confidence)
    value = (
        raw_job.category_confidence
        if raw_job.category_confidence is not None
        else raw_job.classification_confidence
    )
    return float(value) if value is not None else None


def _rawjob_salary_label(raw_job) -> str:
    if raw_job.salary_raw:
        return raw_job.salary_raw
    if raw_job.salary_min is None and raw_job.salary_max is None:
        return ""
    currency = raw_job.salary_currency or ""
    period = f"/{raw_job.salary_period.title()}" if raw_job.salary_period else ""
    if raw_job.salary_min is not None and raw_job.salary_max is not None:
        return f"{raw_job.salary_min:,.0f}-{raw_job.salary_max:,.0f} {currency}{period}".strip()
    if raw_job.salary_min is not None:
        return f"{raw_job.salary_min:,.0f}+ {currency}{period}".strip()
    return f"Up to {raw_job.salary_max:,.0f} {currency}{period}".strip()


def _rawjob_scope_label(raw_job, *, effective: dict | None = None) -> str:
    from harvest.location_resolver import COUNTRY_CODE_TO_NAME, _code_for_country

    effective = effective or effective_raw_job_classification(raw_job)
    country_codes = effective.get("country_codes") or []
    code = (
        (raw_job.country_code or "").upper()
        or (str(country_codes[0]).upper() if country_codes else "")
    )
    country_name = effective.get("country") or ""
    country_code_from_name = _code_for_country(country_name or "")
    if code:
        base = COUNTRY_CODE_TO_NAME.get(code, country_name or code)
    elif country_code_from_name:
        base = COUNTRY_CODE_TO_NAME.get(country_code_from_name, country_name)
    else:
        base = "Unknown"
    scope = raw_job.scope_status or "UNSCOPED"
    scope_label = {
        "PRIORITY_TARGET": "In scope",
        "REVIEW_UNKNOWN_COUNTRY": "Review",
        "COLD_NON_TARGET_COUNTRY": "Cold",
        "COLD_NO_LOCATION": "No location",
        "UNSCOPED": "Unscoped",
    }.get(scope, scope.replace("_", " ").title())
    return f"{base} · {scope_label}"


def _rawjob_jd_label(raw_job) -> str:
    if raw_job.has_description:
        return "Has JD"
    if getattr(raw_job, "is_cold", False) or getattr(raw_job, "jd_fetch_skipped", False):
        return "JD skipped by filter"
    if getattr(raw_job, "jd_backfill_locked_at", None):
        return "Fetching JD"
    return "Missing JD"


def _rawjob_blocker_label(raw_job, gate) -> str:
    if raw_job.sync_skip_reason:
        return raw_job.sync_skip_reason
    if raw_job.sync_status == "DUPLICATE":
        return "DUPLICATE"
    if raw_job.sync_status == "SKIPPED":
        return "SKIPPED"
    if (
        getattr(raw_job, "is_cold", False)
        or getattr(raw_job, "jd_fetch_skipped", False)
        or raw_job.filter_decision in {"COLD", "NO_MATCH"}
    ):
        return "FILTERED_OUT"
    if not raw_job.is_active:
        return "INACTIVE_POSTING"
    if not raw_job.has_description:
        return "MISSING_JD"
    confidence = _rawjob_effective_confidence(raw_job)
    from harvest.runtime_config import get_ready_stage_min_confidence

    if confidence is not None and confidence < get_ready_stage_min_confidence():
        return "LOW_CONFIDENCE"
    if gate and not gate.usable:
        return gate.reason_code
    return ""


def build_rawjob_pipeline_row(raw_job, *, gate=None, country_label: str = "") -> dict:
    if gate is None:
        try:
            from harvest.jd_gate import evaluate_raw_job_resume_gate

            gate = evaluate_raw_job_resume_gate(raw_job)
        except Exception:
            gate = None

    effective = effective_raw_job_classification(raw_job)
    confidence = _rawjob_effective_confidence(raw_job)
    from harvest.runtime_config import get_ready_stage_min_confidence

    ready_min_conf = get_ready_stage_min_confidence()
    confidence_pct = round(confidence * 100) if confidence is not None else None
    scope_label = country_label or _rawjob_scope_label(raw_job, effective=effective)
    blocker = _rawjob_blocker_label(raw_job, gate)
    jd_status = _rawjob_jd_label(raw_job)
    filter_decision = raw_job.filter_decision or ("TEST" if getattr(raw_job, "is_test_run", False) else "")

    location_type = effective.get("location_type") or raw_job.location_type
    is_remote = bool(effective.get("is_remote"))
    if location_type and location_type != "UNKNOWN":
        work_mode = str(location_type).title().replace("_", " ")
    elif is_remote:
        work_mode = "Remote"
    else:
        work_mode = "Unknown"

    snapshot = getattr(raw_job, "classification_snapshot", None)
    classification_source = (
        getattr(snapshot, "approved_source", "")
        if snapshot and getattr(snapshot, "approved_output", None)
        else ""
    ) or "raw_job"

    return {
        "company": raw_job.company_name or "",
        "platform": raw_job.platform_slug or (raw_job.job_platform.name if raw_job.job_platform else ""),
        "job_category": effective.get("job_category") or "",
        "job_domain": effective.get("job_domain") or "",
        "department_normalized": effective.get("department_normalized") or "",
        "country": effective.get("country") or "",
        "country_codes": effective.get("country_codes") or [],
        "classification_source": classification_source,
        "confidence_pct": confidence_pct,
        "confidence_label": f"{confidence_pct}%" if confidence_pct is not None else "Unknown",
        "confidence_level": (
            "high" if confidence is not None and confidence >= 0.75
            else "medium" if confidence is not None and confidence >= ready_min_conf
            else "low" if confidence is not None
            else "unknown"
        ),
        "jd_status": jd_status,
        "jd_level": "good" if raw_job.has_description else ("muted" if blocker == "FILTERED_OUT" else "warn"),
        "country_scope": scope_label,
        "scope_status": raw_job.scope_status or "",
        "gate_code": blocker or (gate.reason_code if gate else ""),
        "gate_pass": bool(gate.usable) if gate else False,
        "blocker": blocker,
        "work_mode": work_mode,
        "salary": _rawjob_salary_label(raw_job),
        "external_id": raw_job.external_id or "",
        "posted": raw_job.posted_date.strftime("%b %-d, %Y") if raw_job.posted_date else "",
        "harvested": raw_job.fetched_at.strftime("%b %-d, %H:%M") if raw_job.fetched_at else "",
        "filter_decision": filter_decision,
        "filter_reason": raw_job.filter_reason or "",
        "is_test_run": bool(getattr(raw_job, "is_test_run", False)),
    }


def attach_job_dual_classification_audit(job):
    audit = build_job_dual_classification_audit(job)
    setattr(job, "dual_classification_audit", audit)
    return audit


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

    country_filter = request.GET.get('country')
    if country_filter:
        canonical_code, canonical_label = _canonical_job_country(country_filter)
        if canonical_code:
            matching_ids = [
                job_id
                for job_id, raw_country in qs.exclude(country="").exclude(country__isnull=True)
                .values_list("id", "country")
                if _canonical_job_country(raw_country)[0] == canonical_code
            ]
            qs = qs.filter(pk__in=matching_ids)
        elif canonical_label:
            qs = qs.filter(country__iexact=canonical_label)

    department_filter = request.GET.get('department')
    if department_filter and department_filter in dict(Job.Department.choices):
        qs = qs.filter(department=department_filter)

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
        qs = apply_job_list_filters(
            super().get_queryset().select_related('posted_by').prefetch_related('marketing_roles'),
            self.request,
        )
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
        context['selected_country'] = _canonical_job_country(self.request.GET.get('country', ''))[0]
        context['selected_department'] = self.request.GET.get('department', '')
        context['show_pool_redirect_hint'] = self.request.GET.get('status', '') == Job.Status.POOL
        context['department_choices'] = Job.Department.choices
        context['country_options'] = _build_job_country_options()
        qd = self.request.GET.copy()
        qd.pop('page', None)
        context['pagination_query'] = qd.urlencode()
        context['page_size'] = get_page_size(self.request, default=100)
        context['page_size_options'] = PAGE_SIZE_OPTIONS
        for job in context.get("jobs", []):
            attach_job_dual_classification_audit(job)
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

    def get_queryset(self):
        return super().get_queryset().select_related('posted_by', 'source_raw_job').prefetch_related('marketing_roles')

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
        if job:
            context["routing_profile"] = effective_routing_profile(job)
            context["routing_profile_json"] = json.dumps(context["routing_profile"], indent=2)
        if job:
            context["dual_classification_audit"] = attach_job_dual_classification_audit(job)

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


class JobRoutingOverrideView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        action = (request.POST.get("action") or "save").strip()
        if action == "clear":
            job.routing_override = {}
            job.routing_override_updated_at = timezone.now()
            job.save(update_fields=["routing_override", "routing_override_updated_at"])
            persist_routing_profile(job, save=True)
            messages.success(request, "Routing override cleared.")
            return redirect("job-detail", pk=pk)

        visa_raw = (request.POST.get("visa_sponsorship") or "").strip().lower()
        if visa_raw == "true":
            visa_sponsorship = True
        elif visa_raw == "false":
            visa_sponsorship = False
        else:
            visa_sponsorship = None

        override = {
            "seniority_primary": (request.POST.get("seniority_primary") or "").strip().lower(),
            "years_min": (request.POST.get("years_min") or "").strip(),
            "years_max": (request.POST.get("years_max") or "").strip(),
            "country_mode": (request.POST.get("country_mode") or "").strip(),
            "country_codes": [c.strip().upper() for c in (request.POST.get("country_codes") or "").split(",") if c.strip()],
            "work_mode": (request.POST.get("work_mode") or "").strip(),
            "visa_sponsorship": visa_sponsorship,
            "work_authorization": (request.POST.get("work_authorization") or "").strip(),
            "clearance_required": request.POST.get("clearance_required") == "1",
        }
        job.routing_override = {key: value for key, value in override.items() if value not in ("", [], None)}
        job.routing_override_updated_at = timezone.now()
        job.save(update_fields=["routing_override", "routing_override_updated_at"])
        persist_routing_profile(job, save=True)
        messages.success(request, "Routing override saved.")
        return redirect("job-detail", pk=pk)

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
        try:
            from harvest.services.manual_job_bridge import ensure_rawjob_for_job

            ensure_rawjob_for_job(self.object, source="manual")
        except Exception:
            logger.exception("Manual job RawJob bridge failed for job %s", self.object.pk)
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
                f"Job \"{self.object.title}\" added to the Vetting Queue for review. "
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
            return _jobs_pipeline_pool_url()
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
                            try:
                                from harvest.services.manual_job_bridge import ensure_rawjob_for_job

                                ensure_rawjob_for_job(job, source="bulk_upload")
                            except Exception:
                                logger.exception("Bulk upload RawJob bridge failed for job %s", job.pk)
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
        return Job.objects.filter(is_archived=True).select_related(
            'posted_by', 'archived_by'
        ).order_by('-archived_at')


class JobIdentityRepairView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """Archive duplicate active jobs inside a known identity conflict group."""

    def post(self, request):
        group_type = (request.POST.get("group_type") or "").strip()
        group_key = (request.POST.get("group_key") or "").strip()
        result = archive_identity_conflict(group_type, group_key, actor=request.user)
        if result.get("archived"):
            survivor = result.get("survivor")
            survivor_label = f" Survivor: #{survivor.pk} {survivor.title}." if survivor else ""
            messages.success(request, f"Archived {result['archived']} duplicate job(s).{survivor_label}")
        else:
            messages.warning(request, "No duplicate jobs were archived for that conflict.")
        return redirect(request.POST.get("next") or "jobs-classification-metrics")


# ─── Vetting Queue / Validation Pipeline ─────────────────────────────────────

class JobPoolView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Compatibility wrapper: keep legacy /jobs/pool/ links alive."""

    def get(self, request):
        return redirect(f"{reverse('jobs-pipeline')}?{_build_job_pool_redirect_query(request)}")


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
            existing_dual = dict((job.validation_result or {}).get("dual_classification") or {})
            if existing_dual:
                result["dual_classification"] = existing_dual
            job.validation_score = result['score']
            job.validation_result = result
            job.validation_run_at = timezone.now()
            job.save(update_fields=['validation_score', 'validation_result', 'validation_run_at'])
            messages.success(request, f"Validation complete — score {result['score']}/100.")

        if request.POST.get('redirect_to') == 'job-detail':
            return redirect('job-detail', pk=job.pk)
        return redirect(request.POST.get('next') or _jobs_pipeline_pool_url())


class JobApproveView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Move a POOL job to OPEN (approve it)."""
    def post(self, request, pk):
        from .gating import apply_gate_result_to_job, evaluate_job_gate

        job = get_object_or_404(Job, pk=pk)
        if job.status != Job.Status.POOL:
            messages.warning(request, f"\"{job.title}\" is not in the pool (status: {job.get_status_display()}).")
            return redirect(_jobs_pipeline_pool_url())
        gate = evaluate_job_gate(job)
        apply_gate_result_to_job(job, gate)
        job.gate_checked_at = timezone.now()
        job.save(update_fields=[
            'hard_gate_passed', 'gate_status', 'vet_lane', 'pipeline_reason_code', 'pipeline_reason_detail',
            'hard_gate_failures', 'hard_gate_checks', 'data_quality_score', 'trust_score',
            'candidate_fit_score', 'vet_priority_score', 'gate_checked_at', 'updated_at'
        ])
        if not job.hard_gate_passed or job.vet_lane == Job.VetLane.BLOCKED:
            messages.error(
                request,
                f"\"{job.title}\" is blocked by vet gate ({job.pipeline_reason_code or 'UNKNOWN'}). "
                "Fix quality/gate issues before approval."
            )
            return redirect(request.POST.get('next') or _jobs_pipeline_pool_url())
        job.status = Job.Status.OPEN
        job.stage = Job.Stage.LIVE
        job.stage_changed_at = timezone.now()
        job.vet_approved_at = timezone.now()
        job.validated_by = request.user
        job.validation_run_at = timezone.now()
        job.save(update_fields=['status', 'stage', 'stage_changed_at', 'vet_approved_at', 'validated_by', 'validation_run_at', 'updated_at'])
        PipelineEvent.record(
            job=job,
            from_stage=Job.Stage.VETTED,
            to_stage=Job.Stage.LIVE,
            task_name="jobs.vet_approve",
            status=PipelineEvent.Status.SUCCESS,
            meta={
                "actor_user_id": request.user.id,
                "gate_status": job.gate_status,
                "vet_lane": job.vet_lane,
            },
        )
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
        return redirect(request.POST.get('next') or _jobs_pipeline_pool_url())


class JobRejectView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Reject a POOL job — moves to CLOSED and records reason."""
    def post(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        if job.status != Job.Status.POOL:
            messages.warning(request, f"\"{job.title}\" is not in the pool.")
            return redirect(_jobs_pipeline_pool_url())
        reason = request.POST.get('rejection_reason', '').strip()
        if not reason:
            messages.error(request, "Please provide a rejection reason.")
            return redirect(_jobs_pipeline_pool_url())
        job.status = Job.Status.CLOSED
        job.stage = Job.Stage.ARCHIVED
        job.stage_changed_at = timezone.now()
        job.gate_status = Job.GateStatus.BLOCKED
        job.vet_lane = Job.VetLane.BLOCKED
        job.pipeline_reason_code = "REJECTED_BY_REVIEWER"
        job.pipeline_reason_detail = reason
        job.rejection_reason = reason
        job.rejected_by = request.user
        job.rejected_at = timezone.now()
        job.save(update_fields=[
            'status', 'stage', 'stage_changed_at', 'gate_status', 'vet_lane',
            'pipeline_reason_code', 'pipeline_reason_detail',
            'rejection_reason', 'rejected_by', 'rejected_at', 'updated_at'
        ])
        PipelineEvent.record(
            job=job,
            from_stage=Job.Stage.VETTED,
            to_stage=Job.Stage.ARCHIVED,
            task_name="jobs.vet_reject",
            status=PipelineEvent.Status.SUCCESS,
            meta={
                "actor_user_id": request.user.id,
                "reason_code": "REJECTED_BY_REVIEWER",
                "reason": reason[:240],
            },
        )
        try:
            from .notify import notify_job_pool_status
            notify_job_pool_status(job, approved=False, actor=request.user)
        except Exception:
            logger.exception("Rejection notification failed for job %s", pk)
        messages.success(request, f"✗ \"{job.title}\" rejected and closed.")
        return redirect(request.POST.get('next') or _jobs_pipeline_pool_url())


class JobBulkApproveView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_bulk_ops'
    """Bulk-approve multiple POOL jobs at once."""
    def post(self, request):
        from .gating import apply_gate_result_to_job, evaluate_job_gate

        job_ids = request.POST.getlist('job_ids')
        if not job_ids:
            messages.warning(request, "No jobs selected.")
            return redirect(request.POST.get('next') or _jobs_pipeline_pool_url())
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
                gate = evaluate_job_gate(job)
                apply_gate_result_to_job(job, gate)
                job.gate_checked_at = now
                job.save(update_fields=[
                    'hard_gate_passed', 'gate_status', 'vet_lane', 'pipeline_reason_code', 'pipeline_reason_detail',
                    'hard_gate_failures', 'hard_gate_checks', 'data_quality_score', 'trust_score',
                    'candidate_fit_score', 'vet_priority_score', 'gate_checked_at', 'updated_at'
                ])
                if not job.hard_gate_passed or job.vet_lane == Job.VetLane.BLOCKED:
                    skipped += 1
                    continue
                job.status = Job.Status.OPEN
                job.stage = Job.Stage.LIVE
                job.stage_changed_at = now
                job.vet_approved_at = now
                job.validated_by = request.user
                job.validation_run_at = now
                job.save(update_fields=[
                    'status', 'stage', 'stage_changed_at', 'vet_approved_at',
                    'validated_by', 'validation_run_at', 'updated_at'
                ])
                PipelineEvent.record(
                    job=job,
                    from_stage=Job.Stage.VETTED,
                    to_stage=Job.Stage.LIVE,
                    task_name="jobs.bulk_vet_approve",
                    status=PipelineEvent.Status.SUCCESS,
                    meta={
                        "actor_user_id": request.user.id,
                        "gate_status": job.gate_status,
                        "vet_lane": job.vet_lane,
                    },
                )
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
        return redirect(request.POST.get('next') or _jobs_pipeline_pool_url())


class JobPoolRefreshLinksView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    employee_feature_key = 'employee_job_pool'
    """Manual trigger from UI: refresh posting URL health in batch."""

    def post(self, request):
        try:
            from .tasks import refresh_all_pool_link_health_task

            # Run async when Celery is up; fallback inline when queue unavailable.
            try:
                r = refresh_all_pool_link_health_task.delay(5000, 5000)
                from urllib.parse import urlencode

                from django.urls import reverse

                q = urlencode({"tp": r.id, "tpl": "Job posting URL check"})
                messages.success(
                    request,
                    "Started full link-health refresh in background for manual + harvested jobs.",
                )
                return redirect(f"{_jobs_pipeline_pool_url()}&{q}")
            except Exception:
                result = refresh_all_pool_link_health_task.apply(kwargs={"manual_batch_size": 5000, "raw_batch_size": 5000}).get()
                messages.success(
                    request,
                    "Link status refresh completed now. "
                    f"Manual jobs checked: {result.get('manual_jobs', {}).get('processed', 0)}. "
                    f"Harvest-linked raw jobs checked: {result.get('linked_raw_jobs', {}).get('checked', 0)}.",
                )
        except Exception:
            logger.exception("Manual pool link refresh failed")
            messages.error(request, "Could not refresh link statuses right now. Please try again.")
        return redirect(_jobs_pipeline_pool_url())


# ─── Jobs Command Center — unified pipeline hub ──────────────────────────────

class JobsPipelineView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """
    Unified pipeline hub: Harvesting (raw) | Vetting (pool) | Live | Archived
    Replaces separate /harvest/raw-jobs/, /jobs/pool/, /jobs/ views with one
    tabbed command center.
    """
    template_name = 'jobs/pipeline.html'
    pipeline_tab_page_size = 100

    def _get_pipeline_paginate_by(self, request):
        return get_page_size(request, default=self.pipeline_tab_page_size)

    def _paginate_pipeline_queryset(self, request, qs, page_num=1):
        paginator = Paginator(qs, self._get_pipeline_paginate_by(request))
        try:
            page_obj = paginator.page(page_num)
        except Exception:
            return paginator, None
        return paginator, page_obj

    def _build_pool_page_context(self, request, q, search_by, page_num=1):
        score_tab = (request.GET.get('score') or 'all').strip().lower()
        lane_tab = (request.GET.get('lane') or 'all').strip().lower()
        gate_tab = request.GET.get('gate', 'all').upper()
        qs = Job.objects.filter(status=Job.Status.POOL, is_archived=False)
        qs = _apply_job_pool_filters(request, qs, search_by=search_by)
        now = timezone.now()
        age_24h = now - timezone.timedelta(hours=24)
        age_6h = now - timezone.timedelta(hours=6)
        age_1h = now - timezone.timedelta(hours=1)
        pool_filtered_total = qs.count()
        pool_high = qs.filter(validation_score__gte=80).count()
        pool_review = qs.filter(validation_score__gte=50, validation_score__lt=80).count()
        pool_flagged = qs.filter(validation_score__lt=50).count()
        pool_unscored = qs.filter(validation_score__isnull=True).count()
        pool_auto_lane = qs.filter(vet_lane=Job.VetLane.AUTO).count()
        pool_human_lane = qs.filter(vet_lane=Job.VetLane.HUMAN).count()
        pool_blocked_lane = qs.filter(vet_lane=Job.VetLane.BLOCKED).count()
        pool_blocked_gate = qs.filter(gate_status=Job.GateStatus.BLOCKED).count()
        pool_aging_lt_1h = qs.filter(queue_entered_at__gte=age_1h).count()
        pool_aging_1_6h = qs.filter(queue_entered_at__lt=age_1h, queue_entered_at__gte=age_6h).count()
        pool_aging_6_24h = qs.filter(queue_entered_at__lt=age_6h, queue_entered_at__gte=age_24h).count()
        pool_aging_gt_24h = qs.filter(queue_entered_at__lt=age_24h).count()
        pool_gate_summary = {
            "eligible": qs.filter(gate_status=Job.GateStatus.ELIGIBLE).count(),
            "review": qs.filter(gate_status=Job.GateStatus.REVIEW).count(),
            "blocked": qs.filter(gate_status=Job.GateStatus.BLOCKED).count(),
        }
        if score_tab == 'high':
            qs = qs.filter(validation_score__gte=80)
        elif score_tab == 'review':
            qs = qs.filter(validation_score__gte=50, validation_score__lt=80)
        elif score_tab == 'flagged':
            qs = qs.filter(validation_score__lt=50)
        elif score_tab == 'unscored':
            qs = qs.filter(validation_score__isnull=True)
        if lane_tab == 'auto':
            qs = qs.filter(vet_lane=Job.VetLane.AUTO)
        elif lane_tab == 'human':
            qs = qs.filter(vet_lane=Job.VetLane.HUMAN)
        elif lane_tab == 'blocked':
            qs = qs.filter(vet_lane=Job.VetLane.BLOCKED)
        elif lane_tab == 'aging':
            qs = qs.filter(queue_entered_at__lt=age_24h)
        if gate_tab == Job.GateStatus.ELIGIBLE:
            qs = qs.filter(gate_status=Job.GateStatus.ELIGIBLE)
        elif gate_tab == Job.GateStatus.REVIEW:
            qs = qs.filter(gate_status=Job.GateStatus.REVIEW)
        elif gate_tab == Job.GateStatus.BLOCKED:
            qs = qs.filter(gate_status=Job.GateStatus.BLOCKED)
        if lane_tab == 'approved_recent':
            result_qs = Job.objects.filter(
                status=Job.Status.OPEN,
                is_archived=False,
                vet_approved_at__isnull=False,
            )
            result_qs = _apply_job_pool_filters(request, result_qs, search_by=search_by)
            result_qs = result_qs.select_related('posted_by', 'company_obj').order_by('-vet_approved_at')
        else:
            result_qs = qs.select_related('posted_by', 'company_obj').order_by('-created_at')
        paginator, page_obj = self._paginate_pipeline_queryset(request, result_qs, page_num)
        tab_jobs = page_obj.object_list if page_obj else []
        for job in tab_jobs:
            attach_job_dual_classification_audit(job)
        pool_filter_query = urlencode(_pool_filter_query_pairs(request))
        poster_ids = (
            Job.objects.filter(status=Job.Status.POOL, is_archived=False)
            .values_list('posted_by_id', flat=True)
            .distinct()
        )
        return {
            'tab_jobs': tab_jobs,
            'page_obj': page_obj,
            'pipeline_tab_has_next': page_obj.has_next() if page_obj else False,
            'pipeline_tab_next_page': page_obj.next_page_number() if page_obj and page_obj.has_next() else None,
            'pipeline_tab_total_filtered': paginator.count,
            'score_tab': score_tab,
            'lane_tab': lane_tab,
            'gate_tab': gate_tab,
            'pool_extra': {
                'score_tab': score_tab,
                'lane_tab': lane_tab,
                'gate_tab': gate_tab,
                'pool_high': pool_high,
                'pool_review': pool_review,
                'pool_flagged': pool_flagged,
                'pool_unscored': pool_unscored,
                'pool_filtered_total': pool_filtered_total,
                'pool_auto_lane': pool_auto_lane,
                'pool_human_lane': pool_human_lane,
                'pool_blocked_lane': pool_blocked_lane,
                'pool_blocked_gate': pool_blocked_gate,
                'pool_gate_summary': pool_gate_summary,
                'pool_aging_lt_1h': pool_aging_lt_1h,
                'pool_aging_1_6h': pool_aging_1_6h,
                'pool_aging_6_24h': pool_aging_6_24h,
                'pool_aging_gt_24h': pool_aging_gt_24h,
                'pool_filter_query': pool_filter_query,
                'pool_posters': User.objects.filter(pk__in=poster_ids).order_by(
                    'first_name', 'last_name', 'username'
                ),
                'job_type_choices': Job.JobType.choices,
                'filter_posted_by': request.GET.get('posted_by') or '',
                'filter_company': (request.GET.get('company') or '').strip(),
                'filter_job_type': request.GET.get('job_type') or '',
                'filter_job_source': (request.GET.get('job_source') or '').strip(),
                'filter_date_from': request.GET.get('date_from') or '',
                'filter_date_to': request.GET.get('date_to') or '',
                'page_size': self._get_pipeline_paginate_by(request),
                'page_size_options': PAGE_SIZE_OPTIONS,
            },
        }

    def _build_live_page_context(self, q, page_num=1):
        qs = Job.objects.filter(status=Job.Status.OPEN, is_archived=False)
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(company__icontains=q))
        qs = qs.select_related('posted_by', 'company_obj').annotate(sub_count=Count('submissions')).order_by('-created_at')
        paginator, page_obj = self._paginate_pipeline_queryset(self.request, qs, page_num)
        return {
            'tab_jobs': page_obj.object_list if page_obj else [],
            'page_obj': page_obj,
            'pipeline_tab_has_next': page_obj.has_next() if page_obj else False,
            'pipeline_tab_next_page': page_obj.next_page_number() if page_obj and page_obj.has_next() else None,
            'pipeline_tab_total_filtered': paginator.count,
        }

    def _build_archived_page_context(self, q, page_num=1):
        qs = Job.objects.filter(is_archived=True)
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(company__icontains=q))
        qs = qs.select_related('posted_by').order_by('-archived_at')
        paginator, page_obj = self._paginate_pipeline_queryset(self.request, qs, page_num)
        return {
            'tab_jobs': page_obj.object_list if page_obj else [],
            'page_obj': page_obj,
            'pipeline_tab_has_next': page_obj.has_next() if page_obj else False,
            'pipeline_tab_next_page': page_obj.next_page_number() if page_obj and page_obj.has_next() else None,
            'pipeline_tab_total_filtered': paginator.count,
        }

    def _pipeline_rows_json_response(self, request, page_context, template_name):
        page_obj = page_context.get("page_obj")
        if not page_obj:
            return JsonResponse({
                "rows_html": "",
                "has_next": False,
                "next_page": None,
                "total": 0,
                "num_pages": 0,
            })
        rows_html = render_to_string(template_name, page_context, request=request)
        return JsonResponse({
            "rows_html": rows_html,
            "has_next": page_obj.has_next(),
            "next_page": page_obj.next_page_number() if page_obj.has_next() else None,
            "total": page_context.get("pipeline_tab_total_filtered", 0),
            "num_pages": page_obj.paginator.num_pages,
            "count": len(page_context.get("tab_jobs", [])),
        })

    def _raw_json_response(self, request):
        from harvest.models import RawJob
        from harvest.enrichments import infer_country_from_location
        from harvest.jd_gate import evaluate_raw_job_resume_gate
        from harvest.services.rawjob_query import apply_rawjob_filters
        # Keep JSON loader query lightweight and avoid select_related+defer conflicts.
        qs = RawJob.objects.prefetch_related("classification_snapshot").order_by('-fetched_at')
        qs = apply_rawjob_filters(qs, request.GET)
        qs = qs.only(
            "id", "company_name", "platform_slug", "title", "original_url",
            "location_raw", "is_remote", "employment_type", "experience_level",
            "salary_min", "salary_max", "salary_raw", "posted_date", "fetched_at",
            "sync_status", "has_description", "is_active",
            "job_category", "job_domain",
            "department", "department_normalized", "state", "country",
            "education_required", "languages_required", "certifications",
            "licenses_required", "clearance_required", "clearance_level",
            "schedule_type", "shift_schedule", "travel_pct_min", "travel_pct_max",
            "resume_ready_score", "classification_confidence", "quality_score",
            "jd_quality_score", "raw_payload", "job_keywords", "title_keywords",
            "company_industry", "company_size", "word_count",
            "category_confidence", "country_code", "scope_status", "scope_reason",
            "sync_skip_reason", "external_id", "location_type", "salary_currency",
            "salary_period", "jd_backfill_locked_at",
            "filter_decision", "filter_reason", "is_cold", "jd_fetch_skipped",
            "is_test_run", "fetch_batch",
        )

        paginator = Paginator(qs, 100)
        try:
            page_num = int(request.GET.get("page", 1))
            page_obj = paginator.page(page_num)
        except Exception:
            return JsonResponse({"jobs": [], "has_next": False, "total": 0, "num_pages": 0, "next_page": None})

        jobs_data = []
        for job in page_obj.object_list:
            jd_gate = evaluate_raw_job_resume_gate(job)
            effective = effective_raw_job_classification(job)
            detected_country = infer_country_from_location(
                location_raw=job.location_raw or "",
                state=job.state or "",
                country=effective.get("country") or "",
            )
            row = build_rawjob_pipeline_row(job, gate=jd_gate, country_label=detected_country or "")
            jobs_data.append({
                "id": job.pk,
                "company_name": (job.company_name or "")[:48],
                "platform_slug": row["platform"],
                "title": (job.title or "")[:60],
                "job_category": row["job_category"],
                "job_domain": row["job_domain"],
                "classification_source": row["classification_source"],
                "filter_decision": row["filter_decision"],
                "filter_reason": row["filter_reason"][:90],
                "is_test_run": row["is_test_run"],
                "location_raw": (job.location_raw or "")[:64],
                "fetched_at": row["harvested"],
                "is_active": bool(job.is_active),
                "stage": job.pipeline_stage_label(),
                "resume_jd_usable": jd_gate.usable,
                "resume_jd_reason_code": jd_gate.reason_code,
                "country": (detected_country or row["country"] or "")[:48],
                "confidence_label": row["confidence_label"],
                "confidence_level": row["confidence_level"],
                "jd_status": row["jd_status"],
                "jd_level": row["jd_level"],
                "country_scope": row["country_scope"],
                "scope_status": row["scope_status"],
                "gate_code": row["gate_code"],
                "gate_pass": row["gate_pass"],
                "blocker": row["blocker"],
                "work_mode": row["work_mode"],
                "salary": row["salary"],
                "external_id": row["external_id"],
                "posted": row["posted"],
                "detail_url": str(reverse_lazy("harvest-rawjob-detail", args=[job.pk])),
            })

        return JsonResponse({
            "jobs": jobs_data,
            "has_next": page_obj.has_next(),
            "next_page": page_obj.next_page_number() if page_obj.has_next() else None,
            "total": paginator.count,
            "num_pages": paginator.num_pages,
        })

    def get(self, request):
        tab = (request.GET.get('tab', '') or '').strip().lower()
        if not tab:
            legacy_subtab = (request.GET.get('_subtab', '') or '').strip().lower()
            if legacy_subtab in {"jobs", "batches", "companies", "stats", "insights"}:
                tab = "raw"
        if tab not in {"pool", "raw", "live", "archived"}:
            tab = "pool"
        q = (request.GET.get('q') or '').strip()
        search_by = (request.GET.get('search_by') or 'title').strip()
        if (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            and tab == "raw"
            and (request.GET.get("raw_json") or "").strip() == "1"
        ):
            return self._raw_json_response(request)
        if (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            and (request.GET.get("pipeline_json") or "").strip() == "1"
        ):
            page_num = request.GET.get("page", 1)
            if tab == "pool":
                page_context = self._build_pool_page_context(request, q, search_by, page_num=page_num)
                return self._pipeline_rows_json_response(request, page_context, "jobs/_pipeline_pool_rows.html")
            if tab == "live":
                page_context = self._build_live_page_context(q, page_num=page_num)
                return self._pipeline_rows_json_response(request, page_context, "jobs/_pipeline_live_rows.html")
            if tab == "archived":
                page_context = self._build_archived_page_context(q, page_num=page_num)
                return self._pipeline_rows_json_response(request, page_context, "jobs/_pipeline_archived_rows.html")

        raw_selected_stage = (request.GET.get("stage") or "").strip().upper()

        # ── Summary stats (always computed) ─────────────────────────────────
        from harvest.models import RawJob, FetchBatch
        from harvest.services.pipeline_snapshot import (
            load_rawjobs_dashboard_stats,
            raw_jobs_workflow_insights,
        )
        from harvest.services.rawjob_query import (
            FILTER_STATE_KEYS,
            apply_rawjob_filters,
            build_funnel_counts,
            effective_classification_q,
            filtered_out_q,
            production_rawjobs_queryset,
            ready_stage_q,
        )
        from harvest.runtime_config import get_ready_stage_min_confidence

        raw_stats = load_rawjobs_dashboard_stats(force_refresh=False)
        raw_insights = raw_jobs_workflow_insights()
        raw_base_qs = production_rawjobs_queryset()
        raw_total = raw_stats.get("total", 0)
        raw_test_total = raw_stats.get("test_rows")
        if raw_test_total is None:
            raw_test_total = RawJob.objects.filter(is_test_run=True).count()
        raw_funnel = (raw_insights or {}).get("funnel") or build_funnel_counts(raw_base_qs)
        ready_min_conf = get_ready_stage_min_confidence()
        raw_ready_q = ready_stage_q(min_conf=ready_min_conf)
        raw_qualified_pending = raw_base_qs.filter(raw_ready_q, sync_status=RawJob.SyncStatus.PENDING).count()
        raw_qualified_synced = raw_base_qs.filter(raw_ready_q, sync_status=RawJob.SyncStatus.SYNCED).count()
        raw_pending_total = raw_stats.get("pending", 0)
        raw_blocked_missing_jd = raw_base_qs.filter(
            sync_status=RawJob.SyncStatus.PENDING,
            has_description=False,
            is_cold=False,
            jd_fetch_skipped=False,
        ).exclude(filter_decision__in=["COLD", "NO_MATCH"]).count()
        raw_blocked_filtered_out = raw_base_qs.filter(
            sync_status=RawJob.SyncStatus.PENDING,
        ).filter(filtered_out_q()).count()
        raw_blocked_inactive = raw_base_qs.filter(sync_status=RawJob.SyncStatus.PENDING, is_active=False).count()
        raw_blocked_low_conf = (
            raw_base_qs.filter(sync_status=RawJob.SyncStatus.PENDING, has_description=True, is_active=True)
            .filter(is_cold=False, jd_fetch_skipped=False)
            .exclude(filter_decision__in=["COLD", "NO_MATCH"])
            .filter(effective_classification_q(min_conf=0.01))
            .exclude(effective_classification_q(min_conf=ready_min_conf))
            .count()
        )
        raw_duplicates_total = (raw_insights or {}).get("duplicates", {}).get("total")
        if raw_duplicates_total is None:
            raw_duplicates_total = raw_base_qs.filter(sync_status=RawJob.SyncStatus.SKIPPED).count()
        raw_gate_summary = {
            "pending_total": raw_pending_total,
            "qualified_pending": raw_qualified_pending,
            "qualified_synced": raw_qualified_synced,
            "blocked_missing_jd": raw_blocked_missing_jd,
            "blocked_filtered_out": raw_blocked_filtered_out,
            "blocked_inactive": raw_blocked_inactive,
            "blocked_low_conf": raw_blocked_low_conf,
            "duplicates": raw_duplicates_total,
            "test_rows": raw_test_total,
        }
        pool_total = Job.objects.filter(status=Job.Status.POOL, is_archived=False).count()
        pool_qs_all = Job.objects.filter(status=Job.Status.POOL, is_archived=False)
        live_total = Job.objects.filter(status=Job.Status.OPEN, is_archived=False).count()
        archived_total = Job.objects.filter(is_archived=True).count()
        vet_gate_summary = {
            "eligible": pool_qs_all.filter(gate_status=Job.GateStatus.ELIGIBLE).count(),
            "review": pool_qs_all.filter(gate_status=Job.GateStatus.REVIEW).count(),
            "blocked": pool_qs_all.filter(gate_status=Job.GateStatus.BLOCKED).count(),
            "auto_lane": pool_qs_all.filter(vet_lane=Job.VetLane.AUTO).count(),
            "human_lane": pool_qs_all.filter(vet_lane=Job.VetLane.HUMAN).count(),
            "blocked_lane": pool_qs_all.filter(vet_lane=Job.VetLane.BLOCKED).count(),
        }

        # Last harvest batch info
        last_batch = FetchBatch.objects.order_by('-created_at').first()

        # ── Tab-specific data ────────────────────────────────────────────────
        tab_jobs = None
        tab_raw = None
        lane_tab = "all"
        score_tab = "all"
        gate_tab = "all"
        pipeline_tab_has_next = False
        pipeline_tab_next_page = None
        pipeline_tab_total_filtered = 0
        raw_filter_passthrough: list[tuple[str, str]] = []
        raw_stage_links: dict[str, str] = {}
        raw_blocker_links: dict[str, str] = {}

        def _raw_query_with_overrides(
            base_pairs: list[tuple[str, str]],
            overrides: dict[str, str],
            remove_keys: set[str] | None = None,
        ) -> str:
            remove = set(remove_keys or set())
            remove.update(overrides.keys())
            pairs = [(k, v) for (k, v) in base_pairs if k not in remove]
            pairs.extend((k, v) for k, v in overrides.items() if v != "")
            return urlencode(pairs)

        if tab == 'raw':
            qs = RawJob.objects.select_related('company', 'job_platform').prefetch_related('classification_snapshot').order_by('-fetched_at')
            qs = apply_rawjob_filters(qs, request.GET)
            for key in FILTER_STATE_KEYS:
                if key == "q":
                    continue
                value = (request.GET.get(key) or "").strip()
                if value:
                    raw_filter_passthrough.append((key, value))
            # Funnel clicks should show canonical stage cohorts. Preserve only scope filters
            # (search/platform/company/date), and clear mutually-exclusive gate filters.
            scope_keys = {
                "q",
                "platform",
                "company_id",
                "label_pk",
                "country_code",
                "marketing_role",
                "include_test",
                "date_from",
                "date_to",
                "last_hours",
                "fetched_from",
                "fetched_to",
            }
            raw_scope_pairs: list[tuple[str, str]] = [("tab", "raw")]
            for key, values in request.GET.lists():
                if key not in scope_keys:
                    continue
                for value in values:
                    value_s = (value or "").strip()
                    if value_s:
                        raw_scope_pairs.append((key, value_s))
            raw_stage_links = {
                "FETCHED": _raw_query_with_overrides(raw_scope_pairs, {}, remove_keys={"stage"}),
                "PARSED": _raw_query_with_overrides(raw_scope_pairs, {"stage": "PARSED"}, remove_keys={"stage"}),
                "ENRICHED": _raw_query_with_overrides(raw_scope_pairs, {"stage": "ENRICHED"}, remove_keys={"stage"}),
                "CLASSIFIED": _raw_query_with_overrides(raw_scope_pairs, {"stage": "CLASSIFIED"}, remove_keys={"stage"}),
                "READY": _raw_query_with_overrides(raw_scope_pairs, {"stage": "READY"}, remove_keys={"stage"}),
                "SYNCED": _raw_query_with_overrides(raw_scope_pairs, {"stage": "SYNCED"}, remove_keys={"stage"}),
            }
            raw_blocker_links = {
                "qualified_pending": _raw_query_with_overrides(
                    raw_scope_pairs,
                    {"stage": "READY", "sync_status": "PENDING"},
                    remove_keys={"stage", "sync_status", "has_jd", "is_active", "classification_bucket"},
                ),
                "blocked_missing_jd": _raw_query_with_overrides(
                    raw_scope_pairs,
                    {"sync_status": "PENDING", "has_jd": "0", "filter_decision": ""},
                    remove_keys={"stage", "sync_status", "has_jd", "is_active", "classification_bucket", "filter_decision"},
                ),
                "blocked_filtered_out": _raw_query_with_overrides(
                    raw_scope_pairs,
                    {"sync_status": "PENDING", "filter_decision": "FILTERED_OUT"},
                    remove_keys={"stage", "sync_status", "has_jd", "is_active", "classification_bucket", "filter_decision"},
                ),
                "blocked_inactive": _raw_query_with_overrides(
                    raw_scope_pairs,
                    {"sync_status": "PENDING", "is_active": "0"},
                    remove_keys={"stage", "sync_status", "has_jd", "is_active", "classification_bucket", "filter_decision"},
                ),
                "blocked_low_conf": _raw_query_with_overrides(
                    raw_scope_pairs,
                    {"sync_status": "PENDING", "classification_bucket": "low"},
                    remove_keys={"stage", "sync_status", "has_jd", "is_active", "classification_bucket", "filter_decision"},
                ),
            }
            raw_seed_qs = qs.only(
                "id",
                "company",
                "job_platform",
                "company_name",
                "platform_slug",
                "title",
                "original_url",
                "location_raw",
                "employment_type",
                "experience_level",
                "posted_date",
                "fetched_at",
                "sync_status",
                "is_active",
                "has_description",
                "quality_score",
                "jd_quality_score",
                "classification_confidence",
                "category_confidence",
                "resume_ready_score",
                "raw_payload",
                "country_code",
                "scope_status",
                "scope_reason",
                "sync_skip_reason",
                "external_id",
                "salary_min",
                "salary_max",
                "salary_raw",
                "salary_currency",
                "salary_period",
                "location_type",
                "is_remote",
                "jd_backfill_locked_at",
                "job_domain",
                "filter_decision",
                "filter_reason",
                "is_cold",
                "jd_fetch_skipped",
                "is_test_run",
                "fetch_batch",
                "state",
                "country",
                "word_count",
            )
            raw_paginator = Paginator(raw_seed_qs, 100)
            raw_page = raw_paginator.page(1)
            tab_raw = raw_page.object_list
            for raw_job in tab_raw:
                raw_job.pipeline_row = build_rawjob_pipeline_row(raw_job)
            raw_has_next = raw_page.has_next()
            raw_next_page = raw_page.next_page_number() if raw_page.has_next() else None
            raw_total_filtered = raw_paginator.count

        elif tab == 'pool':
            pool_context = self._build_pool_page_context(request, q, search_by)
            tab_jobs = pool_context['tab_jobs']
            score_tab = pool_context['score_tab']
            lane_tab = pool_context['lane_tab']
            gate_tab = pool_context['gate_tab']
            pool_extra = pool_context['pool_extra']
            pipeline_tab_has_next = pool_context['pipeline_tab_has_next']
            pipeline_tab_next_page = pool_context['pipeline_tab_next_page']
            pipeline_tab_total_filtered = pool_context['pipeline_tab_total_filtered']

        elif tab == 'live':
            live_context = self._build_live_page_context(q)
            tab_jobs = live_context['tab_jobs']
            pipeline_tab_has_next = live_context['pipeline_tab_has_next']
            pipeline_tab_next_page = live_context['pipeline_tab_next_page']
            pipeline_tab_total_filtered = live_context['pipeline_tab_total_filtered']

        elif tab == 'archived':
            archived_context = self._build_archived_page_context(q)
            tab_jobs = archived_context['tab_jobs']
            pipeline_tab_has_next = archived_context['pipeline_tab_has_next']
            pipeline_tab_next_page = archived_context['pipeline_tab_next_page']
            pipeline_tab_total_filtered = archived_context['pipeline_tab_total_filtered']

        active_filter_chips: list[tuple[str, str]] = []
        if q:
            active_filter_chips.append(("Search", q))
        if tab == "raw":
            raw_label_map = {
                "stage": "Stage",
                "sync_status": "Sync",
                "is_active": "Active",
                "has_jd": "JD",
                "classification_bucket": "Conf",
                "resume_jd": "Resume JD",
                "pending_age_bucket": "Pending age",
                "platform": "Platform",
                "country": "Country",
                "country_code": "Country Code",
                "state": "State",
                "marketing_role": "Marketing Role",
                "filter_decision": "Filter",
                "include_test": "Test Rows",
            }
            raw_role_label_by_slug = {
                role.slug: role.name
                for role in MarketingRole.objects.only("slug", "name")
            }
            for key, value in raw_filter_passthrough:
                display_value = value
                if key == "marketing_role":
                    display_value = raw_role_label_by_slug.get(value, value)
                active_filter_chips.append((raw_label_map.get(key, key.replace("_", " ").title()), display_value))
        if tab == "pool":
            if gate_tab != "all":
                active_filter_chips.append(("Gate", gate_tab))
            if lane_tab != "all":
                active_filter_chips.append(("Lane", lane_tab))
            if score_tab != "all":
                active_filter_chips.append(("Score", score_tab))
            pool_filter_label_map = {
                "posted_by": "Posted by",
                "company": "Company",
                "job_type": "Job type",
                "job_source": "Source",
                "date_from": "From",
                "date_to": "To",
                "page_size": "Records",
            }
            poster_label_map = {
                str(user.pk): (user.get_full_name() or user.username)
                for user in User.objects.filter(
                    pk__in=Job.objects.filter(status=Job.Status.POOL, is_archived=False)
                    .values_list("posted_by_id", flat=True)
                    .distinct()
                )
            }
            for key, value in _pool_filter_query_pairs(request):
                if key in {"q", "search_by"}:
                    continue
                display_value = value
                if key == "posted_by":
                    display_value = poster_label_map.get(value, value)
                elif key == "job_type":
                    display_value = dict(Job.JobType.choices).get(value, value)
                elif key == "page_size":
                    display_value = f"{value}/page"
                active_filter_chips.append((pool_filter_label_map.get(key, key.replace("_", " ").title()), display_value))

        ctx = {
            'tab': tab,
            'q': q,
            'search_by': search_by,
            'raw_selected_stage': raw_selected_stage,
            'raw_filter_passthrough': raw_filter_passthrough,
            'raw_stage_links': raw_stage_links,
            'raw_blocker_links': raw_blocker_links,
            'active_filter_chips': active_filter_chips,
            'raw_total': raw_total,
            'raw_test_total': raw_test_total,
            'raw_funnel': raw_funnel,
            'raw_gate_summary': raw_gate_summary,
            'pool_total': pool_total,
            'live_total': live_total,
            'archived_total': archived_total,
            'last_batch': last_batch,
            'tab_jobs': tab_jobs,
            'tab_raw': tab_raw,
            'raw_has_next': raw_has_next if tab == "raw" else False,
            'raw_next_page': raw_next_page if tab == "raw" else None,
            'raw_total_filtered': raw_total_filtered if tab == "raw" else 0,
            'pipeline_tab_has_next': pipeline_tab_has_next if tab in {"pool", "live", "archived"} else False,
            'pipeline_tab_next_page': pipeline_tab_next_page if tab in {"pool", "live", "archived"} else None,
            'pipeline_tab_total_filtered': pipeline_tab_total_filtered if tab in {"pool", "live", "archived"} else 0,
            'vet_gate_summary': vet_gate_summary,
            'raw_country_code_options': (
                raw_base_qs.exclude(country_code="")
                .values_list("country_code", flat=True)
                .distinct()
                .order_by("country_code")
            ),
            'raw_marketing_roles': MarketingRole.objects.filter(is_active=True).order_by("name"),
            'raw_selected_country_code': (request.GET.get("country_code") or "").strip(),
            'raw_selected_marketing_role': (request.GET.get("marketing_role") or "").strip(),
            'raw_selected_include_test': (request.GET.get("include_test") or "").strip(),
        }
        if tab == 'pool':
            ctx.update(pool_extra)

        return render(request, self.template_name, ctx)


# ─── Phase 4: PipelineEvent timeline + health dashboard ─────────────────────

class JobTimelineView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """Per-job lineage: every PipelineEvent for this Job (and its url_hash)."""
    template_name = 'jobs/job_timeline.html'

    def get(self, request, pk):
        from .models import PipelineEvent
        job = get_object_or_404(Job, pk=pk)
        events_qs = PipelineEvent.objects.filter(
            Q(job=job) | (Q(url_hash=job.url_hash) & ~Q(url_hash=''))
        ).order_by('-occurred_at')[:500]
        return render(request, self.template_name, {
            'job': job,
            'events': events_qs,
            'stage_choices': Job.Stage.choices,
        })


class PipelineHealthView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """Ops dashboard: stage counts, recent failures, throughput (last 24h)."""
    template_name = 'jobs/pipeline_health.html'

    def get(self, request):
        from django.db.models import Count
        from django.utils import timezone as _tz
        from datetime import timedelta
        from .models import PipelineEvent

        since = _tz.now() - timedelta(hours=24)

        stage_counts = list(
            Job.objects.filter(is_archived=False)
            .values('stage').annotate(n=Count('id')).order_by('stage')
        )
        stage_order = [s for s, _ in Job.Stage.choices]
        stage_labels_map = dict(Job.Stage.choices)
        stage_counts.sort(key=lambda r: stage_order.index(r['stage']) if r['stage'] in stage_order else 99)
        for r in stage_counts:
            r['label'] = stage_labels_map.get(r['stage'], r['stage'])

        recent_failures = (
            PipelineEvent.objects.filter(status=PipelineEvent.Status.FAILED, occurred_at__gte=since)
            .select_related('job').order_by('-occurred_at')[:50]
        )
        throughput = (
            PipelineEvent.objects.filter(occurred_at__gte=since)
            .values('to_stage').annotate(n=Count('id')).order_by('-n')
        )
        total_events_24h = PipelineEvent.objects.filter(occurred_at__gte=since).count()
        failure_rate = (
            PipelineEvent.objects.filter(occurred_at__gte=since, status=PipelineEvent.Status.FAILED).count()
            / total_events_24h * 100 if total_events_24h else 0
        )
        return render(request, self.template_name, {
            'stage_counts': stage_counts,
            'stage_labels': dict(Job.Stage.choices),
            'recent_failures': recent_failures,
            'throughput': throughput,
            'total_events_24h': total_events_24h,
            'failure_rate': round(failure_rate, 2),
        })


# ── Job Classification Trigger ────────────────────────────────────────────────

class ClassifyJobsTriggerView(LoginRequiredMixin, UserPassesTestMixin, View):
    """One-button trigger for the full raw-job taxonomy backfill."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, "role", None) in ("ADMIN", "EMPLOYEE")

    def post(self, request, *args, **kwargs):
        from django.core.cache import cache
        from .tasks import classify_jobs_task, CLASSIFY_ACTIVE_TASK_KEY, CLASSIFY_LOCK_KEY, _classify_lock_ttl

        action = request.POST.get("action", "")
        force = request.POST.get("force") == "1"

        # ── Force-unlock: staff can clear a ghost lock from the UI ────────────
        if action == "unlock":
            if not request.user.is_superuser:
                return JsonResponse({"status": "forbidden"}, status=403)
            stale_id = cache.get(CLASSIFY_ACTIVE_TASK_KEY) or cache.get(CLASSIFY_LOCK_KEY)
            cache.delete(CLASSIFY_LOCK_KEY)
            cache.delete(CLASSIFY_ACTIVE_TASK_KEY)
            return JsonResponse({
                "status": "unlocked",
                "cleared_task_id": stale_id or "",
                "message": "Classify lock cleared. You can now start a new classify run.",
            })

        active_task_id = cache.get(CLASSIFY_ACTIVE_TASK_KEY) or cache.get(CLASSIFY_LOCK_KEY)
        if active_task_id:
            return JsonResponse({
                "status": "already_running",
                "task_id": active_task_id,
                "message": "Taxonomy backfill is already in progress.",
            })

        result = classify_jobs_task.apply_async(kwargs={"force_reclassify": force})
        cache.set(CLASSIFY_ACTIVE_TASK_KEY, result.id, _classify_lock_ttl())
        return JsonResponse({"status": "started", "task_id": result.id})

    def get(self, request, *args, **kwargs):
        """Poll task progress."""
        from celery.result import AsyncResult
        from django.core.cache import cache
        from .tasks import CLASSIFY_ACTIVE_TASK_KEY, CLASSIFY_LOCK_KEY

        task_id = request.GET.get("task_id")
        if not task_id and request.GET.get("current") == "1":
            task_id = cache.get(CLASSIFY_ACTIVE_TASK_KEY) or cache.get(CLASSIFY_LOCK_KEY)
            if not task_id:
                return JsonResponse({"state": "IDLE", "info": {}})
        if not task_id:
            return JsonResponse({"error": "missing task_id"}, status=400)

        res = AsyncResult(task_id)
        if res.state == "PROGRESS":
            return JsonResponse({"state": "PROGRESS", "task_id": task_id, "info": res.info or {}})
        if res.state == "SUCCESS":
            return JsonResponse({"state": "SUCCESS", "task_id": task_id, "info": res.result or {}})
        if res.state == "FAILURE":
            return JsonResponse({"state": "FAILURE", "task_id": task_id, "info": str(res.result)})
        return JsonResponse({"state": res.state, "task_id": task_id, "info": {}})


# ─── Bulk Actions ───────────────────────────────────────────────────────────────

class JobBulkActionView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """Handle bulk actions on selected jobs from the job list."""

    def post(self, request):
        ids_str = request.POST.get("selected_ids", "")
        action = request.POST.get("bulk_action", "")
        ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()][:500]

        if not ids:
            messages.error(request, "No jobs selected.")
            return redirect("job-list")

        qs = Job.objects.filter(pk__in=ids, is_archived=False)

        if action == "close":
            updated = qs.exclude(status=Job.Status.CLOSED).update(status=Job.Status.CLOSED)
            messages.success(request, f"Closed {updated} job(s).")
        elif action == "open":
            updated = qs.exclude(status=Job.Status.OPEN).update(status=Job.Status.OPEN)
            messages.success(request, f"Reopened {updated} job(s).")
        elif action == "archive":
            updated = qs.update(
                is_archived=True,
                archived_at=timezone.now(),
                archived_by=request.user,
            )
            messages.success(request, f"Archived {updated} job(s).")
        elif action == "draft":
            updated = qs.exclude(status=Job.Status.DRAFT).update(status=Job.Status.DRAFT)
            messages.success(request, f"Set {updated} job(s) to Draft.")
        elif action == "export":
            # Return CSV of selected jobs
            jobs = qs.select_related("posted_by").order_by("-created_at")
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = 'attachment; filename="selected_jobs.csv"'
            writer = csv.writer(response)
            writer.writerow(["ID", "Title", "Company", "Location", "Status", "Stage", "Created"])
            for j in jobs:
                writer.writerow([j.pk, j.title, j.company, j.location, j.status, j.stage, j.created_at.date()])
            return response
        else:
            messages.error(request, f"Unknown action: {action}")

        return redirect("job-list")

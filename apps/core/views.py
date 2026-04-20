from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, UpdateView, View, ListView, DetailView, CreateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.conf import settings
from django.db.models import Count, Q, Sum
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from datetime import timedelta
from django.contrib import messages
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.core.serializers.json import DjangoJSONEncoder
import json

from users.models import User, ConsultantProfile
from jobs.models import Job
from submissions.models import ApplicationSubmission, Placement, Timesheet, Commission
from resumes.models import ResumeDraft
from .models import (
    PlatformConfig,
    LLMConfig,
    LLMUsageLog,
    AuditLog,
    PipelineRunLog,
    Notification,
    BroadcastMessage,
    BroadcastDelivery,
    FeatureFlag,
    EmployeeDesignation,
)
from .forms import PlatformConfigForm, LLMConfigForm, BroadcastForm
from .broadcast_utils import deliver_broadcast
from .notification_utils import invalidate_notification_unread_cache
from .dashboard_metrics import (
    get_submission_funnel_metrics,
    get_consultant_performance_metrics,
    get_time_to_hire_metrics,
    get_employee_leaderboard_metrics,
)
from .monitor import SystemMonitor
from .security import decrypt_value
from .llm_services import list_openai_models, sort_models_by_cost, get_cost_info
from .llm_pricing import PRICING_PER_1M
from .feature_flags import feature_enabled_for, invalidate_feature_flag_cache


class TaskProgressAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Poll Celery task state for the global progress bar (logged-in users).
    GET /core/api/task-progress/<task_id>/
    """

    def test_func(self):
        return self.request.user.is_authenticated

    def get(self, request, task_id: str):
        from celery.result import AsyncResult

        from config.celery import app as celery_app

        r = AsyncResult(task_id, app=celery_app)
        state = r.state

        if state == "PENDING":
            return JsonResponse(
                {
                    "state": "PENDING",
                    "ready": False,
                    "percent": 0,
                    "current": 0,
                    "total": 0,
                    "message": "Queued — waiting for worker",
                }
            )

        if state == "PROGRESS":
            meta = r.info or {}
            if not isinstance(meta, dict):
                meta = {}
            resp = {
                "state": "PROGRESS",
                "ready": False,
                "percent": int(meta.get("percent") or 0),
                "current": int(meta.get("current") or 0),
                "total": int(meta.get("total") or 0),
                "message": meta.get("message") or "",
            }
            if meta.get("detail") and isinstance(meta["detail"], dict):
                resp["detail"] = meta["detail"]
            return JsonResponse(resp)

        if state == "SUCCESS":
            res = r.result
            safe = res if isinstance(res, (dict, list, str, int, float, bool)) or res is None else None
            return JsonResponse(
                {
                    "state": "SUCCESS",
                    "ready": True,
                    "percent": 100,
                    "current": 1,
                    "total": 1,
                    "message": "Done",
                    "result": safe,
                }
            )

        if state == "FAILURE":
            err = r.info
            if err is not None and not isinstance(err, str):
                err = repr(err)
            return JsonResponse(
                {
                    "state": "FAILURE",
                    "ready": True,
                    "percent": 0,
                    "current": 0,
                    "total": 0,
                    "message": (err or "Task failed")[:500],
                }
            )

        return JsonResponse(
            {
                "state": state,
                "ready": False,
                "percent": 0,
                "current": 0,
                "total": 0,
                "message": "Running…",
            }
        )


class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'


class SystemStatusView(AdminRequiredMixin, TemplateView):
    template_name = 'settings/system_status.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        monitor = SystemMonitor()
        context['health_check'] = monitor.check_all()
        return context


class HealthcheckJSONView(View):
    """
    Lightweight JSON health endpoint for uptime checks.

    Always returns HTTP 200 with a JSON payload containing:
    - overall: 'ok' or 'degraded'
    - database: status block
    - pages: list of page status blocks
    """

    def get(self, request, *args, **kwargs):
        monitor = SystemMonitor()
        health = monitor.check_all()

        db_ok = health["database"]["status"] == "Operational"
        pages_ok = all(p["status"] == "Operational" for p in health["pages"])
        overall = "ok" if (db_ok and pages_ok) else "degraded"

        return JsonResponse(
            {
                "overall": overall,
                "database": health["database"],
                "pages": health["pages"],
            }
        )

class PlatformConfigView(AdminRequiredMixin, UpdateView):
    model = PlatformConfig
    form_class = PlatformConfigForm
    template_name = 'settings/platform_config.html'
    success_url = reverse_lazy('platform-config')

    def get_object(self, queryset=None):
        return PlatformConfig.load()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        logs = {"validate_company_links": None, "validate_job_urls": None, "re_enrich_stale": None, "full_re_enrich": None}
        for log in PipelineRunLog.objects.all():
            logs[log.task_name] = log
        context["pipeline_run_logs"] = logs
        # Pool count for the Job Pool settings tab
        try:
            from jobs.models import Job
            context["pool_job_count"] = Job.objects.filter(status=Job.Status.POOL, is_archived=False).count()
        except Exception:
            context["pool_job_count"] = 0
        return context

    def form_valid(self, form):
        messages.success(self.request, "Platform configuration updated successfully.")
        return super().form_valid(form)


class DataPipelineDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Phase 5: Single place to operate and monitor the company data pipeline.
    Tabs: Ingestion, Enrichment status, Duplicate review, URL validation, Pipeline logs.
    """
    template_name = 'settings/data_pipeline.html'

    def test_func(self):
        u = self.request.user
        if not (u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)):
            return False
        return feature_enabled_for(u, 'system_data_enrichment')

    def get_context_data(self, **kwargs):
        from companies.models import Company
        from companies.tasks import enrich_company_task

        context = super().get_context_data(**kwargs)
        config = PlatformConfig.load()

        # Tab 1: Ingestion
        context['auto_enrich_on_create'] = getattr(config, 'auto_enrich_on_create', True)

        # Tab 2: Enrichment status
        now = timezone.now()
        stale_cutoff = now - timezone.timedelta(days=90)
        context['company_total'] = Company.objects.count()
        context['company_pending'] = Company.objects.filter(enrichment_status=Company.EnrichmentStatus.PENDING).count()
        context['company_enriched'] = Company.objects.filter(enrichment_status=Company.EnrichmentStatus.ENRICHED).count()
        context['company_failed'] = Company.objects.filter(enrichment_status=Company.EnrichmentStatus.FAILED).count()
        context['company_stale'] = Company.objects.filter(
            Q(enrichment_status=Company.EnrichmentStatus.ENRICHED, enriched_at__lt=stale_cutoff)
            | Q(enrichment_status=Company.EnrichmentStatus.STALE)
        ).count()
        context['stale_cutoff_days'] = 90

        # Tab 4: URL validation
        logs = {"validate_company_links": None, "validate_job_urls": None, "re_enrich_stale": None, "full_re_enrich": None}
        for log in PipelineRunLog.objects.all():
            logs[log.task_name] = log
        context['pipeline_run_logs'] = logs
        context['invalid_website_count'] = Company.objects.filter(
            website__isnull=False
        ).exclude(website="").filter(website_is_valid=False).count()
        context['possibly_filled_count'] = Job.objects.filter(possibly_filled=True).count()

        # Tab 5: Pipeline logs
        context['pipeline_logs'] = PipelineRunLog.objects.order_by('-last_run_at')[:50]

        return context

    def post(self, request, *args, **kwargs):
        """Handle Re-enrich stale action."""
        from companies.models import Company
        from companies.tasks import enrich_company_task

        now = timezone.now()
        stale_cutoff = now - timezone.timedelta(days=90)
        stale_ids = list(
            Company.objects.filter(
                Q(enrichment_status=Company.EnrichmentStatus.ENRICHED, enriched_at__lt=stale_cutoff)
                | Q(enrichment_status=Company.EnrichmentStatus.STALE)
            ).values_list("pk", flat=True)
        )
        for pk in stale_ids:
            enrich_company_task.delay(pk)
        messages.success(request, f"Re-enrichment queued for {len(stale_ids)} stale companies.")
        return redirect("data-pipeline")


class AuditLogListView(AdminRequiredMixin, ListView):
    model = AuditLog
    template_name = 'settings/audit_log.html'
    context_object_name = 'audit_logs'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('actor')
        action = self.request.GET.get('action')
        target = self.request.GET.get('target_model')
        if action:
            qs = qs.filter(action__icontains=action)
        if target:
            qs = qs.filter(target_model__icontains=target)
        return qs


class LLMConfigView(AdminRequiredMixin, View):
    template_name = 'settings/llm_config.html'

    def _build_model_choices(self, api_key: str):
        models = []
        if api_key:
            try:
                models = list_openai_models(api_key)
            except Exception as exc:
                self._model_error = str(exc)
        if not models:
            models = list(PRICING_PER_1M.keys())
        models = sort_models_by_cost(models)
        choices = []
        for m in models:
            info = get_cost_info(m)
            if info:
                label = f"{m} — ${info['input']}/$ {info['output']} per 1M"
                label = label.replace('$ ', '$')
            else:
                label = f"{m} — cost unknown"
            choices.append((m, label))
        return choices

    def get(self, request):
        config = LLMConfig.load()
        api_key = decrypt_value(config.encrypted_api_key)
        form = LLMConfigForm(instance=config)
        form.fields['active_model'].choices = self._build_model_choices(api_key)

        context = self._build_metrics_context()
        context.update({
            'form': form,
            'api_key_masked': (api_key[:4] + '…' + api_key[-4:]) if api_key else '',
            'model_error': getattr(self, '_model_error', ''),
        })
        return render(request, self.template_name, context)

    def post(self, request):
        config = LLMConfig.load()
        api_key = decrypt_value(config.encrypted_api_key)
        api_key_for_models = request.POST.get('api_key') or api_key
        form = LLMConfigForm(request.POST, instance=config)
        form.fields['active_model'].choices = self._build_model_choices(api_key_for_models)

        action = request.POST.get('action')
        if action == 'test_key':
            test_key = api_key_for_models
            if not test_key:
                messages.error(request, "Please enter an API key to test.")
            else:
                try:
                    _ = list_openai_models(test_key)
                    messages.success(request, "API key is valid. Models fetched successfully.")
                except Exception as exc:
                    messages.error(request, f"API key test failed: {exc}")
            context = self._build_metrics_context()
            context.update({
                'form': form,
                'api_key_masked': (api_key[:4] + '…' + api_key[-4:]) if api_key else '',
            })
            return render(request, self.template_name, context)

        if form.is_valid():
            form.save()
            messages.success(request, "LLM configuration updated successfully.")
            return redirect('llm-config')

        context = self._build_metrics_context()
        context.update({
            'form': form,
            'api_key_masked': (api_key[:4] + '…' + api_key[-4:]) if api_key else '',
        })
        return render(request, self.template_name, context)

    def _build_metrics_context(self):
        now = timezone.now()
        start_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_week = now - timedelta(days=7)
        start_day = now - timedelta(days=1)

        logs = LLMUsageLog.objects.all()
        total_calls = logs.count()
        success_calls = logs.filter(success=True).count()
        failed_calls = logs.filter(success=False).count()
        total_tokens = logs.aggregate(total=Sum('total_tokens'))['total'] or 0
        total_cost = logs.aggregate(total=Sum('cost_total'))['total'] or 0
        total_latency = logs.aggregate(total=Sum('latency_ms'))['total'] or 0
        avg_latency = int(total_latency / total_calls) if total_calls else 0

        return {
            'llm_config': LLMConfig.load(),
            'total_calls': total_calls,
            'success_calls': success_calls,
            'failed_calls': failed_calls,
            'total_tokens': total_tokens,
            'total_cost': total_cost,
            'avg_latency': avg_latency,
            'calls_today': logs.filter(created_at__gte=start_day).count(),
            'calls_week': logs.filter(created_at__gte=start_week).count(),
            'calls_month': logs.filter(created_at__gte=start_month).count(),
            'recent_logs': logs.order_by('-created_at')[:20],
        }


class LLMLogListView(AdminRequiredMixin, ListView):
    model = LLMUsageLog
    template_name = 'settings/llm_logs.html'
    context_object_name = 'logs'
    paginate_by = 25

    def get_queryset(self):
        qs = LLMUsageLog.objects.select_related('job', 'consultant', 'actor').order_by('-created_at')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(job__title__icontains=q) |
                Q(job__company__icontains=q) |
                Q(model_name__icontains=q) |
                Q(consultant__user__username__icontains=q) |
                Q(consultant__user__first_name__icontains=q) |
                Q(consultant__user__last_name__icontains=q)
            )
        return qs


class LLMLogDetailView(AdminRequiredMixin, DetailView):
    model = LLMUsageLog
    template_name = 'settings/llm_log_detail.html'
    context_object_name = 'log'


class HelpCenterView(AdminRequiredMixin, TemplateView):
    """
    Admin-only Help Center that explains how to configure critical services
    like IMAP email ingestion, LLMs, and background workers.
    """

    template_name = 'settings/help.html'


class GlobalSearchView(LoginRequiredMixin, TemplateView):
    """Phase 2: Search across jobs, consultants, companies, and submissions."""
    template_name = 'core/global_search.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .search_utils import build_global_search_context

        q = self.request.GET.get('q', '').strip()
        context.update(build_global_search_context(self.request, q))
        return context


class GlobalSearchPartialView(LoginRequiredMixin, View):
    """HTMX: return search result fragment for nav dropdown."""

    def get(self, request, *args, **kwargs):
        from .search_utils import build_global_search_context

        q = request.GET.get('q', '').strip()
        ctx = build_global_search_context(request, q)
        return render(request, 'core/global_search_partial.html', ctx)


def home(request):
    """Smart redirect: send each role to their own dashboard."""
    if not request.user.is_authenticated:
        return render(request, 'home.html')

    role = request.user.role
    if request.user.is_superuser or role == 'ADMIN':
        return redirect('admin-dashboard')
    elif role == 'EMPLOYEE':
        return redirect('employee-dashboard')
    elif role == 'CONSULTANT':
        return redirect('consultant-dashboard')
    return render(request, 'home.html')


# ─── Phase 6: Master Prompt Editor ────────────────────────────────────

class MasterPromptListView(AdminRequiredMixin, ListView):
    """List all master prompt versions."""
    template_name = 'settings/master_prompt_list.html'
    context_object_name = 'prompts'

    def get_queryset(self):
        from resumes.models import MasterPrompt
        return MasterPrompt.objects.all().order_by('-updated_at')


class MasterPromptCreateView(AdminRequiredMixin, View):
    """Create a new master prompt version."""

    def get(self, request):
        from resumes.models import MasterPrompt
        return render(request, 'settings/master_prompt_form.html', {
            'prompt': None,
            'form_action': reverse('master-prompt-create'),
        })

    def post(self, request):
        from resumes.engine import INPUT_SECTION_KEYS
        from resumes.models import MasterPrompt

        sections = {
            k: request.POST.get(f'default_input_{k}') == 'on' for k in INPUT_SECTION_KEYS
        }
        sections['personal'] = True
        mp = MasterPrompt(
            name=request.POST.get('name', 'Untitled'),
            system_prompt=request.POST.get('system_prompt', ''),
            generation_rules=request.POST.get('generation_rules', ''),
            is_active=request.POST.get('is_active') == 'on',
            created_by=request.user,
            default_input_sections=sections,
        )
        mp.save()
        messages.success(request, f"Master prompt '{mp.name}' created.")
        return redirect('master-prompt-list')


class MasterPromptEditView(AdminRequiredMixin, View):
    """Edit an existing master prompt."""

    def get(self, request, pk):
        from resumes.models import MasterPrompt
        mp = get_object_or_404(MasterPrompt, pk=pk)
        return render(request, 'settings/master_prompt_form.html', {
            'prompt': mp,
            'form_action': reverse('master-prompt-edit', args=[pk]),
        })

    def post(self, request, pk):
        from resumes.engine import INPUT_SECTION_KEYS
        from resumes.models import MasterPrompt

        mp = get_object_or_404(MasterPrompt, pk=pk)
        mp.name = request.POST.get('name', mp.name)
        mp.system_prompt = request.POST.get('system_prompt', '')
        mp.generation_rules = request.POST.get('generation_rules', '')
        mp.is_active = request.POST.get('is_active') == 'on'
        sections = {
            k: request.POST.get(f'default_input_{k}') == 'on' for k in INPUT_SECTION_KEYS
        }
        sections['personal'] = True
        mp.default_input_sections = sections
        mp.save()
        messages.success(request, f"Master prompt '{mp.name}' updated.")
        return redirect('master-prompt-list')


class MasterPromptActivateView(AdminRequiredMixin, View):
    """Activate a master prompt (deactivates all others)."""

    def post(self, request, pk):
        from resumes.models import MasterPrompt
        mp = get_object_or_404(MasterPrompt, pk=pk)
        mp.is_active = True
        mp.save()
        messages.success(request, f"'{mp.name}' is now the active master prompt.")
        return redirect('master-prompt-list')


class AdminDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'core/admin_dashboard.html'

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Security warnings for admin
        warnings = []
        if not settings.LLM_ENCRYPTION_KEY:
            warnings.append(
                "LLM_ENCRYPTION_KEY is not set in .env. API keys and IMAP passwords "
                "are encrypted with a key derived from SECRET_KEY, which is less secure. "
                "Set a dedicated Fernet key for production."
            )
        llm_config = LLMConfig.load()
        if not decrypt_value(llm_config.encrypted_api_key):
            warnings.append(
                "No OpenAI API key is configured. Resume generation will not work. "
                "Go to Settings \u2192 LLM Config to set one."
            )
        context['admin_warnings'] = warnings

        # Top-level KPIs
        context['total_jobs'] = Job.objects.count()
        context['active_jobs'] = Job.objects.filter(status=Job.Status.OPEN).count()
        context['total_consultants'] = User.objects.filter(role=User.Role.CONSULTANT).count()
        context['total_employees'] = User.objects.filter(role=User.Role.EMPLOYEE).count()
        context['total_applications'] = ApplicationSubmission.objects.count()
        context['pending_applications_count'] = ApplicationSubmission.objects.filter(
            status=ApplicationSubmission.Status.APPLIED
        ).count()

        # Phase 1: Placement & Revenue KPIs
        context['total_placements'] = Placement.objects.count()
        context['active_placements'] = Placement.objects.filter(
            status=Placement.PlacementStatus.ACTIVE
        ).count()
        context['placed_count'] = ApplicationSubmission.objects.filter(
            status=ApplicationSubmission.Status.PLACED
        ).count()
        pending_timesheets = Timesheet.objects.filter(
            status=Timesheet.TimesheetStatus.SUBMITTED
        ).count()
        context['pending_timesheets'] = pending_timesheets
        pending_commissions_amount = Commission.objects.filter(
            status=Commission.CommissionStatus.PENDING
        ).aggregate(total=Sum('commission_amount'))['total'] or 0
        context['pending_commissions_amount'] = pending_commissions_amount

        # Recent activity (Overview tab)
        context['recent_jobs'] = Job.objects.select_related('posted_by').order_by('-created_at')[:5]
        context['recent_applications'] = ApplicationSubmission.objects.select_related(
            'job', 'consultant__user'
        ).order_by('-created_at')[:5]

        # Employee lens: per-employee micro stats (jobs posted, open, apps received, pending)
        employees = User.objects.filter(role=User.Role.EMPLOYEE).select_related(
            'employee_profile', 'employee_profile__department'
        ).annotate(
            jobs_posted=Count('posted_jobs'),
            open_jobs=Count('posted_jobs', filter=Q(posted_jobs__status=Job.Status.OPEN)),
        ).order_by('first_name', 'last_name')
        app_counts = ApplicationSubmission.objects.filter(
            job__posted_by__role=User.Role.EMPLOYEE
        ).values('job__posted_by').annotate(
            apps_received=Count('id'),
            pending=Count('id', filter=Q(status=ApplicationSubmission.Status.APPLIED)),
        )
        app_by_employee = {r['job__posted_by']: r for r in app_counts}
        context['employee_stats'] = [
            {
                'user': e,
                'jobs_posted': e.jobs_posted,
                'open_jobs': e.open_jobs,
                'apps_received': app_by_employee.get(e.pk, {}).get('apps_received', 0),
                'pending': app_by_employee.get(e.pk, {}).get('pending', 0),
            }
            for e in employees
        ]

        # Consultant lens: per-consultant micro stats (applications by stage)
        consultants = ConsultantProfile.objects.select_related('user').annotate(
            total_apps=Count('submissions'),
            in_progress=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.IN_PROGRESS)),
            applied=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.APPLIED)),
            interview=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.INTERVIEW)),
            offer=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.OFFER)),
            rejected=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.REJECTED)),
        ).order_by('user__first_name', 'user__last_name')
        context['consultant_stats'] = list(consultants)

        # --- Analytics That Matter (admin-only) ---
        context.update(get_submission_funnel_metrics())
        context.update(get_consultant_performance_metrics())
        context.update(get_time_to_hire_metrics())
        context.update(get_employee_leaderboard_metrics())
        context.update(self._get_market_intelligence_data())
        context.update(self._get_consultant_roi_data())
        context.update(self._get_submission_quality_data())

        return context

    def _get_market_intelligence_data(self):
        """Market Intelligence: skills/roles in demand, top companies, salary by role."""
        from users.models import MarketingRole
        jobs = Job.objects.filter(status=Job.Status.OPEN).prefetch_related('marketing_roles')
        role_counts = {}
        for job in jobs:
            for role in job.marketing_roles.all():
                role_counts[role.name] = role_counts.get(role.name, 0) + 1
        top_roles = sorted(
            [{'name': k, 'job_count': v} for k, v in role_counts.items()],
            key=lambda x: -x['job_count'],
        )[:15]
        company_counts = list(
            Job.objects.filter(status=Job.Status.OPEN)
            .values('company')
            .annotate(job_count=Count('id'))
            .order_by('-job_count')[:15]
        )
        salary_by_role = {}
        for job in Job.objects.filter(status=Job.Status.OPEN).prefetch_related('marketing_roles'):
            for role in job.marketing_roles.all():
                name = role.name
                if name not in salary_by_role:
                    salary_by_role[name] = []
                if job.salary_range and job.salary_range.strip():
                    salary_by_role[name].append(job.salary_range.strip())
        for k in salary_by_role:
            salary_by_role[k] = list(dict.fromkeys(salary_by_role[k]))[:5]
        job_type_qs = Job.objects.filter(status=Job.Status.OPEN).values('job_type').annotate(c=Count('id'))
        job_type_labels = dict(Job.JobType.choices)
        job_type_counts = [
            (row['job_type'], job_type_labels.get(row['job_type'], row['job_type']), row['c']) for row in job_type_qs
        ]
        return {
            'market_top_roles': top_roles,
            'market_top_companies': company_counts,
            'market_salary_by_role': salary_by_role,
            'market_job_type_breakdown': job_type_counts,
        }

    def _get_consultant_roi_data(self):
        """Consultant ROI Score: submissions, interviews, placements, revenue proxy."""
        AS = ApplicationSubmission
        consultants = ConsultantProfile.objects.select_related('user').annotate(
            total_sub=Count('submissions'),
            interview_count=Count(
                'submissions',
                filter=Q(submissions__status__in=[AS.Status.INTERVIEW, AS.Status.OFFER]),
            ),
            placements=Count('submissions', filter=Q(submissions__status=AS.Status.OFFER)),
            rejected_count=Count('submissions', filter=Q(submissions__status=AS.Status.REJECTED)),
        )
        roi_list = []
        for c in consultants:
            revenue_proxy = None
            if c.placements and c.hourly_rate:
                try:
                    revenue_proxy = float(c.placements) * float(c.hourly_rate) * 40
                except (TypeError, ValueError):
                    pass
            total = c.total_sub
            place = c.placements or 0
            intr = c.interview_count or 0
            interview_rate = (intr / total * 100) if total else 0
            score = min(place * 25, 50)
            score += min(interview_rate * 0.3, 25)
            score += min(total * 0.5, 25)
            roi_score = min(100, round(score))
            roi_list.append(
                {
                    'consultant': c,
                    'total_submissions': total,
                    'interviews': intr,
                    'placements': place,
                    'rejections': c.rejected_count,
                    'revenue_generated': round(revenue_proxy, 0) if revenue_proxy is not None else None,
                    'roi_score': roi_score,
                }
            )
        roi_list.sort(key=lambda x: (-x['roi_score'], -x['placements'], -x['total_submissions']))
        return {'consultant_roi': roi_list}

    def _get_submission_quality_data(self):
        """
        Submission Quality Score per employee:
        quality = interviews / submissions * 100 (INTERVIEW or OFFER).
        """
        AS = ApplicationSubmission
        employees = User.objects.filter(role=User.Role.EMPLOYEE, is_active=True)
        by_id = {u.pk: u for u in employees}

        agg = (
            AS.objects.filter(submitted_by__in=employees)
            .values('submitted_by')
            .annotate(
                submissions=Count('id'),
                interviews=Count('id', filter=Q(status__in=[AS.Status.INTERVIEW, AS.Status.OFFER])),
            )
        )
        rows = []
        for row in agg:
            user = by_id.get(row['submitted_by'])
            if not user:
                continue
            subs = row['submissions'] or 0
            intr = row['interviews'] or 0
            quality = round((intr / subs) * 100) if subs else None
            rows.append(
                {
                    'user': user,
                    'submissions': subs,
                    'interviews': intr,
                    'quality_pct': quality,
                }
            )
        rows.sort(key=lambda r: (r['quality_pct'] is None, -(r['quality_pct'] or 0)))
        return {'submission_quality': rows}


class WarRoomDashboardView(AdminRequiredMixin, TemplateView):
    """
    Real-time style 'War Room' overview for admins during hiring pushes.
    Focused on today's activity plus currently active submissions.
    """
    template_name = 'core/war_room.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Active submissions (not terminal)
        active_statuses = [
            ApplicationSubmission.Status.IN_PROGRESS,
            ApplicationSubmission.Status.APPLIED,
            ApplicationSubmission.Status.INTERVIEW,
            ApplicationSubmission.Status.OFFER,
        ]
        active_subs = (
            ApplicationSubmission.objects.select_related(
                'job', 'consultant__user', 'job__posted_by'
            )
            .filter(status__in=active_statuses)
            .order_by('-updated_at')[:50]
        )

        # Final rounds: interview / offer
        final_rounds = (
            ApplicationSubmission.objects.select_related(
                'job', 'consultant__user', 'job__posted_by'
            )
            .filter(status__in=[ApplicationSubmission.Status.INTERVIEW, ApplicationSubmission.Status.OFFER])
            .order_by('-updated_at')[:50]
        )

        # Employees most active today (jobs + submissions touching today)
        employees = (
            User.objects.filter(role=User.Role.EMPLOYEE)
            .annotate(
                submissions_today=Count(
                    'submitted_applications',
                    filter=Q(submitted_applications__updated_at__gte=start_day),
                ),
                jobs_today=Count(
                    'posted_jobs',
                    filter=Q(posted_jobs__created_at__gte=start_day),
                ),
            )
        )
        employee_activity = []
        for e in employees:
            total_actions = (e.submissions_today or 0) + (e.jobs_today or 0)
            if total_actions == 0:
                continue
            employee_activity.append(
                {
                    'user': e,
                    'submissions_today': e.submissions_today or 0,
                    'jobs_today': e.jobs_today or 0,
                    'total_actions': total_actions,
                }
            )
        employee_activity.sort(key=lambda x: -x['total_actions'])

        # Alerts: stale applications needing attention
        three_days_ago = now - timedelta(days=3)
        two_days_ago = now - timedelta(days=2)
        stale_applied = (
            ApplicationSubmission.objects.select_related('job', 'consultant__user', 'job__posted_by')
            .filter(
                status=ApplicationSubmission.Status.APPLIED,
                updated_at__lte=three_days_ago,
            )
            .order_by('updated_at')[:25]
        )
        stale_in_progress = (
            ApplicationSubmission.objects.select_related('job', 'consultant__user', 'job__posted_by')
            .filter(
                status=ApplicationSubmission.Status.IN_PROGRESS,
                updated_at__lte=two_days_ago,
            )
            .order_by('updated_at')[:25]
        )
        alerts = {
            'stale_applied': stale_applied,
            'stale_in_progress': stale_in_progress,
        }

        context.update(
            {
                'now': now,
                'start_day': start_day,
                'active_submissions': active_subs,
                'final_round_submissions': final_rounds,
                'employee_activity_today': employee_activity,
                'alerts': alerts,
            }
        )
        return context


class EmployeeDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = 'core/employee_dashboard.html'

    def test_func(self):
        u = self.request.user
        return u.role == User.Role.EMPLOYEE or u.is_superuser or u.role == 'ADMIN'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        my_jobs = Job.objects.filter(posted_by=user)
        context['my_jobs_count'] = my_jobs.count()
        context['my_open_jobs'] = my_jobs.filter(status='OPEN').count()
        my_job_ids = my_jobs.values_list('id', flat=True)
        apps_for_my_jobs = ApplicationSubmission.objects.filter(job_id__in=my_job_ids)
        context['total_apps_received'] = apps_for_my_jobs.count()
        context['pending_apps'] = apps_for_my_jobs.filter(status='APPLIED').count()
        context['recent_my_jobs'] = my_jobs.order_by('-created_at')[:5]
        context['recent_apps'] = apps_for_my_jobs.select_related('job', 'consultant').order_by('-created_at')[:5]
        context['all_open_jobs'] = Job.objects.filter(status='OPEN').count()

        # Per-employee job status breakdown
        my_job_status_qs = my_jobs.values('status').annotate(count=Count('status'))
        job_status_map = dict(Job.Status.choices)
        context['my_job_status_labels'] = json.dumps(
            [job_status_map.get(row['status'], row['status']) for row in my_job_status_qs],
            cls=DjangoJSONEncoder,
        )
        context['my_job_status_data'] = json.dumps(
            [row['count'] for row in my_job_status_qs],
            cls=DjangoJSONEncoder,
        )

        # Per-employee application status breakdown
        my_app_status_qs = apps_for_my_jobs.values('status').annotate(count=Count('status'))
        app_status_map = dict(ApplicationSubmission.Status.choices)
        context['my_app_status_labels'] = json.dumps(
            [app_status_map.get(row['status'], row['status']) for row in my_app_status_qs],
            cls=DjangoJSONEncoder,
        )
        context['my_app_status_data'] = json.dumps(
            [row['count'] for row in my_app_status_qs],
            cls=DjangoJSONEncoder,
        )
        return context


class NotificationListView(LoginRequiredMixin, ListView):
    model = Notification
    template_name = 'core/notification_list.html'
    context_object_name = 'notifications'
    paginate_by = 30

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user)
        kind = (self.request.GET.get('kind') or '').strip()
        if kind in {k for k, _ in Notification.Kind.choices}:
            qs = qs.filter(kind=kind)
        if self.request.GET.get('unread') == '1':
            qs = qs.filter(read_at__isnull=True)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        q = self.request.GET.copy()
        if 'page' in q:
            del q['page']
        context['filter_query'] = q.urlencode()
        context['kind_choices'] = Notification.Kind.choices
        context['active_kind'] = (self.request.GET.get('kind') or '').strip()
        context['unread_only'] = self.request.GET.get('unread') == '1'
        context['unread_total'] = Notification.objects.filter(
            user=self.request.user, read_at__isnull=True
        ).count()
        return context


class NotificationMarkReadView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        n = get_object_or_404(Notification, pk=pk, user=request.user)
        if not n.read_at:
            n.read_at = timezone.now()
            n.save(update_fields=['read_at'])
            invalidate_notification_unread_cache(request.user.pk)
        next_url = request.POST.get('next') or reverse('notification-list')
        if next_url and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(next_url)
        return redirect('notification-list')


class NotificationMarkAllReadView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        updated = Notification.objects.filter(user=request.user, read_at__isnull=True).update(
            read_at=timezone.now()
        )
        if updated:
            invalidate_notification_unread_cache(request.user.pk)
        messages.success(request, 'All notifications marked as read.')
        return redirect('notification-list')


class BroadcastListView(AdminRequiredMixin, ListView):
    model = BroadcastMessage
    template_name = 'core/broadcast_list.html'
    context_object_name = 'broadcasts'
    paginate_by = 20


class BroadcastCreateView(AdminRequiredMixin, CreateView):
    model = BroadcastMessage
    form_class = BroadcastForm
    template_name = 'core/broadcast_form.html'

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        self.object = form.save()
        try:
            stats = deliver_broadcast(self.object)
        except Exception as exc:
            messages.error(self.request, f"Broadcast saved but delivery failed: {exc}")
            return redirect('broadcast-detail', pk=self.object.pk)
        messages.success(
            self.request,
            f"Broadcast sent. Delivered: {stats['delivered']}, skipped (in-app off): {stats['skipped_inapp']}.",
        )
        return redirect('broadcast-detail', pk=self.object.pk)


class BroadcastDetailView(AdminRequiredMixin, DetailView):
    model = BroadcastMessage
    template_name = 'core/broadcast_detail.html'
    context_object_name = 'broadcast'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = self.object.deliveries.select_related('user', 'notification').order_by('-created_at')
        context['deliveries'] = qs[:500]
        context['delivery_counts'] = {
            'total': self.object.deliveries.count(),
            'delivered': self.object.deliveries.filter(status=BroadcastDelivery.Status.DELIVERED).count(),
            'skipped': self.object.deliveries.filter(status=BroadcastDelivery.Status.SKIPPED_INAPP).count(),
        }
        return context


class FeatureControlCenterView(AdminRequiredMixin, TemplateView):
    """Superuser / Admin: manage feature flags and designation RBAC."""

    template_name = 'settings/feature_control.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tab = self.request.GET.get('tab') or 'consultant'
        if tab not in ('consultant', 'employee', 'ai', 'system', 'designations'):
            tab = 'consultant'
        context['active_tab'] = tab
        context['consultant_flags'] = FeatureFlag.objects.filter(category=FeatureFlag.Category.CONSULTANT)
        context['employee_flags'] = FeatureFlag.objects.filter(category=FeatureFlag.Category.EMPLOYEE)
        context['ai_flags'] = FeatureFlag.objects.filter(category=FeatureFlag.Category.AI)
        context['system_flags'] = FeatureFlag.objects.filter(category=FeatureFlag.Category.SYSTEM)
        des_qs = EmployeeDesignation.objects.prefetch_related('allowed_features').order_by('level', 'name')
        context['designations'] = des_qs
        context['matrix_flags'] = FeatureFlag.objects.filter(
            category__in=(FeatureFlag.Category.EMPLOYEE, FeatureFlag.Category.AI)
        ).order_by('sort_order', 'key')
        context['designation_matrix'] = {
            d.pk: set(d.allowed_features.values_list('pk', flat=True)) for d in des_qs
        }
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
        next_tab = request.POST.get('next_tab') or 'consultant'
        if action == 'update_flag':
            pk = request.POST.get('pk')
            field = request.POST.get('field')
            value = request.POST.get('value') == 'on'
            allowed = {'is_enabled', 'enabled_for_consultants', 'enabled_for_employees'}
            if pk and field in allowed:
                flag = get_object_or_404(FeatureFlag, pk=pk)
                setattr(flag, field, value)
                flag.updated_by = request.user
                flag.save(update_fields=[field, 'updated_by', 'updated_at'])
                AuditLog.objects.create(
                    actor=request.user,
                    action='feature_flag_update',
                    target_model='FeatureFlag',
                    target_id=str(flag.pk),
                    details={'key': flag.key, 'field': field, 'value': value},
                )
                invalidate_feature_flag_cache()
                messages.success(request, f'Updated {flag.key}.')
        elif action == 'designation_matrix':
            des_pk = request.POST.get('designation_pk')
            flag_pk = request.POST.get('flag_pk')
            checked = request.POST.get('checked') == '1'
            des = get_object_or_404(EmployeeDesignation, pk=des_pk)
            ff = get_object_or_404(FeatureFlag, pk=flag_pk)
            if checked:
                des.allowed_features.add(ff)
            else:
                des.allowed_features.remove(ff)
            invalidate_feature_flag_cache()
            messages.success(request, 'Designation access updated.')
        return redirect(f"{reverse('feature-control-center')}?tab={next_tab}")


class MyFeaturesJsonView(LoginRequiredMixin, View):
    """Return enabled feature keys for the current user (mobile / extensions)."""

    def get(self, request, *args, **kwargs):
        keys = FeatureFlag.objects.values_list('key', flat=True)
        data = {k: feature_enabled_for(request.user, k) for k in keys}
        return JsonResponse(data)


# ─────────────────────────────────────────────────────────────────────────────
# TASK SCHEDULER GUI
# Full GUI to view, toggle, edit, and manually trigger all periodic tasks.
# Changes take effect immediately — Celery Beat polls the DB every ~5 seconds.
# ─────────────────────────────────────────────────────────────────────────────

TASK_CATEGORY_META = {
    "email":       {"label": "Email",        "icon": "✉️",  "color": "blue"},
    "submissions": {"label": "Submissions",  "icon": "📋",  "color": "purple"},
    "jobs":        {"label": "Jobs",         "icon": "💼",  "color": "yellow"},
    "companies":   {"label": "Companies",    "icon": "🏢",  "color": "teal"},
    "reports":     {"label": "Reports",      "icon": "📊",  "color": "indigo"},
    "harvest":     {"label": "Harvest",      "icon": "🌾",  "color": "green"},
}

TASK_NAME_TO_CATEGORY = {
    "Email Ingest — IMAP poll":                  "email",
    "Follow-up Reminders — send":                "submissions",
    "Stale Submissions — detect":                "submissions",
    "Job URLs — validate":                       "jobs",
    "Stale Jobs — auto-close":                   "jobs",
    "Company Links — validate":                  "companies",
    "Companies — re-enrich stale":               "companies",
    "Digest — weekly consultant pipeline":        "reports",
    "Report — weekly executive summary":         "reports",
    "Harvest — backfill labels from job URLs":    "harvest",
    "Harvest — detect company platforms":        "harvest",
    "Harvest — fetch new jobs":                  "harvest",
    "Harvest — sync to job pool":                "harvest",
    "Harvest — cleanup expired jobs":            "harvest",
}


def _get_schedule_label(task):
    """Return a human-readable schedule string for a PeriodicTask."""
    if task.crontab:
        c = task.crontab
        m, h, dow, dom, moy = c.minute, c.hour, c.day_of_week, c.day_of_month, c.month_of_year
        if m.startswith("*/"):
            return f"Every {m[2:]} min"
        if h.startswith("*/"):
            return f"Every {h[2:]} hours"
        day_map = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat"}
        if dow != "*":
            return f"{day_map.get(dow, dow)} {h.zfill(2)}:{m.zfill(2)} UTC"
        return f"Daily {h.zfill(2)}:{m.zfill(2)} UTC"
    if task.interval:
        return f"Every {task.interval}"
    return "—"


class TaskSchedulerView(AdminRequiredMixin, TemplateView):
    template_name = "settings/task_scheduler.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django_celery_beat.models import PeriodicTask

        tasks = PeriodicTask.objects.select_related("crontab", "interval").order_by("name")

        # Annotate with category + schedule label
        enriched = []
        for t in tasks:
            cat_key = TASK_NAME_TO_CATEGORY.get(t.name, "other")
            cat = TASK_CATEGORY_META.get(cat_key, {"label": "Other", "icon": "⚙️", "color": "gray"})
            enriched.append({
                "obj": t,
                "category_key": cat_key,
                "category_label": cat["label"],
                "category_icon": cat["icon"],
                "category_color": cat["color"],
                "schedule_label": _get_schedule_label(t),
                "kwargs_pretty": t.kwargs if t.kwargs and t.kwargs != "{}" else "—",
            })

        # Group by category
        from collections import defaultdict
        groups = defaultdict(list)
        for item in enriched:
            groups[item["category_key"]].append(item)

        ordered_groups = []
        for key in ["email", "submissions", "jobs", "companies", "reports", "harvest"]:
            if key in groups:
                meta = TASK_CATEGORY_META[key]
                ordered_groups.append({
                    "key": key,
                    "label": meta["label"],
                    "icon": meta["icon"],
                    "color": meta["color"],
                    "tasks": groups[key],
                })

        context["task_groups"] = ordered_groups
        context["total_tasks"] = tasks.count()
        context["active_tasks"] = tasks.filter(enabled=True).count()
        context["paused_tasks"] = tasks.filter(enabled=False).count()
        return context


class TaskToggleView(AdminRequiredMixin, View):
    """POST → toggle a periodic task on/off. Returns immediately (Beat re-reads within ~5s)."""

    def post(self, request, pk, *args, **kwargs):
        from django_celery_beat.models import PeriodicTask
        task = get_object_or_404(PeriodicTask, pk=pk)
        task.enabled = not task.enabled
        task.save(update_fields=["enabled"])
        status = "enabled" if task.enabled else "paused"
        messages.success(request, f"\u2705 Task '{task.name}' is now {status}.")
        return redirect("task-scheduler")


class TaskEditScheduleView(AdminRequiredMixin, View):
    """POST → update the crontab schedule for a periodic task."""

    def post(self, request, pk, *args, **kwargs):
        from django_celery_beat.models import PeriodicTask, CrontabSchedule
        task = get_object_or_404(PeriodicTask, pk=pk)

        minute       = request.POST.get("minute", "0").strip() or "0"
        hour         = request.POST.get("hour", "*").strip() or "*"
        day_of_week  = request.POST.get("day_of_week", "*").strip() or "*"
        day_of_month = request.POST.get("day_of_month", "*").strip() or "*"
        month_of_year = request.POST.get("month_of_year", "*").strip() or "*"

        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=minute,
            hour=hour,
            day_of_week=day_of_week,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
        )
        task.crontab = crontab
        task.interval = None
        task.save(update_fields=["crontab", "interval"])
        messages.success(request, f"\u2705 Schedule updated for '{task.name}'.")
        return redirect("task-scheduler")


class TaskRunNowView(AdminRequiredMixin, View):
    """POST → trigger the Celery task immediately (one-off, off-schedule)."""

    TASK_MAP = {
        "core.tasks.poll_email_ingest_task":                   ("core.tasks", "poll_email_ingest_task"),
        "submissions.tasks.send_followup_reminders":            ("submissions.tasks", "send_followup_reminders"),
        "submissions.tasks.detect_stale_submissions":           ("submissions.tasks", "detect_stale_submissions"),
        "jobs.tasks.validate_job_urls_task":                    ("jobs.tasks", "validate_job_urls_task"),
        "jobs.tasks.auto_close_jobs_task":                      ("jobs.tasks", "auto_close_jobs_task"),
        "companies.tasks.validate_company_links_task":          ("companies.tasks", "validate_company_links_task"),
        "companies.tasks.re_enrich_stale_companies_task":       ("companies.tasks", "re_enrich_stale_companies_task"),
        "core.tasks.send_weekly_consultant_pipeline_digest_task": ("core.tasks", "send_weekly_consultant_pipeline_digest_task"),
        "core.tasks.send_weekly_executive_report_task":         ("core.tasks", "send_weekly_executive_report_task"),
        "harvest.backfill_platform_labels_from_jobs":            ("harvest.tasks", "backfill_platform_labels_from_jobs_task"),
        "harvest.detect_company_platforms":                     ("harvest.tasks", "detect_company_platforms_task"),
        "harvest.harvest_jobs":                                 ("harvest.tasks", "harvest_jobs_task"),
        "harvest.sync_harvested_to_pool":                       ("harvest.tasks", "sync_harvested_to_pool_task"),
        "harvest.cleanup_harvested_jobs":                       ("harvest.tasks", "cleanup_harvested_jobs_task"),
    }

    def post(self, request, pk, *args, **kwargs):
        from django_celery_beat.models import PeriodicTask
        import importlib
        task = get_object_or_404(PeriodicTask, pk=pk)

        mapping = self.TASK_MAP.get(task.task)
        if not mapping:
            messages.error(request, f"⚠️ No run-now mapping for task: {task.task}")
            return redirect("task-scheduler")

        module_path, func_name = mapping
        try:
            module = importlib.import_module(module_path)
            celery_task = getattr(module, func_name)
            kwargs_dict = json.loads(task.kwargs) if task.kwargs and task.kwargs != "{}" else {}
            result = celery_task.delay(**kwargs_dict)
            messages.success(request, f"\U0001f680 Task '{task.name}' triggered! ID: {result.id[:8]}...")
            from urllib.parse import urlencode

            q = urlencode({"tp": result.id, "tpl": (task.name or "Scheduled task")[:120]})
            return redirect(f"{reverse('task-scheduler')}?{q}")
        except Exception as e:
            messages.error(request, f"❌ Failed to trigger task: {e}")

        return redirect("task-scheduler")

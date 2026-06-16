from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, UpdateView, View, ListView, DetailView, CreateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, Max, Q, Sum
from django.db.models.functions import TruncHour
from django.db.utils import OperationalError, ProgrammingError
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
    ErrorLog,
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
from .theme_catalog import get_theme_catalog, get_theme_definition, get_theme_groups


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
        form = context.get("form")
        selected_theme_slug = (
            form.data.get("color_theme")
            if form is not None and getattr(form, "is_bound", False)
            else getattr(self.object, "color_theme", PlatformConfig.ColorTheme.INDIGO)
        )
        context["theme_groups"] = get_theme_groups()
        context["theme_catalog"] = get_theme_catalog()
        context["selected_theme_preview"] = get_theme_definition(selected_theme_slug)
        context["selected_nav_layout"] = (
            (form.data.get("nav_layout") if form is not None and getattr(form, "is_bound", False) else None)
            or getattr(self.object, "nav_layout", PlatformConfig.NavLayout.TOP)
        )
        context["active_settings_tab"] = (
            (form.data.get("active_tab") if form is not None and getattr(form, "is_bound", False) else None)
            or "tab-general"
        )
        context["logo_preview_url"] = (
            (form.data.get("logo_url") if form is not None and getattr(form, "is_bound", False) else None)
            or getattr(self.object, "logo_url", "")
        )
        # Pool count for the Job Pool settings tab
        try:
            from jobs.models import Job
            context["pool_job_count"] = Job.objects.filter(status=Job.Status.POOL, is_archived=False).count()
        except Exception:
            context["pool_job_count"] = 0
        try:
            from jobs.models import RawJobClassificationSnapshot

            context["dual_classification_review_count"] = RawJobClassificationSnapshot.objects.filter(needs_review=True).count()
            context["dual_classification_approved_not_pushed_count"] = (
                RawJobClassificationSnapshot.objects.exclude(approved_output={}).filter(pushed_to_vetting_at__isnull=True).count()
            )
        except Exception:
            context["dual_classification_review_count"] = 0
            context["dual_classification_approved_not_pushed_count"] = 0
        return context

    def get_success_url(self):
        active_tab = (self.request.POST.get("active_tab") or "").strip().lstrip("#")
        base_url = reverse("platform-config")
        return f"{base_url}#{active_tab}" if active_tab else base_url

    def form_valid(self, form):
        messages.success(self.request, "Platform configuration updated successfully.")
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, "Platform configuration was not saved. Fix the highlighted fields and try again.")
        return super().form_invalid(form)


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
        p = self.request.GET
        if p.get('action'):
            qs = qs.filter(action__icontains=p['action'].strip())
        if p.get('target_model'):
            qs = qs.filter(target_model__icontains=p['target_model'].strip())
        if p.get('event_code'):
            qs = qs.filter(event_code__icontains=p['event_code'].strip())
        if p.get('outcome'):
            qs = qs.filter(outcome=p['outcome'].strip())
        if p.get('correlation_id'):
            qs = qs.filter(correlation_id=p['correlation_id'].strip())
        actor_q = p.get('actor', '').strip()
        if actor_q:
            if actor_q.isdigit():
                qs = qs.filter(actor_id=int(actor_q))
            else:
                qs = qs.filter(
                    Q(actor__username__icontains=actor_q)
                    | Q(actor__email__icontains=actor_q)
                )
        from django.utils.dateparse import parse_date

        df = p.get('date_from')
        dt_to = p.get('date_to')
        if df:
            d = parse_date(df)
            if d:
                qs = qs.filter(timestamp__date__gte=d)
        if dt_to:
            d = parse_date(dt_to)
            if d:
                qs = qs.filter(timestamp__date__lte=d)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['outcome_choices'] = AuditLog.Outcome.choices
        q = self.request.GET.copy()
        q.pop('page', None)
        ctx['filter_querystring'] = q.urlencode()
        return ctx


class AuditLogDetailView(AdminRequiredMixin, DetailView):
    """Single audit row with full JSON details for debugging and AI handoff."""

    model = AuditLog
    template_name = 'settings/audit_log_detail.html'
    context_object_name = 'log'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        o = self.object
        details = o.details or {}
        ctx['details_json'] = json.dumps(details, indent=2, default=str)
        ctx['audit_clipboard'] = {
            "audit_log_id": o.pk,
            "timestamp": o.timestamp.isoformat() if o.timestamp else None,
            "actor_id": o.actor_id,
            "actor_username": o.actor.username if o.actor else None,
            "event_code": o.event_code or None,
            "outcome": o.outcome,
            "human_summary": o.human_summary or None,
            "action": o.action,
            "target_model": o.target_model or None,
            "target_id": o.target_id or None,
            "correlation_id": o.correlation_id or None,
            "view_name": o.view_name or None,
            "url_name": o.url_name or None,
            "ip_address": str(o.ip_address) if o.ip_address else None,
            "user_agent": o.user_agent or None,
            "details": details,
            "_glossary": {
                "event_code": "Stable id for filtering; http.* = middleware mutation capture.",
                "outcome": "success|failure|denied|partial|unknown — derived from HTTP status for middleware rows.",
                "correlation_id": "Per-request UUID linking rows from the same request.",
                "details": "Structured context; post_keys_summary redacts passwords/tokens.",
            },
        }
        return ctx


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

    def _model_suggestions(self):
        """Known model ids (across providers) for the <datalist> autocomplete."""
        return sort_models_by_cost(list(PRICING_PER_1M.keys()))

    def get(self, request):
        config = LLMConfig.load()
        api_key = decrypt_value(config.encrypted_api_key)
        form = LLMConfigForm(instance=config)

        context = self._build_metrics_context()
        context.update({
            'form': form,
            'api_key_masked': (api_key[:4] + '…' + api_key[-4:]) if api_key else '',
            'model_error': getattr(self, '_model_error', ''),
            'model_suggestions': self._model_suggestions(),
            'provider_base_urls': LLMConfig.PROVIDER_BASE_URLS,
        })
        return render(request, self.template_name, context)

    def post(self, request):
        config = LLMConfig.load()
        api_key = decrypt_value(config.encrypted_api_key)
        api_key_for_models = request.POST.get('api_key') or api_key
        form = LLMConfigForm(request.POST, instance=config)

        action = request.POST.get('action')
        if action == 'test_key':
            test_key = api_key_for_models
            if not test_key:
                messages.error(request, "Please enter an API key to test.")
            else:
                # Test against the chosen provider/base_url (works for any OpenAI-compatible API)
                provider = request.POST.get('provider') or config.provider
                base_url = (request.POST.get('base_url') or '').strip() \
                    or LLMConfig.PROVIDER_BASE_URLS.get(provider, '') or None
                try:
                    import openai as _openai
                    _openai.OpenAI(api_key=test_key, base_url=base_url).models.list()
                    messages.success(request, f"API key valid for {provider}. Connection OK.")
                except Exception as exc:
                    messages.error(request, f"API key test failed: {exc}")
            context = self._build_metrics_context()
            context.update({
                'form': form,
                'api_key_masked': (api_key[:4] + '…' + api_key[-4:]) if api_key else '',
                'model_suggestions': self._model_suggestions(),
                'provider_base_urls': LLMConfig.PROVIDER_BASE_URLS,
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
            'model_suggestions': self._model_suggestions(),
            'provider_base_urls': LLMConfig.PROVIDER_BASE_URLS,
        })
        return render(request, self.template_name, context)

    def _build_metrics_context(self):
        now = timezone.now()
        start_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_week = now - timedelta(days=7)
        start_day = now - timedelta(days=1)

        # Single aggregate instead of 6 separate queries
        agg = LLMUsageLog.objects.aggregate(
            total_calls=Count('id'),
            success_calls=Count('id', filter=Q(success=True)),
            failed_calls=Count('id', filter=Q(success=False)),
            total_tokens=Sum('total_tokens'),
            total_cost=Sum('cost_total'),
            total_latency=Sum('latency_ms'),
            calls_today=Count('id', filter=Q(created_at__gte=start_day)),
            calls_week=Count('id', filter=Q(created_at__gte=start_week)),
            calls_month=Count('id', filter=Q(created_at__gte=start_month)),
        )
        total_calls = agg['total_calls'] or 0
        total_latency = agg['total_latency'] or 0
        avg_latency = int(total_latency / total_calls) if total_calls else 0

        # Which service (request_type) used which model/API — calls, tokens, cost
        usage_breakdown = list(
            LLMUsageLog.objects
            .values('request_type', 'model_name')
            .annotate(
                calls=Count('id'),
                tokens=Sum('total_tokens'),
                cost=Sum('cost_total'),
                fails=Count('id', filter=Q(success=False)),
            )
            .order_by('-cost')[:40]
        )

        return {
            'llm_config': LLMConfig.load(),
            'total_calls': total_calls,
            'success_calls': agg['success_calls'] or 0,
            'failed_calls': agg['failed_calls'] or 0,
            'total_tokens': agg['total_tokens'] or 0,
            'total_cost': agg['total_cost'] or 0,
            'avg_latency': avg_latency,
            'calls_today': agg['calls_today'] or 0,
            'calls_week': agg['calls_week'] or 0,
            'calls_month': agg['calls_month'] or 0,
            'usage_breakdown': usage_breakdown,
            'recent_logs': LLMUsageLog.objects.order_by('-created_at')[:20],
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
    paginate_by = 50

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
        # System Prompt is no longer edited in the UI (the pipeline uses a fixed system
        # message and reads only generation_rules). Preserve any existing value rather
        # than wiping it when the field isn't submitted.
        mp.system_prompt = request.POST.get('system_prompt', mp.system_prompt)
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

    # Section -> partial template mapping for HTMX polling
    SECTION_TEMPLATES = {
        'kpis':           'core/dashboard_partials/_dashboard_kpis.html',
        'actions':        'core/dashboard_partials/_dashboard_actions.html',
        'harvest':        'core/dashboard_partials/_dashboard_harvest.html',
        'mid':            'core/dashboard_partials/_dashboard_consultants_ops.html',
        'activity':       'core/dashboard_partials/_dashboard_activity.html',
        'tables':         'core/dashboard_partials/_dashboard_tables.html',
    }

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.role == 'ADMIN'

    def get_template_names(self):
        if self.request.headers.get('HX-Request'):
            section = self.request.GET.get('section', '')
            if section in self.SECTION_TEMPLATES:
                return [self.SECTION_TEMPLATES[section]]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        section = self.request.GET.get('section', '') if self.request.headers.get('HX-Request') else ''

        if section == 'kpis':
            context.update(self._get_kpi_context())
        elif section == 'actions':
            # Action items depend on harvest + kpi data
            context.update(self._get_kpi_context())
            context.update(self._get_harvest_context())
            context.update(self._get_resume_context())
            context.update(self._get_interview_context())
            context.update(self._get_action_items_context(context))
        elif section == 'harvest':
            context.update(self._get_harvest_context())
        elif section == 'mid':
            context.update(self._get_consultant_status_context())
            context.update(self._get_ops_runs_context())
            context.update(self._get_resume_context())
            context.update(self._get_llm_usage_context())
        elif section == 'activity':
            context.update(get_submission_funnel_metrics())
            context.update(self._get_recent_activity_context())
        elif section == 'tables':
            context.update(self._get_consultant_roi_data())
            context.update(self._get_employee_stats_context())
            context.update(self._get_interview_context())
            context.update(self._get_job_breakdown_context())
        else:
            # Full page load -- all context
            context.update(self._get_warnings_context())
            context.update(self._get_kpi_context())
            context.update(self._get_harvest_context())
            context.update(self._get_consultant_status_context())
            context.update(self._get_ops_runs_context())
            context.update(self._get_resume_context())
            context.update(self._get_interview_context())
            context.update(self._get_job_breakdown_context())
            context.update(self._get_company_context())
            context.update(self._get_llm_usage_context())
            context.update(self._get_recent_activity_context())
            context.update(self._get_employee_stats_context())
            context.update(self._get_consultant_stats_context())
            context.update(get_submission_funnel_metrics())
            context.update(get_consultant_performance_metrics())
            context.update(get_time_to_hire_metrics())
            context.update(get_employee_leaderboard_metrics())
            context.update(self._get_market_intelligence_data())
            context.update(self._get_consultant_roi_data())
            context.update(self._get_submission_quality_data())
            context.update(self._get_system_health_context())
            context.update(self._get_pipeline_bottleneck_context())
            context.update(self._get_action_items_context(context))

        return context

    # -- Section context builders -----------------------------------------------

    def _get_warnings_context(self):
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
                "Go to Settings → LLM Config to set one."
            )
        return {'admin_warnings': warnings}

    def _get_kpi_context(self):
        ctx = {}
        # Jobs — 1 aggregate instead of 3 separate counts
        job_agg = Job.objects.aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(status=Job.Status.OPEN)),
            pool=Count('id', filter=Q(status='POOL', is_archived=False)),
            closed=Count('id', filter=Q(status=Job.Status.CLOSED)),
            archived=Count('id', filter=Q(is_archived=True)),
        )
        ctx['total_jobs'] = job_agg['total']
        ctx['active_jobs'] = job_agg['active']
        ctx['job_pool'] = job_agg['pool']
        ctx['closed_jobs'] = job_agg['closed']
        ctx['archived_jobs'] = job_agg['archived']

        # Users — 1 aggregate instead of 2
        user_agg = User.objects.aggregate(
            consultants=Count('id', filter=Q(role=User.Role.CONSULTANT)),
            employees=Count('id', filter=Q(role=User.Role.EMPLOYEE)),
        )
        ctx['total_consultants'] = user_agg['consultants']
        ctx['total_employees'] = user_agg['employees']

        # Applications — 1 aggregate instead of 3
        app_agg = ApplicationSubmission.objects.aggregate(
            total=Count('id'),
            pending=Count('id', filter=Q(status=ApplicationSubmission.Status.APPLIED)),
            placed=Count('id', filter=Q(status=ApplicationSubmission.Status.PLACED)),
        )
        ctx['total_applications'] = app_agg['total']
        ctx['pending_applications_count'] = app_agg['pending']
        ctx['placed_count'] = app_agg['placed']

        # Placements — 1 aggregate instead of 2
        placement_agg = Placement.objects.aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(status=Placement.PlacementStatus.ACTIVE)),
        )
        ctx['total_placements'] = placement_agg['total']
        ctx['active_placements'] = placement_agg['active']

        ctx['pending_timesheets'] = Timesheet.objects.filter(
            status=Timesheet.TimesheetStatus.SUBMITTED
        ).count()
        ctx['pending_commissions_amount'] = Commission.objects.filter(
            status=Commission.CommissionStatus.PENDING
        ).aggregate(total=Sum('commission_amount'))['total'] or 0
        try:
            from companies.models import Company
            ctx['company_total'] = Company.objects.count()
            ctx['company_with_platform'] = Company.objects.filter(
                platform_label__platform__isnull=False
            ).count()
        except Exception:
            ctx['company_total'] = 0
            ctx['company_with_platform'] = 0
        # Consultant status — 1 query instead of N
        consultant_status_agg = dict(
            ConsultantProfile.objects.values_list('status').annotate(c=Count('id')).order_by()
        )
        ctx['consultant_status'] = {
            s.value: consultant_status_agg.get(s.value, 0)
            for s in ConsultantProfile.Status
        }
        ctx['rawjob_total'] = 0
        try:
            from harvest.models import RawJob
            rawjob_sync_agg = dict(
                RawJob.objects.values_list('sync_status').annotate(c=Count('id')).order_by()
            )
            ctx['rawjob_total'] = sum(rawjob_sync_agg.values())
            ctx['rawjob_sync'] = {
                s.value: rawjob_sync_agg.get(s.value, 0) for s in RawJob.SyncStatus
            }
        except Exception:
            ctx['rawjob_sync'] = {}
        return ctx

    def _get_harvest_context(self):
        try:
            from harvest.models import RawJob
            # Bulk aggregation: 2 queries instead of 12+
            sync_agg = dict(
                RawJob.objects.values_list('sync_status').annotate(c=Count('id')).order_by()
            )
            scope_agg = dict(
                RawJob.objects.values_list('scope_status').annotate(c=Count('id')).order_by()
            )
            jd_agg = dict(
                RawJob.objects.values_list('has_description').annotate(c=Count('id')).order_by()
            )
            total = sum(sync_agg.values())
            return {
                'rawjob_total': total,
                'rawjob_sync': {s.value: sync_agg.get(s.value, 0) for s in RawJob.SyncStatus},
                'rawjob_scope': {s.value: scope_agg.get(s.value, 0) for s in RawJob.ScopeStatus},
                'rawjob_with_jd': jd_agg.get(True, 0),
                'rawjob_missing_jd': jd_agg.get(False, 0),
            }
        except Exception:
            return {
                'rawjob_total': 0, 'rawjob_sync': {}, 'rawjob_scope': {},
                'rawjob_with_jd': 0, 'rawjob_missing_jd': 0,
            }

    def _get_ops_runs_context(self):
        try:
            from harvest.models import HarvestOpsRun
            return {
                'recent_ops_runs': list(
                    HarvestOpsRun.objects.order_by("-created_at")
                    .values("id", "operation", "status", "created_at", "finished_at",
                            "progress_current", "progress_total", "progress_message")[:8]
                ),
            }
        except Exception:
            return {'recent_ops_runs': []}

    def _get_consultant_status_context(self):
        consultants = ConsultantProfile.objects.select_related('user').annotate(
            total_apps=Count('submissions'),
            in_progress=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.IN_PROGRESS)),
            applied=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.APPLIED)),
            interview=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.INTERVIEW)),
            offer=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.OFFER)),
            rejected=Count('submissions', filter=Q(submissions__status=ApplicationSubmission.Status.REJECTED)),
        ).order_by('user__first_name', 'user__last_name')
        return {
            'consultant_stats': list(consultants),
            'consultant_status': {
                s.value: ConsultantProfile.objects.filter(status=s).count()
                for s in ConsultantProfile.Status
            },
        }

    def _get_consultant_stats_context(self):
        return self._get_consultant_status_context()

    def _get_resume_context(self):
        try:
            from resumes.models import ResumeDraft
            return {
                'resume_stats': {
                    s.value: ResumeDraft.objects.filter(status=s).count()
                    for s in ResumeDraft.Status
                },
                'resume_total': ResumeDraft.objects.count(),
                'resume_avg_ats': ResumeDraft.objects.filter(
                    status__in=[ResumeDraft.Status.DRAFT, ResumeDraft.Status.FINAL],
                    ats_score__gt=0,
                ).aggregate(avg=Avg('ats_score'))['avg'] or 0,
            }
        except Exception:
            return {'resume_stats': {}, 'resume_total': 0, 'resume_avg_ats': 0}

    def _get_interview_context(self):
        try:
            from interviews_app.models import Interview
            return {
                'interview_stats': {
                    'scheduled': Interview.objects.filter(status='SCHEDULED').count(),
                    'completed': Interview.objects.filter(status='COMPLETED').count(),
                    'total': Interview.objects.count(),
                },
            }
        except Exception:
            return {'interview_stats': {'scheduled': 0, 'completed': 0, 'total': 0}}

    def _get_job_breakdown_context(self):
        return {
            'total_jobs': Job.objects.count(),
            'job_pool': Job.objects.filter(status='POOL', is_archived=False).count(),
            'job_open': Job.objects.filter(status='OPEN', is_archived=False).count(),
            'job_closed': Job.objects.filter(status='CLOSED').count(),
            'job_draft': Job.objects.filter(status='DRAFT').count(),
        }

    def _get_company_context(self):
        try:
            from companies.models import Company
            return {
                'company_total': Company.objects.count(),
                'company_with_platform': Company.objects.filter(
                    platform_label__platform__isnull=False
                ).count(),
            }
        except Exception:
            return {'company_total': 0, 'company_with_platform': 0}

    def _get_llm_usage_context(self):
        try:
            month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            from core.models import LLMUsageLog
            llm_qs = LLMUsageLog.objects.filter(created_at__gte=month_start)
            return {
                'llm_usage': {
                    'calls': llm_qs.count(),
                    'tokens': llm_qs.aggregate(t=Sum('total_tokens'))['t'] or 0,
                    'cost': float(llm_qs.aggregate(c=Sum('cost_total'))['c'] or 0),
                },
            }
        except Exception:
            return {'llm_usage': {'calls': 0, 'tokens': 0, 'cost': 0}}

    def _get_recent_activity_context(self):
        return {
            'recent_jobs': Job.objects.select_related('posted_by').order_by('-created_at')[:5],
            'recent_applications': ApplicationSubmission.objects.select_related(
                'job', 'consultant__user'
            ).order_by('-created_at')[:5],
        }

    def _get_employee_stats_context(self):
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
        return {
            'employee_stats': [
                {
                    'user': e,
                    'jobs_posted': e.jobs_posted,
                    'open_jobs': e.open_jobs,
                    'apps_received': app_by_employee.get(e.pk, {}).get('apps_received', 0),
                    'pending': app_by_employee.get(e.pk, {}).get('pending', 0),
                }
                for e in employees
            ],
        }

    def _get_action_items_context(self, context):
        action_items = []
        uc_count = context.get('rawjob_scope', {}).get('REVIEW_UNKNOWN_COUNTRY', 0)
        if uc_count:
            action_items.append({
                'type': 'warning', 'icon': '\U0001f30d',
                'text': f'{uc_count} jobs need country review',
                'url': reverse('harvest-unknown-country-review'),
            })
        failed_sync = context.get('rawjob_sync', {}).get('FAILED', 0)
        if failed_sync:
            action_items.append({
                'type': 'error', 'icon': '❌',
                'text': f'{failed_sync} RawJobs failed to sync',
                'url': reverse('jobs-pipeline') + '?tab=raw&sync_status=FAILED',
            })
        if context.get('pending_applications_count', 0):
            action_items.append({
                'type': 'info', 'icon': '\U0001f4cb',
                'text': f'{context["pending_applications_count"]} applications pending review',
                'url': reverse('submission-list') + '?status=APPLIED',
            })
        if context.get('pending_timesheets', 0):
            action_items.append({
                'type': 'info', 'icon': '⏱️',
                'text': f'{context["pending_timesheets"]} timesheets awaiting approval',
                'url': reverse('timesheet-list'),
            })
        if context.get('interview_stats', {}).get('scheduled', 0):
            action_items.append({
                'type': 'info', 'icon': '\U0001f3af',
                'text': f'{context["interview_stats"]["scheduled"]} interviews scheduled',
                'url': reverse('interview-list'),
            })
        unscoped = context.get('rawjob_scope', {}).get('UNSCOPED', 0)
        if unscoped:
            action_items.append({
                'type': 'warning', 'icon': '\U0001f50d',
                'text': f'{unscoped:,} unscoped raw jobs need evaluation',
                'url': reverse('jobs-pipeline') + '?tab=raw&scope_status=UNSCOPED',
            })
        resume_errors = context.get('resume_stats', {}).get('ERROR', 0)
        if resume_errors:
            action_items.append({
                'type': 'error', 'icon': '\U0001f4c4',
                'text': f'{resume_errors} resume drafts failed generation',
                'url': reverse('resume-generate'),
            })
        return {'action_items': action_items}

    def _get_market_intelligence_data(self):
        """Market Intelligence: skills/roles in demand, top companies, salary by role."""
        from users.models import MarketingRole
        open_jobs = Job.objects.filter(status=Job.Status.OPEN)

        # Role counts — DB-side aggregation, no Python loops (was N+1)
        top_roles = list(
            MarketingRole.objects
            .filter(jobs__status=Job.Status.OPEN)
            .values('name')
            .annotate(job_count=Count('jobs', distinct=True))
            .order_by('-job_count')[:15]
        )

        company_counts = list(
            open_jobs.values('company')
            .annotate(job_count=Count('id'))
            .order_by('-job_count')[:15]
        )

        # Salary by role — single query with values, no Python loop over all jobs
        salary_rows = (
            open_jobs
            .filter(marketing_roles__isnull=False, salary_range__isnull=False)
            .exclude(salary_range='')
            .values('marketing_roles__name', 'salary_range')
            .order_by('marketing_roles__name')[:500]
        )
        salary_by_role = {}
        for row in salary_rows:
            name = row['marketing_roles__name']
            sal = row['salary_range'].strip()
            if sal:
                if name not in salary_by_role:
                    salary_by_role[name] = []
                if sal not in salary_by_role[name] and len(salary_by_role[name]) < 5:
                    salary_by_role[name].append(sal)

        job_type_qs = open_jobs.values('job_type').annotate(c=Count('id'))
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

    def _get_system_health_context(self):
        """System health: harvest pipeline, data quality, celery, coverage metrics."""
        now = timezone.now()
        health = {}
        try:
            from harvest.models import RawJob, CompanyPlatformLabel, FetchBatch
            from companies.models import Company

            # Harvest pipeline health
            total_companies = Company.objects.count()
            companies_with_labels = Company.objects.filter(platform_label__platform__isnull=False).count()
            labels_live = CompanyPlatformLabel.objects.filter(portal_alive=True).count()
            labels_down = CompanyPlatformLabel.objects.filter(portal_alive=False).count()
            labels_no_tenant = CompanyPlatformLabel.objects.filter(tenant_id='').count() + \
                               CompanyPlatformLabel.objects.filter(tenant_id__isnull=True).count()

            # Recent harvest activity
            last_24h = now - timedelta(hours=24)
            last_7d = now - timedelta(days=7)
            new_rawjobs_24h = RawJob.objects.filter(fetched_at__gte=last_24h).count()
            new_rawjobs_7d = RawJob.objects.filter(fetched_at__gte=last_7d).count()
            last_batch = FetchBatch.objects.order_by('-created_at').first()
            latest_rawjob_at = RawJob.objects.aggregate(latest=Max('fetched_at'))['latest']

            # Data quality
            rawjobs_no_jd = RawJob.objects.filter(has_description=False, is_active=True).count()
            rawjobs_cold = RawJob.objects.filter(is_cold=True).count()

            last_batch_at = last_batch.created_at if last_batch else None
            source_sync_at = latest_rawjob_at or last_batch_at

            harvest_stale = True
            harvest_stale_hours = 0
            if source_sync_at:
                age = now - source_sync_at
                harvest_stale_hours = int(age.total_seconds() / 3600)
                harvest_stale = harvest_stale_hours >= 24 and new_rawjobs_24h == 0

            batch_stale = False
            batch_stale_hours = None
            if last_batch_at:
                batch_age = now - last_batch_at
                batch_stale_hours = int(batch_age.total_seconds() / 3600)
                batch_stale = batch_stale_hours >= 24

            health['harvest_health'] = {
                'total_companies': total_companies,
                'companies_with_labels': companies_with_labels,
                'companies_coverage_pct': round(companies_with_labels / total_companies * 100) if total_companies else 0,
                'labels_live': labels_live,
                'labels_down': labels_down,
                'labels_no_tenant': labels_no_tenant,
                'new_rawjobs_24h': new_rawjobs_24h,
                'new_rawjobs_7d': new_rawjobs_7d,
                'last_batch_at': last_batch_at,
                'last_batch_count': last_batch.raw_jobs_created if last_batch and hasattr(last_batch, 'raw_jobs_created') else 0,
                'latest_rawjob_at': latest_rawjob_at,
                'source_sync_at': source_sync_at,
                'source_sync_basis': 'raw_jobs' if latest_rawjob_at else 'fetch_batch',
                'batch_stale': batch_stale,
                'batch_stale_hours': batch_stale_hours,
                'rawjobs_no_jd': rawjobs_no_jd,
                'rawjobs_cold': rawjobs_cold,
                'harvest_stale': harvest_stale,
                'harvest_stale_hours': harvest_stale_hours,
            }
        except Exception:
            health['harvest_health'] = {}

        # Application pipeline health
        AS = ApplicationSubmission
        pipeline_agg = AS.objects.aggregate(
            in_progress=Count('id', filter=Q(status=AS.Status.IN_PROGRESS)),
            applied=Count('id', filter=Q(status=AS.Status.APPLIED)),
            interview=Count('id', filter=Q(status=AS.Status.INTERVIEW)),
            offer=Count('id', filter=Q(status=AS.Status.OFFER)),
            rejected=Count('id', filter=Q(status=AS.Status.REJECTED)),
            withdrawn=Count('id', filter=Q(status=AS.Status.WITHDRAWN)),
        )
        health['pipeline_health'] = pipeline_agg

        # Time-based activity
        last_24h = now - timedelta(hours=24)
        last_7d = now - timedelta(days=7)
        health['activity_metrics'] = {
            'new_jobs_24h': Job.objects.filter(created_at__gte=last_24h).count(),
            'new_jobs_7d': Job.objects.filter(created_at__gte=last_7d).count(),
            'new_apps_24h': AS.objects.filter(created_at__gte=last_24h).count(),
            'new_apps_7d': AS.objects.filter(created_at__gte=last_7d).count(),
        }

        return health

    def _get_pipeline_bottleneck_context(self):
        """Pipeline bottleneck: count jobs at each stage + average time spent."""
        try:
            from harvest.models import RawJob
            stage_counts = {}
            for stage_val, stage_label in Job.Stage.choices:
                stage_counts[stage_val] = {
                    'label': stage_label,
                    'count': Job.objects.filter(stage=stage_val, is_archived=False).count(),
                }

            # RawJob scope pipeline
            scope_counts = {}
            scope_statuses = RawJob.objects.filter(is_active=True).values('scope_status').annotate(
                cnt=Count('id')
            )
            for row in scope_statuses:
                scope_counts[row['scope_status'] or 'UNSCOPED'] = row['cnt']

            # Sync status pipeline
            sync_counts = {}
            sync_statuses = RawJob.objects.filter(is_active=True).values('sync_status').annotate(
                cnt=Count('id')
            )
            for row in sync_statuses:
                sync_counts[row['sync_status'] or 'NONE'] = row['cnt']

            # Gate status counts
            gate_counts = {}
            gate_statuses = Job.objects.filter(is_archived=False).values('gate_status').annotate(
                cnt=Count('id')
            )
            for row in gate_statuses:
                gate_counts[row['gate_status'] or 'NONE'] = row['cnt']

            max_stage_count = max((s['count'] for s in stage_counts.values()), default=1) or 1

            return {
                'bottleneck_stages': stage_counts,
                'bottleneck_max_stage': max_stage_count,
                'bottleneck_scope': scope_counts,
                'bottleneck_sync': sync_counts,
                'bottleneck_gate': gate_counts,
            }
        except Exception:
            return {}


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

        # Job aggregates — single query
        job_agg = my_jobs.aggregate(
            total=Count('id'),
            open=Count('id', filter=Q(status='OPEN')),
            closed=Count('id', filter=Q(status='CLOSED')),
            pool=Count('id', filter=Q(status='POOL')),
            draft=Count('id', filter=Q(status='DRAFT')),
        )
        context['my_jobs_count'] = job_agg['total']
        context['my_open_jobs'] = job_agg['open']
        context['my_closed_jobs'] = job_agg['closed']
        context['my_pool_jobs'] = job_agg['pool']
        context['my_draft_jobs'] = job_agg['draft']

        my_job_ids = my_jobs.values_list('id', flat=True)
        apps_for_my_jobs = ApplicationSubmission.objects.filter(job_id__in=my_job_ids)

        # App aggregates — single query
        app_agg = apps_for_my_jobs.aggregate(
            total=Count('id'),
            applied=Count('id', filter=Q(status='APPLIED')),
            interview=Count('id', filter=Q(status='INTERVIEW')),
            offer=Count('id', filter=Q(status='OFFER')),
            placed=Count('id', filter=Q(status='PLACED')),
            rejected=Count('id', filter=Q(status='REJECTED')),
        )
        context['total_apps_received'] = app_agg['total']
        context['pending_apps'] = app_agg['applied']
        context['interview_apps'] = app_agg['interview']
        context['offer_apps'] = app_agg['offer']
        context['placed_apps'] = app_agg['placed']
        context['rejected_apps'] = app_agg['rejected']

        context['recent_my_jobs'] = my_jobs.select_related('company_obj').order_by('-created_at')[:6]
        context['recent_apps'] = (
            apps_for_my_jobs
            .select_related('job', 'consultant__user')
            .order_by('-created_at')[:8]
        )
        context['all_open_jobs'] = Job.objects.filter(status='OPEN').count()

        # Upcoming interviews for my jobs
        try:
            from interviews_app.models import Interview
            context['upcoming_interviews'] = (
                Interview.objects
                .filter(submission__job__posted_by=user, status='SCHEDULED')
                .select_related('submission__job', 'submission__consultant__user')
                .order_by('scheduled_at')[:5]
            )
        except Exception:
            context['upcoming_interviews'] = []

        # Employee profile info
        ep = getattr(user, 'employee_profile', None)
        context['can_manage_consultants'] = getattr(ep, 'can_manage_consultants', False)
        context['designation'] = getattr(ep, 'designation', None) if ep else None
        context['department'] = getattr(ep, 'department', None) if ep else None

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
                from .audit_utils import log_audit_event

                log_audit_event(
                    actor=request.user,
                    action='feature_flag_update',
                    event_code='settings.feature_flag_update',
                    outcome=AuditLog.Outcome.SUCCESS,
                    human_summary=f"Feature flag {flag.key}: set {field}={value}",
                    target_model='FeatureFlag',
                    target_id=str(flag.pk),
                    details={'key': flag.key, 'field': field, 'value': value},
                    request=request,
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
# SYSTEM OPERATIONS CENTER (GLOBAL MONITORING)
# One place to see: IN-PROGRESS, SCHEDULED, COMPLETED + subsystem health.
# ─────────────────────────────────────────────────────────────────────────────

OPS_DEFAULT_TASK_META = {
    "label": None,
    "color": "#334155",
    "owner": "Platform Ops",
    "priority": "P2",
    "sla_minutes": 60,
}

OPS_TASK_META = {
    "harvest.backfill_descriptions": {"label": "JD Backfill", "color": "#f97316", "owner": "Harvest Team", "priority": "P2", "sla_minutes": 180},
    "harvest.backfill_descriptions_chunk": {"label": "JD Backfill Chunk", "color": "#f97316", "owner": "Harvest Team", "priority": "P2", "sla_minutes": 60},
    "harvest.fetch_raw_jobs_batch": {"label": "Harvest Batch", "color": "#0ea5e9", "owner": "Harvest Team", "priority": "P1", "sla_minutes": 30},
    "harvest.fetch_raw_jobs_for_company": {"label": "Company Fetch", "color": "#0ea5e9", "owner": "Harvest Team", "priority": "P1", "sla_minutes": 60},
    "harvest.harvest_jobs": {"label": "Harvest Jobs", "color": "#0ea5e9", "owner": "Harvest Team", "priority": "P1", "sla_minutes": 30},
    "harvest.sync_harvested_to_pool": {"label": "Pool Sync", "color": "#10b981", "owner": "Harvest Team", "priority": "P1", "sla_minutes": 45},
    "harvest.detect_company_platforms": {"label": "Platform Detection", "color": "#38bdf8", "owner": "Harvest Team", "priority": "P2", "sla_minutes": 120},
    "harvest.verify_all_portals": {"label": "Portal Verify", "color": "#8b5cf6", "owner": "Harvest Team", "priority": "P2", "sla_minutes": 180},
    "harvest.enrich_existing_jobs": {"label": "Harvest Enrichment", "color": "#22c55e", "owner": "Data Team", "priority": "P2", "sla_minutes": 180},
    "harvest.backfill_resume_contract": {"label": "Resume Contract Backfill", "color": "#6366f1", "owner": "Data Team", "priority": "P2", "sla_minutes": 240},
    "harvest.backfill_platform_labels_from_jobs": {"label": "Label Backfill", "color": "#64748b", "owner": "Harvest Team", "priority": "P3", "sla_minutes": 240},
    "harvest.cleanup_harvested_jobs": {"label": "Harvest Cleanup", "color": "#94a3b8", "owner": "Harvest Team", "priority": "P3", "sla_minutes": 300},
    "harvest.jarvis_ingest": {"label": "Jarvis Ingest", "color": "#ec4899", "owner": "Harvest Team", "priority": "P1", "sla_minutes": 15},
    "companies.tasks.validate_company_links_task": {"label": "Company Link Validator", "color": "#14b8a6", "owner": "Data Team", "priority": "P2", "sla_minutes": 120},
    "companies.tasks.re_enrich_stale_companies_task": {"label": "Company Re-Enrichment", "color": "#06b6d4", "owner": "Data Team", "priority": "P2", "sla_minutes": 240},
    "jobs.tasks.validate_job_urls_task": {"label": "Job Link Validator", "color": "#f59e0b", "owner": "Jobs Team", "priority": "P2", "sla_minutes": 180},
    "jobs.tasks.auto_close_jobs_task": {"label": "Auto Close Jobs", "color": "#f59e0b", "owner": "Jobs Team", "priority": "P3", "sla_minutes": 360},
    "submissions.tasks.send_followup_reminders": {"label": "Followup Reminders", "color": "#8b5cf6", "owner": "Submissions Team", "priority": "P2", "sla_minutes": 120},
    "submissions.tasks.detect_stale_submissions": {"label": "Stale Submissions Detector", "color": "#7c3aed", "owner": "Submissions Team", "priority": "P2", "sla_minutes": 120},
    "core.tasks.poll_email_ingest_task": {"label": "Email Ingest", "color": "#64748b", "owner": "Platform Ops", "priority": "P1", "sla_minutes": 15},
    "core.tasks.send_weekly_executive_report_task": {"label": "Weekly Executive Report", "color": "#334155", "owner": "Platform Ops", "priority": "P3", "sla_minutes": 1440},
}


def _ops_task_meta(task_name: str) -> dict:
    if not task_name:
        leaf_label = "Unknown Task"
    else:
        leaf = task_name.split(".")[-1]
        leaf_label = leaf.replace("_", " ").strip().title()
    base = dict(OPS_DEFAULT_TASK_META)
    specific = OPS_TASK_META.get(task_name, {})
    base.update({k: v for k, v in specific.items() if v is not None})
    base["label"] = base.get("label") or leaf_label
    return base


def _ops_pretty_task_label(task_name: str) -> str:
    return _ops_task_meta(task_name).get("label", "Unknown Task")


def _ops_task_color(task_name: str) -> str:
    return _ops_task_meta(task_name).get("color", "#334155")


def _ops_schedule_label(task):
    if getattr(task, "crontab", None):
        c = task.crontab
        m, h, dow = c.minute, c.hour, c.day_of_week
        day_map = {"0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat"}
        if m.startswith("*/"):
            return f"Every {m[2:]} min"
        if h.startswith("*/"):
            return f"Every {h[2:]} hr"
        if dow != "*":
            return f"{day_map.get(dow, dow)} {str(h).zfill(2)}:{str(m).zfill(2)} UTC"
        return f"Daily {str(h).zfill(2)}:{str(m).zfill(2)} UTC"
    if getattr(task, "interval", None):
        return f"Every {task.interval}"
    return "—"


def _ops_run_summary(result_payload) -> str:
    if not isinstance(result_payload, dict):
        return ""
    keys = ("total", "processed", "created", "updated", "skipped", "failed", "synced")
    parts = [f"{k}:{result_payload[k]}" for k in keys if k in result_payload and result_payload.get(k) not in (None, "")]
    if parts:
        return " · ".join(parts)[:220]
    msg = result_payload.get("message") or ""
    return str(msg)[:220]


def _build_ops_snapshot() -> dict:
    cache_key = "ops_center_snapshot_v2"
    cached = cache.get(cache_key)
    if cached:
        return cached

    from celery import current_app
    from django_celery_beat.models import PeriodicTask
    from django_celery_results.models import TaskResult
    from companies.models import Company
    from harvest.models import CompanyFetchRun, FetchBatch, RawJob
    from jobs.models import Job
    from submissions.models import ApplicationSubmission
    from resumes.models import ResumeDraft

    now = timezone.now()
    since_24h = now - timedelta(hours=24)

    inspect_errors = []
    try:
        inspect = current_app.control.inspect(timeout=1)
    except Exception as exc:
        inspect = None
        inspect_errors.append(f"Failed to initialize Celery inspect: {exc}")

    def _safe_inspect(callable_name):
        if inspect is None:
            return {}
        try:
            fn = getattr(inspect, callable_name, None)
            return fn() or {}
        except Exception as exc:
            inspect_errors.append(f"Celery inspect.{callable_name} failed: {exc}")
            return {}

    # Run all 4 inspect calls in parallel (each is a blocking RPC with 1s timeout)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        f_active = executor.submit(_safe_inspect, "active")
        f_reserved = executor.submit(_safe_inspect, "reserved")
        f_eta = executor.submit(_safe_inspect, "scheduled")
        f_stats = executor.submit(_safe_inspect, "stats")
    active_map = f_active.result()
    reserved_map = f_reserved.result()
    eta_map = f_eta.result()
    stats_map = f_stats.result()

    live_tasks = []
    seen_live_keys = set()

    def _append_live_task(payload):
        task_id = payload.get("id") or ""
        key = task_id or f"{payload.get('state','')}:{payload.get('task_name','')}:{payload.get('worker','')}"
        if key in seen_live_keys:
            return
        seen_live_keys.add(key)
        live_tasks.append(payload)

    for worker_name, entries in active_map.items():
        for t in entries or []:
            task_id = t.get("id", "")
            task_name = t.get("name", "")
            meta = _ops_task_meta(task_name)
            state = "RUNNING"
            percent, message = 5, "Running..."
            started = t.get("time_start")
            age_seconds = int((timezone.now().timestamp() - float(started))) if started else None
            _append_live_task({
                "id": task_id,
                "task_name": task_name,
                "label": meta["label"],
                "color": meta["color"],
                "owner": meta["owner"],
                "priority": meta["priority"],
                "sla_minutes": meta["sla_minutes"],
                "state": state,
                "worker": worker_name,
                "percent": max(0, min(100, percent)),
                "message": message,
                "age_seconds": age_seconds,
            })

    for worker_name, entries in reserved_map.items():
        for t in entries or []:
            task_name = t.get("name", "")
            meta = _ops_task_meta(task_name)
            _append_live_task({
                "id": t.get("id", ""),
                "task_name": task_name,
                "label": meta["label"],
                "color": meta["color"],
                "owner": meta["owner"],
                "priority": meta["priority"],
                "sla_minutes": meta["sla_minutes"],
                "state": "QUEUED",
                "worker": worker_name,
                "percent": 0,
                "message": "Queued on worker; waiting for execution slot.",
                "age_seconds": None,
            })

    for worker_name, entries in eta_map.items():
        for item in entries or []:
            req = item.get("request") if isinstance(item, dict) else {}
            req = req if isinstance(req, dict) else {}
            task_name = req.get("name", "")
            eta = item.get("eta") if isinstance(item, dict) else ""
            meta = _ops_task_meta(task_name)
            _append_live_task({
                "id": req.get("id", ""),
                "task_name": task_name,
                "label": meta["label"],
                "color": meta["color"],
                "owner": meta["owner"],
                "priority": meta["priority"],
                "sla_minutes": meta["sla_minutes"],
                "state": "SCHEDULED_QUEUE",
                "worker": worker_name,
                "percent": 0,
                "message": f"ETA: {eta}" if eta else "Scheduled by worker ETA queue.",
                "age_seconds": None,
            })

    workers = []
    for wname, info in stats_map.items():
        pool = info.get("pool", {}) if isinstance(info, dict) else {}
        queues = [q.get("name", "") for q in (info.get("consumer", {}) or {}).get("queues", []) if isinstance(q, dict)]
        workers.append({
            "name": wname,
            "concurrency": pool.get("max-concurrency") or 0,
            "processes": len(pool.get("processes", []) or []),
            "queues": [q for q in queues if q],
        })

    schedule_rows = []
    for pt in PeriodicTask.objects.select_related("crontab", "interval").order_by("-enabled", "name")[:120]:
        meta = _ops_task_meta(pt.task or pt.name)
        crontab_obj = getattr(pt, "crontab", None)
        schedule_rows.append({
            "id": pt.pk,
            "name": pt.name,
            "task": pt.task,
            "label": meta["label"],
            "color": meta["color"],
            "owner": meta["owner"],
            "priority": meta["priority"],
            "sla_minutes": meta["sla_minutes"],
            "enabled": bool(pt.enabled),
            "one_off": bool(pt.one_off),
            "last_run_at": pt.last_run_at.isoformat() if pt.last_run_at else "",
            "next_run_at": getattr(pt, "next_run_at", None).isoformat() if getattr(pt, "next_run_at", None) else "",
            "total_run_count": int(pt.total_run_count or 0),
            "uses_crontab": bool(crontab_obj),
            "minute": getattr(crontab_obj, "minute", "0"),
            "hour": getattr(crontab_obj, "hour", "*"),
            "day_of_week": getattr(crontab_obj, "day_of_week", "*"),
            "day_of_month": getattr(crontab_obj, "day_of_month", "*"),
            "month_of_year": getattr(crontab_obj, "month_of_year", "*"),
            "interval_label": str(pt.interval) if getattr(pt, "interval", None) else "",
            "schedule": _ops_schedule_label(pt),
        })

    recent_results_qs = TaskResult.objects.only(
        "task_id", "task_name", "status", "date_done", "date_created"
    ).order_by("-date_done")[:120]
    recent_results = []
    for tr in recent_results_qs:
        runtime = None
        if tr.date_done and tr.date_created:
            runtime = int((tr.date_done - tr.date_created).total_seconds())
        task_name = tr.task_name or ""
        meta = _ops_task_meta(task_name)
        recent_results.append({
            "id": (tr.task_id or "")[:8],
            "task_name": task_name,
            "label": meta["label"],
            "color": meta["color"],
            "owner": meta["owner"],
            "priority": meta["priority"],
            "sla_minutes": meta["sla_minutes"],
            "status": tr.status or "UNKNOWN",
            "completed_at": tr.date_done.isoformat() if tr.date_done else "",
            "runtime_seconds": runtime,
        })

    result_stats = TaskResult.objects.filter(date_done__gte=since_24h).values("status").annotate(c=Count("id"))
    result_stats_map = {row["status"]: row["c"] for row in result_stats}
    success_24h = int(result_stats_map.get("SUCCESS", 0))
    failed_24h = int(result_stats_map.get("FAILURE", 0))
    revoked_24h = int(result_stats_map.get("REVOKED", 0))

    job_stats = Job.objects.filter(is_archived=False).aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(status=Job.Status.OPEN)),
        pool=Count("id", filter=Q(status=Job.Status.POOL)),
        closed=Count("id", filter=Q(status=Job.Status.CLOSED)),
    )
    pool_jobs = Job.objects.filter(is_archived=False, status=Job.Status.POOL)
    gate_stats = pool_jobs.aggregate(
        eligible=Count("id", filter=Q(gate_status=Job.GateStatus.ELIGIBLE)),
        review=Count("id", filter=Q(gate_status=Job.GateStatus.REVIEW)),
        blocked=Count("id", filter=Q(gate_status=Job.GateStatus.BLOCKED)),
        lane_auto=Count("id", filter=Q(vet_lane=Job.VetLane.AUTO)),
        lane_human=Count("id", filter=Q(vet_lane=Job.VetLane.HUMAN)),
        lane_blocked=Count("id", filter=Q(vet_lane=Job.VetLane.BLOCKED)),
        age_gt_24h=Count("id", filter=Q(queue_entered_at__lt=since_24h)),
    )
    company_stats = Company.objects.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(enrichment_status=Company.EnrichmentStatus.PENDING)),
        failed=Count("id", filter=Q(enrichment_status=Company.EnrichmentStatus.FAILED)),
    )
    try:
        raw_stats = RawJob.objects.aggregate(
            total=Count("id"),
            pending_sync=Count("id", filter=Q(sync_status=RawJob.SyncStatus.PENDING)),
            failed_sync=Count("id", filter=Q(sync_status=RawJob.SyncStatus.FAILED)),
            missing_jd=Count("id", filter=Q(has_description=False)),
        )
    except (OperationalError, ProgrammingError):
        raw_stats = RawJob.objects.aggregate(
            total=Count("id"),
            pending_sync=Count("id", filter=Q(sync_status=RawJob.SyncStatus.PENDING)),
            failed_sync=Count("id", filter=Q(sync_status=RawJob.SyncStatus.FAILED)),
            missing_jd=Count("id", filter=Q(description__isnull=True) | Q(description="")),
        )
    submission_stats = ApplicationSubmission.objects.filter(is_archived=False).aggregate(
        total=Count("id"),
        in_progress=Count("id", filter=Q(status=ApplicationSubmission.Status.IN_PROGRESS)),
        interviews=Count("id", filter=Q(status=ApplicationSubmission.Status.INTERVIEW)),
        offers=Count("id", filter=Q(status=ApplicationSubmission.Status.OFFER)),
    )

    harvest_running_batches = FetchBatch.objects.filter(status=FetchBatch.Status.RUNNING).count()
    harvest_running_company_runs = CompanyFetchRun.objects.filter(status=CompanyFetchRun.Status.RUNNING).count()
    harvest_failed_24h = CompanyFetchRun.objects.filter(
        started_at__gte=since_24h,
        status__in=[CompanyFetchRun.Status.FAILED, CompanyFetchRun.Status.PARTIAL],
    ).count()

    from harvest.models import HarvestOpsRun
    ops_runs = []
    for run in HarvestOpsRun.objects.order_by("-created_at")[:30]:
        total = run.progress_total or 0
        current = run.progress_current or 0
        pct = int(100 * current / total) if total else (100 if run.status == HarvestOpsRun.Status.SUCCESS else 0)
        runtime = None
        if run.finished_at and run.created_at:
            runtime = int((run.finished_at - run.created_at).total_seconds())
        ops_runs.append({
            "id": run.pk,
            "operation": run.operation,
            "operation_label": run.get_operation_display(),
            "status": run.status,
            "started_at": run.created_at.isoformat() if run.created_at else "",
            "finished_at": run.finished_at.isoformat() if run.finished_at else "",
            "runtime_seconds": runtime,
            "progress_current": current,
            "progress_total": total,
            "progress_pct": pct,
            "progress_message": run.progress_message or "",
            "audit_payload": run.audit_payload or {},
        })

    pipeline_logs = []
    for log in PipelineRunLog.objects.order_by("-last_run_at")[:40]:
        payload = log.last_run_result if isinstance(log.last_run_result, dict) else {}
        meta = _ops_task_meta(log.task_name)
        pipeline_logs.append({
            "task_name": log.task_name,
            "label": meta["label"],
            "owner": meta["owner"],
            "priority": meta["priority"],
            "last_run_at": log.last_run_at.isoformat() if log.last_run_at else "",
            "summary": _ops_run_summary(payload),
        })

    missing_jd_threshold = int(getattr(settings, "OPS_ALERT_MISSING_JD_THRESHOLD", 2000))
    pending_sync_threshold = int(getattr(settings, "OPS_ALERT_PENDING_SYNC_THRESHOLD", 5000))

    alerts = []
    for inspect_error in inspect_errors:
        alerts.append({"level": "error", "message": inspect_error[:220]})
    if failed_24h > 0:
        alerts.append({"level": "error", "message": f"{failed_24h} background tasks failed in the last 24 hours."})
    if harvest_failed_24h > 0:
        alerts.append({"level": "warning", "message": f"{harvest_failed_24h} Harvest company runs were partial/failed in the last 24 hours."})
    if int(raw_stats.get("missing_jd") or 0) > missing_jd_threshold:
        alerts.append({"level": "warning", "message": f"JD backlog is high: {raw_stats['missing_jd']} raw jobs are missing descriptions."})
    if int(raw_stats.get("pending_sync") or 0) > pending_sync_threshold:
        alerts.append({"level": "warning", "message": f"Pool sync backlog is high: {raw_stats['pending_sync']} pending raw jobs."})

    # 24h trend timeline (hourly)
    timeline_hours = []
    slot0 = (now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
    for i in range(24):
        timeline_hours.append(slot0 + timedelta(hours=i))

    task_trend_map = {h: {"SUCCESS": 0, "FAILURE": 0, "REVOKED": 0, "TOTAL": 0} for h in timeline_hours}
    qs_task = (
        TaskResult.objects
        .filter(date_done__gte=slot0)
        .annotate(hour=TruncHour("date_done"))
        .values("hour", "status")
        .annotate(c=Count("id"))
    )
    for row in qs_task:
        hour = row.get("hour")
        status = row.get("status") or "UNKNOWN"
        c = int(row.get("c") or 0)
        if hour not in task_trend_map:
            continue
        task_trend_map[hour]["TOTAL"] += c
        if status in ("SUCCESS", "FAILURE", "REVOKED"):
            task_trend_map[hour][status] += c

    harvest_trend_map = {h: {"SUCCESS": 0, "PARTIAL": 0, "FAILED": 0, "RUNNING": 0, "TOTAL": 0} for h in timeline_hours}
    qs_harvest = (
        CompanyFetchRun.objects
        .filter(started_at__gte=slot0)
        .annotate(hour=TruncHour("started_at"))
        .values("hour", "status")
        .annotate(c=Count("id"))
    )
    for row in qs_harvest:
        hour = row.get("hour")
        status = row.get("status") or "UNKNOWN"
        c = int(row.get("c") or 0)
        if hour not in harvest_trend_map:
            continue
        harvest_trend_map[hour]["TOTAL"] += c
        if status in harvest_trend_map[hour]:
            harvest_trend_map[hour][status] += c

    trend = {
        "labels": [h.strftime("%H:%M") for h in timeline_hours],
        "task_total": [task_trend_map[h]["TOTAL"] for h in timeline_hours],
        "task_failed": [task_trend_map[h]["FAILURE"] for h in timeline_hours],
        "task_success": [task_trend_map[h]["SUCCESS"] for h in timeline_hours],
        "harvest_total": [harvest_trend_map[h]["TOTAL"] for h in timeline_hours],
        "harvest_failed": [harvest_trend_map[h]["FAILED"] for h in timeline_hours],
        "harvest_partial": [harvest_trend_map[h]["PARTIAL"] for h in timeline_hours],
        "harvest_success": [harvest_trend_map[h]["SUCCESS"] for h in timeline_hours],
    }

    summary = {
        "running_now": len([t for t in live_tasks if t["state"] == "RUNNING"]),
        "queued_now": len([t for t in live_tasks if t["state"] in ("QUEUED", "SCHEDULED_QUEUE")]),
        "workers_online": len(workers),
        "scheduled_enabled": len([s for s in schedule_rows if s["enabled"]]),
        "scheduled_total": len(schedule_rows),
        "completed_24h": success_24h + failed_24h + revoked_24h,
        "success_24h": success_24h,
        "failed_24h": failed_24h,
        "jobs_total": int(job_stats.get("total") or 0),
        "jobs_open": int(job_stats.get("open") or 0),
        "jobs_pool": int(job_stats.get("pool") or 0),
        "jobs_pool_eligible": int(gate_stats.get("eligible") or 0),
        "jobs_pool_review": int(gate_stats.get("review") or 0),
        "jobs_pool_blocked": int(gate_stats.get("blocked") or 0),
        "jobs_lane_auto": int(gate_stats.get("lane_auto") or 0),
        "jobs_lane_human": int(gate_stats.get("lane_human") or 0),
        "jobs_lane_blocked": int(gate_stats.get("lane_blocked") or 0),
        "jobs_pool_age_gt_24h": int(gate_stats.get("age_gt_24h") or 0),
        "companies_total": int(company_stats.get("total") or 0),
        "companies_pending_enrichment": int(company_stats.get("pending") or 0),
        "companies_failed_enrichment": int(company_stats.get("failed") or 0),
        "raw_total": int(raw_stats.get("total") or 0),
        "raw_pending_sync": int(raw_stats.get("pending_sync") or 0),
        "raw_failed_sync": int(raw_stats.get("failed_sync") or 0),
        "raw_missing_jd": int(raw_stats.get("missing_jd") or 0),
        "submissions_total": int(submission_stats.get("total") or 0),
        "submissions_in_progress": int(submission_stats.get("in_progress") or 0),
        "submissions_interviews": int(submission_stats.get("interviews") or 0),
        "submissions_offers": int(submission_stats.get("offers") or 0),
        "resume_drafts": ResumeDraft.objects.count(),
        "harvest_running_batches": int(harvest_running_batches),
        "harvest_running_company_runs": int(harvest_running_company_runs),
        "ops_alert_missing_jd_threshold": missing_jd_threshold,
        "ops_alert_pending_sync_threshold": pending_sync_threshold,
    }

    payload = {
        "generated_at": now.isoformat(),
        "summary": summary,
        "alerts": alerts,
        "inspect_ok": len(inspect_errors) == 0,
        "inspect_errors": inspect_errors,
        "live_tasks": live_tasks[:120],
        "workers": workers[:40],
        "scheduled": schedule_rows,
        "completed": recent_results,
        "pipelines": pipeline_logs,
        "ops_runs": ops_runs,
        "trend": trend,
    }
    cache.set(cache_key, payload, timeout=30)
    return payload


def _build_system_health():
    """Server health metrics visible in the Ops Center — no SSH needed.
    Every probe is fail-safe: a failure shows '—' instead of breaking the page."""
    health = {}

    # Disk (the container root reflects the host overlay filesystem)
    try:
        import shutil
        du = shutil.disk_usage("/")
        health["disk_total_gb"] = round(du.total / 1e9, 1)
        health["disk_used_gb"] = round(du.used / 1e9, 1)
        health["disk_pct"] = round(du.used / du.total * 100)
    except Exception:
        health["disk_pct"] = None

    # Database size
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            health["db_size"] = cur.fetchone()[0]
    except Exception:
        health["db_size"] = None

    # Errors (24h) from the in-app error log
    try:
        from .models import ErrorLog
        day_ago = timezone.now() - timedelta(hours=24)
        health["errors_24h"] = ErrorLog.objects.filter(created_at__gte=day_ago).count()
    except Exception:
        health["errors_24h"] = None

    # LLM spend this month
    try:
        from django.db.models import Sum as _Sum
        from .models import LLMUsageLog
        month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        health["llm_cost_month"] = float(
            LLMUsageLog.objects.filter(created_at__gte=month_start)
            .aggregate(c=_Sum("cost_total"))["c"] or 0)
    except Exception:
        health["llm_cost_month"] = None

    # Celery queue depth (Redis list lengths for default + harvest queues)
    try:
        import redis as _redis
        from django.conf import settings as _settings
        broker = getattr(_settings, "CELERY_BROKER_URL", "") or ""
        if broker.startswith("redis"):
            r = _redis.Redis.from_url(broker, socket_timeout=2)
            health["queue_default"] = r.llen("celery")
            health["queue_harvest"] = r.llen("harvest")
        else:
            health["queue_default"] = None
    except Exception:
        health["queue_default"] = None

    # Pipeline volume counters
    try:
        from harvest.models import RawJob
        from jobs.models import Job
        health["rawjob_count"] = RawJob.objects.count()
        health["job_count"] = Job.objects.count()
    except Exception:
        pass

    # Harvest freshness — the alarm that catches a silently-dead harvest.
    # (The engine stalled for ~4 weeks in May–June with zero signal; this makes
    # that impossible to miss.)
    try:
        from django.db.models import Max, Count as _Count
        from harvest.models import RawJob
        now = timezone.now()
        health["harvest_new_24h"] = RawJob.objects.filter(
            fetched_at__gte=now - timedelta(hours=24)).count()
        health["harvest_new_7d"] = RawJob.objects.filter(
            fetched_at__gte=now - timedelta(days=7)).count()
        newest = RawJob.objects.aggregate(m=Max("fetched_at"))["m"]
        health["harvest_newest_age_h"] = (
            round((now - newest).total_seconds() / 3600, 1) if newest else None)
        # Per-platform freshness scoreboard (single GROUP BY query)
        health["platform_freshness"] = list(
            RawJob.objects.values("platform_slug")
            .annotate(
                last_fetch=Max("fetched_at"),
                new_7d=_Count("id", filter=Q(fetched_at__gte=now - timedelta(days=7))),
            )
            .order_by("-last_fetch")[:30]
        )
        for row in health["platform_freshness"]:
            lf = row.get("last_fetch")
            row["age_days"] = round((now - lf).total_seconds() / 86400, 1) if lf else None
    except Exception:
        health["harvest_new_24h"] = None

    return health


class SystemOpsCenterView(AdminRequiredMixin, TemplateView):
    template_name = "settings/ops_center.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        snapshot = _build_ops_snapshot()
        ctx["ops_summary"] = snapshot.get("summary", {})
        ctx["ops_alerts"] = snapshot.get("alerts", [])
        ctx["ops_generated_at"] = snapshot.get("generated_at", "")
        ctx["ops_runs"] = snapshot.get("ops_runs", [])
        from harvest.models import FetchBatch

        ctx["latest_fetch_batch"] = FetchBatch.objects.order_by("-created_at").first()
        ctx["system_health"] = _build_system_health()
        return ctx


class SystemOpsCenterApiView(AdminRequiredMixin, View):
    """JSON snapshot for the system operations center."""

    def get(self, request, *args, **kwargs):
        return JsonResponse(_build_ops_snapshot(), encoder=DjangoJSONEncoder)


def _ops_next_url(request, fallback_name="ops-center"):
    """Safe post-action redirect target."""
    candidate = (
        request.POST.get("next")
        or request.GET.get("next")
        or request.META.get("HTTP_REFERER")
        or ""
    ).strip()
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return reverse(fallback_name)


def _is_ajax_request(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


class TaskToggleView(AdminRequiredMixin, View):
    """POST → toggle a periodic task on/off. Returns immediately (Beat re-reads within ~5s)."""

    def post(self, request, pk, *args, **kwargs):
        from django_celery_beat.models import PeriodicTask
        task = get_object_or_404(PeriodicTask, pk=pk)
        task.enabled = not task.enabled
        task.save(update_fields=["enabled"])
        status = "enabled" if task.enabled else "paused"
        msg = f"Task '{task.name}' is now {status}."
        if _is_ajax_request(request):
            return JsonResponse({"ok": True, "message": msg, "enabled": bool(task.enabled), "task_id": task.pk})
        messages.success(request, f"\u2705 {msg}")
        return redirect(_ops_next_url(request))


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
        msg = f"Schedule updated for '{task.name}'."
        if _is_ajax_request(request):
            return JsonResponse({"ok": True, "message": msg, "task_id": task.pk})
        messages.success(request, f"\u2705 {msg}")
        return redirect(_ops_next_url(request))


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
            msg = f"No run-now mapping for task: {task.task}"
            if _is_ajax_request(request):
                return JsonResponse({"ok": False, "message": msg}, status=400)
            messages.error(request, f"⚠️ {msg}")
            return redirect(_ops_next_url(request))

        module_path, func_name = mapping
        try:
            module = importlib.import_module(module_path)
            celery_task = getattr(module, func_name)
            kwargs_dict = json.loads(task.kwargs) if task.kwargs and task.kwargs != "{}" else {}
            result = celery_task.delay(**kwargs_dict)
            msg = f"Task '{task.name}' triggered. ID: {result.id[:8]}..."
            if _is_ajax_request(request):
                return JsonResponse({"ok": True, "message": msg, "task_id": task.pk, "run_id": result.id})
            messages.success(request, f"\U0001f680 {msg}")
            from urllib.parse import urlencode
            target = _ops_next_url(request)
            q = urlencode({"tp": result.id, "tpl": (task.name or "Scheduled task")[:120]})
            separator = "&" if "?" in target else "?"
            return redirect(f"{target}{separator}{q}")
        except Exception as e:
            msg = f"Failed to trigger task: {e}"
            if _is_ajax_request(request):
                return JsonResponse({"ok": False, "message": msg}, status=500)
            messages.error(request, f"❌ {msg}")

        return redirect(_ops_next_url(request))


# ── Incident Log (Error Tracking) ─────────────────────────────────────

class IncidentListView(AdminRequiredMixin, ListView):
    """Error log page — shows 500 errors, slow requests, and other incidents."""
    model = ErrorLog
    template_name = 'core/incident_list.html'
    context_object_name = 'incidents'
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related('user')
        p = self.request.GET
        if p.get('severity'):
            qs = qs.filter(severity=p['severity'])
        if p.get('resolved') == '1':
            qs = qs.filter(resolved=True)
        elif p.get('resolved') == '0':
            qs = qs.filter(resolved=False)
        if p.get('path'):
            qs = qs.filter(path__icontains=p['path'].strip())
        if p.get('q'):
            q = p['q'].strip()
            qs = qs.filter(
                Q(path__icontains=q) |
                Q(error_message__icontains=q) |
                Q(error_type__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = ErrorLog.objects.all()
        ctx['total_errors'] = qs.filter(severity='ERROR').count()
        ctx['total_slow'] = qs.filter(severity='SLOW').count()
        ctx['unresolved'] = qs.filter(resolved=False).count()
        ctx['today_count'] = qs.filter(
            created_at__date=timezone.now().date()
        ).count()
        return ctx


class IncidentDetailView(AdminRequiredMixin, DetailView):
    model = ErrorLog
    template_name = 'core/incident_detail.html'
    context_object_name = 'incident'


class IncidentResolveView(AdminRequiredMixin, View):
    """Toggle resolved status on an incident."""
    def post(self, request, pk):
        incident = get_object_or_404(ErrorLog, pk=pk)
        incident.resolved = not incident.resolved
        incident.save(update_fields=['resolved'])
        messages.success(request, f"Incident #{pk} {'resolved' if incident.resolved else 'reopened'}.")
        return redirect('incident-list')

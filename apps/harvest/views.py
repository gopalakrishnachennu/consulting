import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, ListView, TemplateView, UpdateView, View

from core.http import redirect_with_task_progress

from .forms import JobBoardPlatformForm
from .models import CompanyPlatformLabel, HarvestRun, HarvestedJob, JobBoardPlatform

logger = logging.getLogger(__name__)


class SuperuserRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_superuser


# ── Platform Registry ──────────────────────────────────────────────────────────

class PlatformListView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/settings_platforms.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["platforms"] = JobBoardPlatform.objects.annotate(
            company_count=Count("labels")
        ).order_by("name")
        ctx["form"] = JobBoardPlatformForm()
        ctx["active_tab"] = "platforms"
        ctx["total_platforms"] = JobBoardPlatform.objects.count()
        ctx["enabled_count"] = JobBoardPlatform.objects.filter(is_enabled=True).count()
        return ctx


class PlatformCreateView(SuperuserRequiredMixin, CreateView):
    model = JobBoardPlatform
    form_class = JobBoardPlatformForm
    template_name = "harvest/platform_form.html"
    success_url = reverse_lazy("harvest-platforms")

    def form_valid(self, form):
        messages.success(self.request, f"Platform '{form.instance.name}' created successfully.")
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, "Please fix the errors below.")
        return super().form_invalid(form)


class PlatformUpdateView(SuperuserRequiredMixin, UpdateView):
    model = JobBoardPlatform
    form_class = JobBoardPlatformForm
    template_name = "harvest/platform_form.html"
    success_url = reverse_lazy("harvest-platforms")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["is_edit"] = True
        return ctx

    def form_valid(self, form):
        messages.success(self.request, f"Platform '{form.instance.name}' updated.")
        return super().form_valid(form)


class PlatformDeleteView(SuperuserRequiredMixin, View):
    def post(self, request, pk):
        platform = get_object_or_404(JobBoardPlatform, pk=pk)
        name = platform.name
        platform.delete()
        messages.success(request, f"Platform '{name}' deleted.")
        return redirect("harvest-platforms")


class PlatformToggleView(SuperuserRequiredMixin, View):
    def post(self, request, pk):
        platform = get_object_or_404(JobBoardPlatform, pk=pk)
        platform.is_enabled = not platform.is_enabled
        platform.save(update_fields=["is_enabled"])
        return JsonResponse({"enabled": platform.is_enabled, "name": platform.name})


# ── Schedule Config ────────────────────────────────────────────────────────────

class ScheduleConfigView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/settings_schedule.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "schedule"
        try:
            from django_celery_beat.models import PeriodicTask
            ctx["periodic_tasks"] = PeriodicTask.objects.filter(
                Q(name__icontains="harvest") | Q(name__icontains="detect") | Q(name__icontains="cleanup")
            ).order_by("name")
        except Exception:
            ctx["periodic_tasks"] = []
        return ctx


# ── Run Monitor ────────────────────────────────────────────────────────────────

class RunMonitorView(SuperuserRequiredMixin, ListView):
    template_name = "harvest/settings_monitor.html"
    context_object_name = "runs"
    paginate_by = 30

    def get_queryset(self):
        return HarvestRun.objects.select_related("platform", "triggered_user").order_by("-started_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "monitor"
        ctx["platforms"] = JobBoardPlatform.objects.filter(is_enabled=True)
        ctx["total_harvested"] = HarvestedJob.objects.filter(is_active=True).count()
        ctx["pending_sync"] = HarvestedJob.objects.filter(sync_status="PENDING").count()
        ctx["synced_count"] = HarvestedJob.objects.filter(sync_status="SYNCED").count()
        ctx["total_runs"] = HarvestRun.objects.count()
        ctx["running_runs"] = HarvestRun.objects.filter(status="RUNNING").count()
        return ctx


# ── Company Labels ─────────────────────────────────────────────────────────────

class CompanyLabelListView(SuperuserRequiredMixin, ListView):
    template_name = "harvest/settings_labels.html"
    context_object_name = "labels"
    paginate_by = 50

    def get_queryset(self):
        qs = CompanyPlatformLabel.objects.select_related(
            "company", "platform", "verified_by"
        ).order_by("company__name")

        platform_f = self.request.GET.get("platform", "").strip()
        if platform_f:
            qs = qs.filter(platform__slug=platform_f)

        confidence_f = self.request.GET.get("confidence", "").strip()
        if confidence_f:
            qs = qs.filter(confidence=confidence_f)

        method_f = self.request.GET.get("method", "").strip()
        if method_f:
            qs = qs.filter(detection_method=method_f)

        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(company__name__icontains=q)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "labels"
        ctx["platforms"] = JobBoardPlatform.objects.annotate(
            company_count=Count("labels")
        ).order_by("name")
        from companies.models import Company

        ctx["stat_labeled"] = CompanyPlatformLabel.objects.exclude(
            detection_method="UNDETECTED"
        ).count()
        ctx["stat_undetected"] = CompanyPlatformLabel.objects.filter(
            detection_method="UNDETECTED"
        ).count()
        ctx["stat_unlabeled"] = Company.objects.exclude(
            platform_label__isnull=False
        ).count()
        ctx["stat_verified"] = CompanyPlatformLabel.objects.filter(is_verified=True).count()
        ctx["confidence_choices"] = CompanyPlatformLabel.Confidence.choices
        ctx["method_choices"] = CompanyPlatformLabel.DetectionMethod.choices
        ctx["selected_platform"] = self.request.GET.get("platform", "")
        ctx["selected_confidence"] = self.request.GET.get("confidence", "")
        ctx["selected_method"] = self.request.GET.get("method", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class LabelVerifyView(SuperuserRequiredMixin, View):
    def post(self, request, pk):
        label = get_object_or_404(CompanyPlatformLabel, pk=pk)
        label.is_verified = not label.is_verified
        label.verified_by = request.user if label.is_verified else None
        label.verified_at = timezone.now() if label.is_verified else None
        label.save(update_fields=["is_verified", "verified_by", "verified_at"])
        return JsonResponse({"verified": label.is_verified})


class LabelManualSetView(SuperuserRequiredMixin, View):
    def post(self, request, pk):
        label = get_object_or_404(CompanyPlatformLabel, pk=pk)
        platform_id = request.POST.get("platform_id", "")
        platform = get_object_or_404(JobBoardPlatform, pk=platform_id) if platform_id else None
        label.platform = platform
        label.detection_method = "MANUAL"
        label.confidence = "HIGH"
        label.is_verified = True
        label.verified_by = request.user
        label.verified_at = timezone.now()
        label.save()
        messages.success(
            request,
            f"Set {label.company.name} to {platform.name if platform else 'None'}",
        )
        return redirect(request.META.get("HTTP_REFERER", "harvest-labels"))


# ── Trigger Actions ────────────────────────────────────────────────────────────

class RunDetectNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import detect_company_platforms_task
        task = detect_company_platforms_task.delay(
            batch_size=200,
            triggered_user_id=request.user.id,
        )
        messages.success(
            request,
            "Platform detection is running on the server. "
            f"Refresh Run Monitor to see progress (task {task.id[:8]}…). "
            "Switching tabs does not stop this job.",
        )
        return redirect_with_task_progress(
            "harvest-monitor",
            task.id,
            "Platform detection",
        )


class RunHarvestNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import harvest_jobs_task
        platform_slug = request.POST.get("platform_slug", "").strip() or None
        task = harvest_jobs_task.delay(
            platform_slug=platform_slug,
            triggered_by="MANUAL",
            triggered_user_id=request.user.id,
        )
        label = platform_slug or "all platforms"
        messages.success(
            request,
            f"Harvest for {label} is running on the server (task {task.id[:8]}…). "
            "Refresh Run Monitor for results; switching tabs does not cancel work.",
        )
        return redirect_with_task_progress(
            "harvest-monitor",
            task.id,
            f"Harvest ({label})",
        )


class RunSyncNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import sync_harvested_to_pool_task
        task = sync_harvested_to_pool_task.delay()
        messages.success(request, f"Sync to job pool started (Task: {task.id[:8]}...)")
        return redirect_with_task_progress("harvest-monitor", task.id, "Sync to job pool")


class RunCleanupNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import cleanup_harvested_jobs_task
        task = cleanup_harvested_jobs_task.delay()
        messages.success(request, f"Cleanup started (Task: {task.id[:8]}...)")
        return redirect_with_task_progress("harvest-monitor", task.id, "Harvest cleanup")

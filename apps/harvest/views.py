import json
import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView, View

from core.http import redirect_with_task_progress

from .forms import JobBoardPlatformForm
from .models import (
    CompanyFetchRun,
    CompanyPlatformLabel,
    FetchBatch,
    HarvestRun,
    HarvestedJob,
    JobBoardPlatform,
    RawJob,
)

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
    paginate_by = 100

    def get_queryset(self):
        qs = CompanyPlatformLabel.objects.select_related(
            "company", "platform", "verified_by"
        ).order_by("company__name")

        platform_f = self.request.GET.get("platform", "").strip()
        if platform_f == "UNDETECTED":
            qs = qs.filter(detection_method="UNDETECTED")
        elif platform_f:
            qs = qs.filter(platform__slug=platform_f)

        confidence_f = self.request.GET.get("confidence", "").strip()
        if confidence_f:
            qs = qs.filter(confidence=confidence_f)

        method_f = self.request.GET.get("method", "").strip()
        if method_f:
            qs = qs.filter(detection_method=method_f)

        status_f = self.request.GET.get("status", "").strip()
        if status_f == "verified":
            qs = qs.filter(portal_alive=True)
        elif status_f == "down":
            qs = qs.filter(portal_alive=False)
        elif status_f == "unchecked":
            qs = qs.filter(portal_alive__isnull=True, platform__isnull=False)
        elif status_f == "no_tenant":
            qs = qs.filter(platform__isnull=False, tenant_id="")
        elif status_f == "no_ats":
            qs = qs.filter(detection_method="UNDETECTED")

        verified_f = self.request.GET.get("verified", "").strip()
        if verified_f == "yes":
            qs = qs.filter(is_verified=True)
        elif verified_f == "no":
            qs = qs.filter(is_verified=False)

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
        ctx["platforms_chart"] = JobBoardPlatform.objects.annotate(
            company_count=Count("labels")
        ).filter(company_count__gt=0).order_by("-company_count")
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
        ctx["stat_live"] = CompanyPlatformLabel.objects.filter(portal_alive=True).count()
        ctx["stat_down"] = CompanyPlatformLabel.objects.filter(portal_alive=False).count()
        ctx["confidence_choices"] = CompanyPlatformLabel.Confidence.choices
        ctx["method_choices"] = CompanyPlatformLabel.DetectionMethod.choices
        ctx["selected_platform"] = self.request.GET.get("platform", "")
        ctx["selected_confidence"] = self.request.GET.get("confidence", "")
        ctx["selected_method"] = self.request.GET.get("method", "")
        ctx["selected_status"] = self.request.GET.get("status", "")
        ctx["selected_verified"] = self.request.GET.get("verified", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class LabelVerifyView(SuperuserRequiredMixin, View):
    """Toggle verified status — returns JSON for AJAX or redirects for plain POST."""
    def post(self, request, pk):
        label = get_object_or_404(CompanyPlatformLabel, pk=pk)
        label.is_verified = not label.is_verified
        label.verified_by = request.user if label.is_verified else None
        label.verified_at = timezone.now() if label.is_verified else None
        label.save(update_fields=["is_verified", "verified_by", "verified_at"])
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"verified": label.is_verified, "pk": pk})
        return redirect(request.META.get("HTTP_REFERER") or "harvest-labels")


class LabelManualSetView(SuperuserRequiredMixin, View):
    """Set platform + optional tenant for a label — returns JSON for AJAX."""
    def post(self, request, pk):
        label = get_object_or_404(CompanyPlatformLabel, pk=pk)
        platform_id = request.POST.get("platform_id", "").strip()
        tenant_id = request.POST.get("tenant_id", "").strip()
        platform = None
        if platform_id:
            platform = get_object_or_404(JobBoardPlatform, pk=platform_id)
        label.platform = platform
        label.detection_method = "MANUAL"
        label.confidence = "HIGH"
        label.is_verified = True
        label.verified_by = request.user
        label.verified_at = timezone.now()
        if tenant_id:
            label.tenant_id = tenant_id
        label.save()
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            from .career_url import build_career_url
            url = build_career_url(platform.slug if platform else "", label.tenant_id)
            return JsonResponse({
                "ok": True,
                "pk": pk,
                "platform_name": platform.name if platform else "",
                "platform_color": platform.color_hex if platform else "#6B7280",
                "tenant_id": label.tenant_id,
                "career_url": url,
                "scrape_status": label.scrape_status,
            })
        messages.success(
            request,
            f"Set {label.company.name} → {platform.name if platform else 'None'}",
        )
        return redirect(request.META.get("HTTP_REFERER") or "harvest-labels")


class LabelUpdateTenantView(SuperuserRequiredMixin, View):
    """Inline update of tenant_id only — AJAX only."""
    def post(self, request, pk):
        label = get_object_or_404(CompanyPlatformLabel, pk=pk)
        tenant_id = request.POST.get("tenant_id", "").strip()
        label.tenant_id = tenant_id
        label.portal_alive = None   # reset health — needs re-check
        label.portal_last_verified = None
        label.save(update_fields=["tenant_id", "portal_alive", "portal_last_verified"])
        from .career_url import build_career_url
        url = build_career_url(label.platform.slug if label.platform else "", tenant_id)
        return JsonResponse({
            "ok": True,
            "pk": pk,
            "tenant_id": tenant_id,
            "career_url": url,
            "scrape_status": label.scrape_status,
        })


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


class RunBackfillNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import backfill_platform_labels_from_jobs_task
        task = backfill_platform_labels_from_jobs_task.delay()
        messages.success(request, f"Backfill started — scanning all job URLs to detect platforms (Task: {task.id[:8]}...)")
        return redirect_with_task_progress("harvest-labels", task.id, "Platform backfill from job URLs")


class RunVerifyPortalsView(SuperuserRequiredMixin, View):
    """Queue async HTTP health checks for all career portal URLs."""
    def post(self, request):
        from .tasks import verify_all_portals_task
        task = verify_all_portals_task.delay()
        messages.success(
            request,
            f"Portal verification started — checking all career URLs in the background (Task: {task.id[:8]}...)"
        )
        return redirect_with_task_progress("harvest-labels", task.id, "Verifying career portal health")


# ── Raw Jobs Views ─────────────────────────────────────────────────────────────

class RawJobListView(SuperuserRequiredMixin, ListView):
    model = RawJob
    template_name = "harvest/rawjobs_list.html"
    context_object_name = "jobs"
    paginate_by = 100

    def get_queryset(self):
        qs = RawJob.objects.select_related("company", "job_platform").order_by("-fetched_at")

        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q) | Q(company_name__icontains=q)
            )

        platform_f = self.request.GET.get("platform", "").strip()
        if platform_f:
            qs = qs.filter(platform_slug=platform_f)

        location_f = self.request.GET.get("location_type", "").strip()
        if location_f:
            qs = qs.filter(location_type=location_f)

        employment_f = self.request.GET.get("employment_type", "").strip()
        if employment_f:
            qs = qs.filter(employment_type=employment_f)

        exp_f = self.request.GET.get("experience_level", "").strip()
        if exp_f:
            qs = qs.filter(experience_level=exp_f)

        sync_f = self.request.GET.get("sync_status", "").strip()
        if sync_f:
            qs = qs.filter(sync_status=sync_f)

        remote_f = self.request.GET.get("is_remote", "").strip()
        if remote_f == "1":
            qs = qs.filter(is_remote=True)
        elif remote_f == "0":
            qs = qs.filter(is_remote=False)

        date_from = self.request.GET.get("date_from", "").strip()
        if date_from:
            qs = qs.filter(posted_date__gte=date_from)

        date_to = self.request.GET.get("date_to", "").strip()
        if date_to:
            qs = qs.filter(posted_date__lte=date_to)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "rawjobs"

        # Stats
        ctx["total_jobs"] = RawJob.objects.count()
        ctx["active_jobs"] = RawJob.objects.filter(is_active=True).count()
        ctx["remote_jobs"] = RawJob.objects.filter(is_remote=True).count()
        ctx["synced_jobs"] = RawJob.objects.filter(sync_status="SYNCED").count()
        ctx["pending_jobs"] = RawJob.objects.filter(sync_status="PENDING").count()
        ctx["failed_jobs"] = RawJob.objects.filter(sync_status="FAILED").count()

        from django.utils.timezone import now
        from datetime import timedelta
        today = now().date()
        ctx["new_today"] = RawJob.objects.filter(fetched_at__date=today).count()

        # Platform breakdown
        ctx["platform_stats"] = (
            RawJob.objects.values("platform_slug")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        # Recent batches
        ctx["recent_batches"] = FetchBatch.objects.order_by("-created_at")[:5]

        # Platforms list for filter dropdown
        ctx["platforms"] = JobBoardPlatform.objects.filter(is_enabled=True).order_by("name")

        # Choices for filter dropdowns
        ctx["location_type_choices"] = RawJob.LocationType.choices
        ctx["employment_type_choices"] = RawJob.EmploymentType.choices
        ctx["experience_level_choices"] = RawJob.ExperienceLevel.choices
        ctx["sync_status_choices"] = RawJob.SyncStatus.choices

        # Filter state
        ctx["q"] = self.request.GET.get("q", "")
        ctx["selected_platform"] = self.request.GET.get("platform", "")
        ctx["selected_location_type"] = self.request.GET.get("location_type", "")
        ctx["selected_employment_type"] = self.request.GET.get("employment_type", "")
        ctx["selected_experience_level"] = self.request.GET.get("experience_level", "")
        ctx["selected_sync_status"] = self.request.GET.get("sync_status", "")
        ctx["selected_is_remote"] = self.request.GET.get("is_remote", "")
        ctx["selected_date_from"] = self.request.GET.get("date_from", "")
        ctx["selected_date_to"] = self.request.GET.get("date_to", "")

        # Running batch check (for live polling)
        ctx["has_running_batch"] = FetchBatch.objects.filter(status="RUNNING").exists()

        return ctx


class RawJobDetailView(SuperuserRequiredMixin, DetailView):
    model = RawJob
    template_name = "harvest/rawjob_detail.html"
    context_object_name = "job"

    def get_queryset(self):
        return RawJob.objects.select_related("company", "job_platform", "platform_label")


class FetchBatchListView(SuperuserRequiredMixin, ListView):
    model = FetchBatch
    template_name = "harvest/rawjobs_batches.html"
    context_object_name = "batches"
    paginate_by = 20

    def get_queryset(self):
        return FetchBatch.objects.prefetch_related("company_runs").order_by("-created_at")

    def get(self, request, *args, **kwargs):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            qs = self.get_queryset()[:50]
            batches = []
            for b in qs:
                batches.append({
                    "id": b.pk,
                    "name": b.name,
                    "status": b.status,
                    "platform_filter": b.platform_filter,
                    "total": b.total_companies,
                    "completed": b.completed_companies,
                    "failed": b.failed_companies,
                    "total_jobs_found": b.total_jobs_found,
                    "total_jobs_new": b.total_jobs_new,
                    "progress_pct": b.progress_pct,
                    "created_at": b.created_at.strftime("%Y-%m-%d %H:%M") if b.created_at else "",
                })
            return JsonResponse({"batches": batches})
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "rawjobs"
        return ctx


class CompanyFetchStatusView(SuperuserRequiredMixin, ListView):
    template_name = "harvest/rawjobs_company_status.html"
    context_object_name = "runs"
    paginate_by = 50

    def get_queryset(self):
        qs = CompanyFetchRun.objects.select_related(
            "label__company", "label__platform", "batch"
        ).order_by("-started_at")

        status_f = self.request.GET.get("status", "").strip()
        if status_f:
            qs = qs.filter(status=status_f)

        platform_f = self.request.GET.get("platform", "").strip()
        if platform_f:
            qs = qs.filter(label__platform__slug=platform_f)

        return qs

    def get(self, request, *args, **kwargs):
        # JSON response for AJAX calls from the rawjobs_list template
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            qs = self.get_queryset()[:100]
            runs = []
            for run in qs:
                runs.append({
                    "label_pk": run.label_id,
                    "company_name": run.label.company.name if run.label and run.label.company else "",
                    "platform_slug": run.label.platform.slug if run.label and run.label.platform else "",
                    "status": run.status,
                    "jobs_found": run.jobs_found,
                    "jobs_new": run.jobs_new,
                    "started_at": run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "",
                })
            return JsonResponse({"runs": runs})
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "rawjobs"
        ctx["status_choices"] = CompanyFetchRun.Status.choices
        ctx["platforms"] = JobBoardPlatform.objects.filter(is_enabled=True).order_by("name")
        ctx["selected_status"] = self.request.GET.get("status", "")
        ctx["selected_platform"] = self.request.GET.get("platform", "")
        return ctx


class TriggerCompanyFetchView(SuperuserRequiredMixin, View):
    """AJAX POST — triggers a single-company raw job fetch."""
    def post(self, request):
        from .tasks import fetch_raw_jobs_for_company_task
        label_pk = request.POST.get("label_pk", "").strip()
        if not label_pk:
            return JsonResponse({"ok": False, "error": "Missing label_pk"}, status=400)
        try:
            label_pk = int(label_pk)
        except ValueError:
            return JsonResponse({"ok": False, "error": "Invalid label_pk"}, status=400)

        task = fetch_raw_jobs_for_company_task.delay(label_pk, None, "MANUAL")
        return JsonResponse({"ok": True, "task_id": task.id})


class TriggerBatchFetchView(SuperuserRequiredMixin, View):
    """POST — triggers a batch raw job fetch for all or filtered companies."""
    def post(self, request):
        from .tasks import fetch_raw_jobs_batch_task
        platform_slug = request.POST.get("platform_slug", "").strip() or None
        batch_name = request.POST.get("batch_name", "").strip() or None
        test_mode = request.POST.get("test_mode", "") in ("1", "true", "True", "yes")
        test_max_jobs = int(request.POST.get("test_max_jobs", "10") or "10")
        task = fetch_raw_jobs_batch_task.delay(
            platform_slug=platform_slug,
            batch_name=batch_name,
            triggered_user_id=request.user.id,
            test_mode=test_mode,
            test_max_jobs=test_max_jobs,
        )
        if test_mode:
            messages.success(
                request,
                f"Test fetch started — 1 company per platform, up to {test_max_jobs} jobs each (Task: {task.id[:8]}…)",
            )
            return redirect_with_task_progress("harvest-rawjobs", task.id, f"Test fetch ({test_max_jobs} jobs/platform)")
        messages.success(
            request,
            f"Raw jobs batch fetch started"
            + (f" for platform '{platform_slug}'" if platform_slug else " for all platforms")
            + f" (Task: {task.id[:8]}...)",
        )
        return redirect_with_task_progress("harvest-rawjobs", task.id, "Raw jobs batch fetch")


class StopBatchView(SuperuserRequiredMixin, View):
    """POST — cancels the running (or a specific) FetchBatch and revokes pending Celery tasks."""

    def post(self, request):
        from celery import current_app

        batch_id = request.POST.get("batch_id") or None
        if batch_id:
            batch = get_object_or_404(FetchBatch, pk=batch_id)
        else:
            batch = FetchBatch.objects.filter(status="RUNNING").order_by("-created_at").first()

        if not batch:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": "No running batch found."}, status=404)
            messages.warning(request, "No running batch found.")
            return redirect("harvest-rawjobs")

        if batch.status not in ("RUNNING", "PENDING"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": f"Batch is already {batch.status}."})
            messages.warning(request, f"Batch #{batch.pk} is already {batch.status}.")
            return redirect("harvest-rawjobs")

        # 1. Revoke the main batch orchestration task (if it's still queued/running)
        if batch.task_id:
            current_app.control.revoke(batch.task_id, terminate=True, signal="SIGTERM")

        # 2. Revoke all PENDING/RUNNING per-company tasks for this batch
        pending_runs = CompanyFetchRun.objects.filter(
            batch=batch, status__in=["PENDING", "RUNNING"]
        ).exclude(task_id="").exclude(task_id=None)
        task_ids = list(pending_runs.values_list("task_id", flat=True))
        if task_ids:
            current_app.control.revoke(task_ids, terminate=True, signal="SIGTERM")

        # 3. Mark company runs as SKIPPED
        pending_runs.update(status="SKIPPED")

        # 4. Mark batch as CANCELLED
        batch.status = "CANCELLED"
        if not batch.completed_at:
            batch.completed_at = timezone.now()
        batch.save(update_fields=["status", "completed_at"])

        logger.info(
            "[HARVEST] Batch #%s cancelled by %s — revoked %d task(s)",
            batch.pk, request.user.username, len(task_ids) + (1 if batch.task_id else 0),
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "batch_id": batch.pk, "revoked": len(task_ids)})

        messages.success(request, f"Batch #{batch.pk} cancelled — {len(task_ids)} pending tasks revoked.")
        return redirect("harvest-rawjobs")


class RawJobStatsView(SuperuserRequiredMixin, View):
    """JSON endpoint — live stats for dashboard polling."""
    def get(self, request):
        from django.utils.timezone import now
        today = now().date()

        # Running batch info
        running_batch = FetchBatch.objects.filter(status="RUNNING").order_by("-created_at").first()
        batch_data = None
        if running_batch:
            batch_data = {
                "id": running_batch.pk,
                "name": running_batch.name,
                "total": running_batch.total_companies,
                "completed": running_batch.completed_companies,
                "failed": running_batch.failed_companies,
                "progress_pct": running_batch.progress_pct,
                "total_jobs_found": running_batch.total_jobs_found,
                "total_jobs_new": running_batch.total_jobs_new,
            }

        return JsonResponse({
            "total_jobs": RawJob.objects.count(),
            "active_jobs": RawJob.objects.filter(is_active=True).count(),
            "remote_jobs": RawJob.objects.filter(is_remote=True).count(),
            "synced_jobs": RawJob.objects.filter(sync_status="SYNCED").count(),
            "pending_jobs": RawJob.objects.filter(sync_status="PENDING").count(),
            "failed_jobs": RawJob.objects.filter(sync_status="FAILED").count(),
            "new_today": RawJob.objects.filter(fetched_at__date=today).count(),
            "running_batch": batch_data,
            "platform_stats": list(
                RawJob.objects.values("platform_slug")
                .annotate(count=Count("id"))
                .order_by("-count")
                .values("platform_slug", "count")
            ),
        })

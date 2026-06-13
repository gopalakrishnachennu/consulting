import json
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, F, Q, Sum, Value, Max, Min
from django.db.models.functions import Coalesce, Length, Trim
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from datetime import timedelta

from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView, View

from core.http import redirect_with_task_progress
from users.models import User

from .forms import HarvestRoleCategoryForm, JobBoardPlatformForm, JobDomainForm
from .models import (
    CompanyFetchRun,
    CompanyPlatformLabel,
    DuplicateLabel,
    DuplicateResolution,
    FetchBatch,
    HarvestEngineConfig,
    HarvestFilterSnapshot,
    HarvestOpsRun,
    HarvestRoleCategory,
    JobDomain,
    HarvestSkippedTitle,
    JobBoardPlatform,
    RawJob,
    RawJobDuplicatePair,
)
from .platform_engine import harvester_class_name_for_slug, kind_for_slug
from .resume_profile import build_resume_job_profile
from .jd_gate import evaluate_raw_job_resume_gate
from .enrichments import infer_country_from_location
from .services.pipeline_snapshot import (
    load_rawjobs_dashboard_stats as _svc_load_rawjobs_dashboard_stats,
    raw_jobs_missing_description_count as _svc_raw_jobs_missing_description_count,
    raw_jobs_missing_jd_expired_count as _svc_raw_jobs_missing_jd_expired_count,
    raw_jobs_workflow_insights as _svc_raw_jobs_workflow_insights,
)
from .services.rawjob_query import (
    apply_rawjob_filters as _svc_apply_rawjob_filters,
    effective_classification_q as _svc_effective_classification_q,
    production_rawjobs_queryset as _svc_production_rawjobs_queryset,
    ready_stage_q as _svc_ready_stage_q,
    rawjob_filter_state as _svc_rawjob_filter_state,
)

logger = logging.getLogger(__name__)


_FULL_CRAWL_COOLDOWN_HOURS = 2  # fallback; actual value from HarvestEngineConfig
_FULL_CRAWL_LOCK_KEY = "harvest:full_crawl:cooldown_lock"  # cache-layer enforcement


def _effective_classification_q(min_conf: float = 0.01) -> Q:
    """Backward-compatible wrapper to shared query service."""
    return _svc_effective_classification_q(min_conf=min_conf)


def _ready_stage_q(min_conf: float | None = None) -> Q:
    """Backward-compatible wrapper to shared query service."""
    return _svc_ready_stage_q(min_conf=min_conf)


def _find_existing_live_job_for_rawjob(raw_job):
    """Find existing non-archived Job mapped to a RawJob URL hash/link."""
    from jobs.dedup import find_existing_job_by_url
    from jobs.models import Job

    if raw_job.url_hash:
        by_hash = Job.objects.filter(url_hash=raw_job.url_hash, is_archived=False).first()
        if by_hash:
            return by_hash
    if raw_job.original_url:
        by_url = find_existing_job_by_url(raw_job.original_url)
        if by_url and not by_url.is_archived:
            return by_url
        by_link = Job.objects.filter(original_link=raw_job.original_url, is_archived=False).first()
        if by_link:
            return by_link
    return None


def _build_rawjob_ops_timeline(raw_job: RawJob) -> list[dict]:
    """Small unified timeline for raw-job debugging."""
    from jobs.models import Job, PipelineEvent

    def item(at, kind, title, status="", body="", tone="slate") -> dict:
        return {
            "at": at,
            "kind": kind,
            "title": title,
            "status": status,
            "body": body,
            "tone": tone,
        }

    gate = raw_job.resume_jd_gate()
    events: list[dict] = [
        item(
            raw_job.updated_at or raw_job.fetched_at,
            "RawJob",
            "Current raw job state",
            raw_job.sync_status,
            f"Scope: {raw_job.scope_status or 'unknown'} · Gate: {gate.get('reason_code', 'unknown')}",
            "green" if raw_job.sync_status == RawJob.SyncStatus.SYNCED else "slate",
        )
    ]

    job_q = Q()
    if raw_job.pk:
        job_q |= Q(source_raw_job_id=raw_job.pk)
    if raw_job.url_hash:
        job_q |= Q(url_hash=raw_job.url_hash)
    linked_jobs = list(Job.objects.filter(job_q).order_by("-updated_at")[:10]) if job_q else []
    linked_job_ids = [job.pk for job in linked_jobs]
    for job in linked_jobs:
        events.append(item(
            getattr(job, "updated_at", None) or getattr(job, "created_at", None),
            "Job",
            "Synced vet/pool job row",
            getattr(job, "status", "") or getattr(job, "stage", ""),
            f"Job #{job.pk}: {job.title}",
            "green",
        ))

    event_q = Q(pk__in=[])
    if raw_job.url_hash:
        event_q |= Q(url_hash=raw_job.url_hash)
    if linked_job_ids:
        event_q |= Q(job_id__in=linked_job_ids)
    for ev in PipelineEvent.objects.filter(event_q).order_by("-occurred_at")[:20]:
        title = ev.task_name or "Pipeline event"
        stage = f"{ev.from_stage or '-'} -> {ev.to_stage or '-'}"
        body = ev.error or stage
        tone = "red" if ev.status == PipelineEvent.Status.FAILED else (
            "yellow" if ev.status == PipelineEvent.Status.SKIPPED else "green"
        )
        events.append(item(ev.occurred_at, "PipelineEvent", title, ev.status, body, tone))

    if raw_job.platform_label_id:
        runs = CompanyFetchRun.objects.filter(label_id=raw_job.platform_label_id).order_by("-started_at")[:8]
        for run in runs:
            body = (
                f"Found {run.jobs_found:,} · new {run.jobs_new:,} · "
                f"updated {run.jobs_updated:,} · dup {run.jobs_duplicate:,}"
            )
            if run.is_test_run:
                body += " · test/capped run"
            tone = "red" if run.status == CompanyFetchRun.Status.FAILED else (
                "yellow" if run.status in {CompanyFetchRun.Status.PARTIAL, CompanyFetchRun.Status.EMPTY} else "green"
            )
            events.append(item(run.completed_at or run.started_at, "CompanyFetchRun", "Company fetch run", run.status, body, tone))

    for snapshot in raw_job.payload_snapshots.all()[:8]:
        events.append(item(
            snapshot.captured_at,
            "Payload",
            snapshot.get_payload_kind_display(),
            "failure" if snapshot.is_failure else snapshot.size_label,
            f"{snapshot.platform_slug or raw_job.platform_slug} · hash {snapshot.content_hash[:10]}",
            "red" if snapshot.is_failure else "blue",
        ))

    pairs = RawJobDuplicatePair.objects.filter(
        Q(primary_id=raw_job.pk) | Q(duplicate_id=raw_job.pk)
    ).select_related("primary", "duplicate").order_by("-detected_at")[:8]
    for pair in pairs:
        other = pair.duplicate if pair.primary_id == raw_job.pk else pair.primary
        events.append(item(
            pair.detected_at,
            "Duplicate",
            pair.get_label_display(),
            pair.resolution,
            f"Matched RawJob #{other.pk} at {pair.similarity:.2f} via {pair.method or 'detector'}",
            "yellow",
        ))

    events.sort(key=lambda row: row["at"].timestamp() if row.get("at") else 0, reverse=True)
    return events[:40]


def _invalidate_rawjobs_dashboard_cache() -> None:
    """Best-effort cache bust for Raw Jobs KPI cards."""
    try:
        cache.delete("rawjobs_dashboard_stats")
        cache.delete("rawjobs_expired_missing_jd")
        for h in (3, 6, 12, 24):
            cache.delete(f"rawjobs_workflow_insights_{h}")
    except Exception:
        pass


def _load_rawjobs_dashboard_stats(*, force_refresh: bool = False) -> dict:
    """Backward-compatible wrapper to shared snapshot service."""
    return _svc_load_rawjobs_dashboard_stats(force_refresh=force_refresh)


def _sync_rawjob_to_pool(raw_job, *, posted_by):
    """
    Sync one RawJob into Job pool (same mapping as bulk sync task).

    Returns ``(job, created_new)``.
    """
    from django.utils import timezone as _tz
    from jobs.models import Job
    from jobs.quality import compute_quality_score
    from jobs.gating import apply_gate_result_to_job, evaluate_raw_job_gate
    from .url_health import check_job_posting_live, is_definitive_inactive

    existing = _find_existing_live_job_for_rawjob(raw_job)
    if existing:
        if raw_job.sync_status != "SYNCED":
            raw_job.sync_status = "SKIPPED"
            raw_job.save(update_fields=["sync_status", "updated_at"])
            _invalidate_rawjobs_dashboard_cache()
        return existing, False

    # Last-mile link-health check before any promotion.
    if (raw_job.original_url or "").strip():
        live = check_job_posting_live(
            raw_job.original_url,
            platform_slug=(raw_job.platform_slug or ""),
        )
        if not live.is_live and is_definitive_inactive(live):
            payload = dict(raw_job.raw_payload or {})
            payload["link_health"] = {
                "is_live": False,
                "reason": live.reason,
                "status_code": live.status_code,
                "checked_at": _tz.now().isoformat(),
                "final_url": live.final_url,
                "decisive": True,
            }
            raw_job.is_active = False
            raw_job.raw_payload = payload
            raw_job.save(update_fields=["is_active", "raw_payload", "updated_at"])
            _invalidate_rawjobs_dashboard_cache()
            raise ValueError(
                f"Cannot promote to vet queue: posting appears inactive ({live.reason})."
            )

    gate = evaluate_raw_job_gate(raw_job)
    if not gate.passed:
        raise ValueError(
            f"Cannot promote to vet queue: blocked by gate ({gate.reason_code}). "
            f"Reasons: {', '.join(gate.reasons[:3])}"
        )

    platform_slug = raw_job.platform_slug or (raw_job.job_platform.slug if raw_job.job_platform else "")
    job_location = " | ".join(raw_job.location_candidates or []) or raw_job.location_raw or ""
    job_country = raw_job.country or ((raw_job.country_codes or [""])[0] if raw_job.country_codes else "")
    with transaction.atomic():
        job = Job.objects.create(
            title=raw_job.title,
            company=raw_job.company_name or (raw_job.company.name if raw_job.company else ""),
            company_obj=raw_job.company,
            location=job_location,
            description=raw_job.description or raw_job.title,
            original_link=raw_job.original_url,
            salary_range=raw_job.salary_raw or "",
            job_type=raw_job.employment_type if raw_job.employment_type != "UNKNOWN" else "FULL_TIME",
            status="POOL",
            stage=Job.Stage.VETTED,
            stage_changed_at=_tz.now(),
            url_hash=raw_job.url_hash or "",
            job_source=f"HARVESTED_{platform_slug.upper()}" if platform_slug else "HARVESTED",
            posted_by=posted_by,
            source_raw_job=raw_job,
            queue_entered_at=_tz.now(),
            country=job_country,
            department=raw_job.department_normalized or "",
        )
        apply_gate_result_to_job(job, gate)
        job.quality_score = compute_quality_score(job)
        job.validation_score = int(round(gate.vet_priority_score * 100))
        job.validation_result = {
            "score": job.validation_score,
            "lane": gate.lane,
            "gate_status": gate.status,
            "reason_code": gate.reason_code,
            "reasons": gate.reasons,
            "checks": gate.checks,
            "multi_score": {
                "data_quality": gate.data_quality_score,
                "trust": gate.trust_score,
                "candidate_fit": gate.candidate_fit_score,
                "vet_priority": gate.vet_priority_score,
            },
        }
        job.validation_run_at = _tz.now()
        job.gate_checked_at = _tz.now()
        job.save(
            update_fields=[
                "hard_gate_passed", "gate_status", "vet_lane",
                "pipeline_reason_code", "pipeline_reason_detail",
                "hard_gate_failures", "hard_gate_checks",
                "data_quality_score", "trust_score", "candidate_fit_score", "vet_priority_score",
                "quality_score", "validation_score", "validation_result",
                "validation_run_at", "gate_checked_at",
            ]
        )
        payload = dict(raw_job.raw_payload or {})
        payload["vet_gate"] = {
            "status": "eligible",
            "lane": gate.lane,
            "reason_code": gate.reason_code,
            "checks": gate.checks,
            "scores": {
                "data_quality": gate.data_quality_score,
                "trust": gate.trust_score,
                "candidate_fit": gate.candidate_fit_score,
                "vet_priority": gate.vet_priority_score,
            },
            "job_id": job.pk,
            "checked_at": _tz.now().isoformat(),
        }
        from jobs.marketing_role_routing import assign_marketing_roles_to_job

        assign_marketing_roles_to_job(job, raw_job=raw_job)
        raw_job.sync_status = "SYNCED"
        raw_job.raw_payload = payload
        raw_job.save(update_fields=["sync_status", "raw_payload", "updated_at"])
    _invalidate_rawjobs_dashboard_cache()
    return job, True


def _full_crawl_cooldown_minutes() -> int:
    """Return configured full-crawl cooldown in minutes (from HarvestEngineConfig)."""
    try:
        from .models import HarvestEngineConfig
        return int(HarvestEngineConfig.get().full_fetch_cooldown_minutes)
    except Exception:
        return int(_FULL_CRAWL_COOLDOWN_HOURS * 60)


def _full_crawl_cooldown_ctx() -> dict:
    """Return last_full_batch + cooldown_remaining_sec for any view that shows the Full Crawl button."""
    last_full_batch = (
        FetchBatch.objects.filter(status__in=["COMPLETED", "PARTIAL", "RUNNING", "CANCELLED"])
        .exclude(name__icontains="PLATFORM CHECK")
        .exclude(name__icontains="QUICK SYNC")
        .order_by("-created_at")
        .first()
    )
    cooldown_secs = _full_crawl_cooldown_minutes() * 60
    cooldown_remaining_sec = 0
    if last_full_batch:
        elapsed = (timezone.now() - last_full_batch.created_at).total_seconds()
        cooldown_remaining_sec = max(0, int(cooldown_secs - elapsed))
    return {"last_full_batch": last_full_batch, "cooldown_remaining_sec": cooldown_remaining_sec}


_ALLOWED_RETURN_VIEWS = {
    "harvest-rawjobs",
    "jobs-pipeline",
    "ops-center",
    "harvest-labels",
    "harvest-schedule",
}


def _resolve_return_target(
    request,
    *,
    default_view: str = "jobs-pipeline",
    default_pipeline_tab: str = "raw",
) -> tuple[str, dict | None]:
    """
    Normalize ``return_to`` from form posts and optional ``return_tab``.

    When returning to jobs pipeline, keep tab context so long-running task actions
    do not bounce users into a different section.
    """
    return_to = (request.POST.get("return_to", "") or "").strip() or default_view
    if return_to not in _ALLOWED_RETURN_VIEWS:
        return_to = default_view
    if return_to == "harvest-rawjobs":
        return_to = "jobs-pipeline"

    extra_query: dict | None = None
    if return_to == "jobs-pipeline":
        return_tab = (request.POST.get("return_tab", "") or "").strip() or default_pipeline_tab
        extra_query = {"tab": return_tab}

    return return_to, extra_query


def raw_jobs_missing_description_count() -> int:
    return _svc_raw_jobs_missing_description_count()


def raw_jobs_missing_jd_expired_count() -> int:
    return _svc_raw_jobs_missing_jd_expired_count()


def _raw_jobs_workflow_insights(*, stale_pending_hours: int = 6) -> dict:
    return _svc_raw_jobs_workflow_insights(stale_pending_hours=stale_pending_hours)


class SuperuserRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_superuser


# ── Platform Registry ──────────────────────────────────────────────────────────

class PlatformListView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/settings_platforms.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        platforms = list(
            JobBoardPlatform.objects.annotate(
                company_count=Count("labels"),
                missing_tenant_count=Count("labels", filter=Q(labels__tenant_id="")),
            ).order_by("name")
        )
        for platform in platforms:
            platform.implementation_kind = kind_for_slug(platform.slug).value
            platform.harvester_class_name = harvester_class_name_for_slug(platform.slug)
        total_platforms = JobBoardPlatform.objects.count()
        enabled_count = JobBoardPlatform.objects.filter(is_enabled=True).count()
        ctx["platforms"] = platforms
        ctx["form"] = JobBoardPlatformForm()
        ctx["active_tab"] = "platforms"
        ctx["total_platforms"] = total_platforms
        ctx["enabled_count"] = enabled_count
        ctx["disabled_count"] = total_platforms - enabled_count
        ctx["company_label_count"] = CompanyPlatformLabel.objects.count()
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
        labels_count = platform.labels.count()
        raw_jobs_count = platform.raw_jobs.count()
        if labels_count or raw_jobs_count:
            messages.error(
                request,
                (
                    f"Platform '{platform.name}' is still linked to "
                    f"{labels_count} company label(s) and {raw_jobs_count} raw job(s). "
                    "Disable it instead, or migrate those records before deleting."
                ),
            )
            return redirect("harvest-platforms")
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


# ── Company Labels ─────────────────────────────────────────────────────────────

class CompanyLabelListView(SuperuserRequiredMixin, ListView):
    template_name = "harvest/settings_labels.html"
    context_object_name = "labels"
    paginate_by = 100

    def get(self, request, *args, **kwargs):
        from companies.views import labels_query_to_companies_url

        return redirect(labels_query_to_companies_url(request.GET))

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

        # Consolidated: 1 aggregate instead of 6 separate COUNT queries
        label_agg = CompanyPlatformLabel.objects.aggregate(
            labeled=Count("id", filter=~Q(detection_method="UNDETECTED")),
            undetected=Count("id", filter=Q(detection_method="UNDETECTED")),
            verified=Count("id", filter=Q(is_verified=True)),
            live=Count("id", filter=Q(portal_alive=True)),
            down=Count("id", filter=Q(portal_alive=False)),
            total=Count("id"),
        )
        ctx["stat_labeled"] = label_agg["labeled"]
        ctx["stat_undetected"] = label_agg["undetected"]
        ctx["stat_unlabeled"] = Company.objects.count() - label_agg["total"]
        ctx["stat_verified"] = label_agg["verified"]
        ctx["stat_live"] = label_agg["live"]
        ctx["stat_down"] = label_agg["down"]
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
        label.portal_consecutive_failures = 0
        label.save(update_fields=[
            "tenant_id",
            "portal_alive",
            "portal_last_verified",
            "portal_consecutive_failures",
        ])
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
        from .models import HarvestEngineConfig
        from .tasks import detect_company_platforms_task

        # batch_size defaults to HarvestEngineConfig.detect_batch_size (configurable from GUI)
        raw_batch = (request.POST.get("batch_size") or "").strip()
        batch_size = int(raw_batch) if raw_batch.isdigit() else None  # None → task reads config
        platform_slug = (request.POST.get("platform_slug") or "").strip() or None
        force_recheck = request.POST.get("force_recheck", "") in ("1", "true", "True")

        task = detect_company_platforms_task.delay(
            batch_size=batch_size,
            force_recheck=force_recheck,
            triggered_user_id=request.user.id,
            platform_slug=platform_slug,
        )
        platform_label = f" ({platform_slug})" if platform_slug else ""
        messages.success(
            request,
            f"Platform detection{platform_label} is running on the server. "
            f"Refresh Run Monitor to see progress (task {task.id[:8]}…). "
            "Switching tabs does not stop this job.",
        )
        return redirect_with_task_progress(
            "ops-center",
            task.id,
            f"Platform detection{platform_label}",
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
            "ops-center",
            task.id,
            f"Harvest ({label})",
        )


class RunSyncNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import sync_harvested_to_pool_task
        raw_max = (request.POST.get("max_jobs", "") or "").strip()
        try:
            max_jobs = int(raw_max) if raw_max else 0
        except (TypeError, ValueError):
            max_jobs = 0
        qualified_only = (request.POST.get("qualified_only", "1").strip() != "0")
        chunk_raw = (request.POST.get("chunk_size", "") or "").strip()
        try:
            chunk_size = int(chunk_raw) if chunk_raw else 500
        except (TypeError, ValueError):
            chunk_size = 500

        task = sync_harvested_to_pool_task.delay(
            max_jobs=max_jobs,
            qualified_only=qualified_only,
            chunk_size=chunk_size,
        )
        scope_txt = "all qualified pending jobs" if not max_jobs else f"up to {max_jobs:,} qualified jobs"
        if not qualified_only:
            scope_txt = "pending jobs"
            if max_jobs:
                scope_txt = f"up to {max_jobs:,} pending jobs"
        messages.success(
            request,
            f"Vet sync started ({scope_txt}, Task: {task.id[:8]}…). "
            "This scans across the backlog and updates multi-page results.",
        )
        return_to, extra_query = _resolve_return_target(
            request,
            default_view="jobs-pipeline",
            default_pipeline_tab="raw",
        )
        return redirect_with_task_progress(
            return_to,
            task.id,
            "Sync Qualified to Vet Queue",
            extra_query=extra_query,
        )


class RunBulkSyncView(SuperuserRequiredMixin, View):
    """POST — sync up to 20,000 pending RawJobs to the pool in one shot."""
    def post(self, request):
        from .tasks import sync_harvested_to_pool_task
        task = sync_harvested_to_pool_task.delay(max_jobs=20000, qualified_only=False, chunk_size=500)
        messages.success(
            request,
            f"Bulk sync started — up to 20,000 pending jobs → pool (Task: {task.id[:8]}…). "
            "This runs in the background. Refresh to see progress.",
        )
        return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
        return redirect_with_task_progress(
            return_to,
            task.id,
            "Bulk sync (20k jobs)",
            extra_query=extra_query,
        )


class RunRetryFailedFetchesView(SuperuserRequiredMixin, View):
    """POST — retry FAILED company fetch runs from the last 7 days."""

    def post(self, request):
        from .tasks import retry_failed_raw_jobs_task

        task = retry_failed_raw_jobs_task.delay()
        messages.success(
            request,
            f"Retry failed fetches queued (Task: {task.id[:8]}…). "
            "This will re-run failed company fetches in the background.",
        )
        return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
        return redirect_with_task_progress(
            return_to,
            task.id,
            "Retry failed fetches",
            extra_query=extra_query,
        )


class RunValidateRawUrlsView(SuperuserRequiredMixin, View):
    """POST — run robust link-health validation on active raw jobs.

    Defaults for pending_only and recent_hours fall back to HarvestEngineConfig
    so they're tunable from the GUI:
      validate_links_include_synced → pending_only default
      validate_links_recent_hours   → recent_hours default
    """

    def post(self, request):
        from .models import HarvestEngineConfig
        from .tasks import validate_raw_job_urls_task

        cfg = HarvestEngineConfig.get()
        platform = (request.POST.get("platform_slug") or "").strip() or None
        recent_hours_raw = request.POST.get("recent_hours", "").strip()
        max_jobs = request.POST.get("max_jobs", "").strip()

        # pending_only: if caller explicitly passes 0/1 use it; otherwise use config
        pending_only_raw = request.POST.get("pending_only", "")
        if pending_only_raw in ("0", "1"):
            pending_only = (pending_only_raw == "1")
        else:
            # include_synced=True → pending_only=False
            pending_only = not cfg.validate_links_include_synced

        kwargs = {
            "batch_size": 100,
            "concurrency": 3,
            "pending_only": pending_only,
        }
        if platform:
            kwargs["platform_slug"] = platform
        if recent_hours_raw.isdigit():
            kwargs["recent_hours"] = int(recent_hours_raw)
        else:
            kwargs["recent_hours"] = cfg.validate_links_recent_hours or 168
        if max_jobs.isdigit():
            kwargs["max_jobs"] = min(int(max_jobs), 800)
        else:
            kwargs["max_jobs"] = 800

        scope = "PENDING only" if pending_only else "PENDING + SYNCED"
        task = validate_raw_job_urls_task.delay(**kwargs)
        messages.success(
            request,
            f"Link-health validation queued ({scope}, Task: {task.id[:8]}…). "
            "Soft-404 pages will be marked inactive before vet sync.",
        )
        return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
        return redirect_with_task_progress(
            return_to,
            task.id,
            "Validate Raw Job URLs",
            extra_query=extra_query,
        )


class RunSyncSelectedRawJobsView(SuperuserRequiredMixin, View):
    """POST — sync selected RawJob ids into pool."""

    def post(self, request):
        raw_ids = request.POST.get("raw_job_ids", "").strip()
        if not raw_ids:
            messages.error(request, "No rows selected for sync.")
            return redirect(f"{reverse('jobs-pipeline')}?tab=raw")

        parts = [p.strip() for p in raw_ids.split(",") if p.strip()]
        ids: list[int] = []
        for part in parts:
            if part.isdigit():
                ids.append(int(part))
        ids = ids[:500]
        if not ids:
            messages.error(request, "Selected ids were invalid.")
            return redirect(f"{reverse('jobs-pipeline')}?tab=raw")

        qs = RawJob.objects.select_related("company", "job_platform").filter(pk__in=ids)
        synced = skipped = failed = 0
        skipped_reasons: list[str] = []
        for raw_job in qs:
            try:
                _sync_rawjob_to_pool(raw_job, posted_by=request.user)
                synced += 1
            except ValueError as exc:
                skipped += 1
                reason = str(exc).strip()
                if reason and len(skipped_reasons) < 3:
                    skipped_reasons.append(reason)
            except Exception:
                failed += 1
                logger.exception("Sync selected raw job failed: raw_job_id=%s", raw_job.pk)

        msg = f"Selected sync complete — {synced} synced, {skipped} skipped, {failed} failed."
        if skipped_reasons:
            msg += " Sample blocked reasons: " + " | ".join(skipped_reasons)
        messages.success(request, msg)
        return redirect(f"{reverse('jobs-pipeline')}?tab=raw")


class RunCleanupNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import cleanup_harvested_jobs_task
        task = cleanup_harvested_jobs_task.delay()
        messages.success(request, f"Cleanup started (Task: {task.id[:8]}...)")
        return redirect_with_task_progress("ops-center", task.id, "Harvest cleanup")


class RunBackfillNowView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import backfill_platform_labels_from_jobs_task
        task = backfill_platform_labels_from_jobs_task.delay()
        messages.success(request, f"Backfill started — scanning all job URLs to detect platforms (Task: {task.id[:8]}...)")
        return redirect_with_task_progress(
            "company-list",
            task.id,
            "Platform backfill from job URLs",
            extra_query={"view": "ats"},
        )


class RunVerifyPortalsView(SuperuserRequiredMixin, View):
    """Queue async HTTP health checks for all career portal URLs."""
    def post(self, request):
        from .tasks import verify_all_portals_task
        task = verify_all_portals_task.delay()
        messages.success(
            request,
            f"Portal verification started — checking all career URLs in the background (Task: {task.id[:8]}...)"
        )
        return redirect_with_task_progress(
            "company-list",
            task.id,
            "Verifying career portal health",
            extra_query={"view": "ats"},
        )


# ── Raw Jobs Views ─────────────────────────────────────────────────────────────

class RawJobListView(SuperuserRequiredMixin, ListView):
    model = RawJob
    template_name = "jobs/pipeline.html"
    context_object_name = "jobs"
    paginate_by = 100

    def get(self, request, *args, **kwargs):
        """JSON path for infinite-scroll: ?page=N with X-Requested-With:XMLHttpRequest"""
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            from django.core.paginator import Paginator
            # Fetch only the columns rendered in the list — skips description /
            # raw_payload blobs which can be 10–50 KB each and are never shown here.
            qs = self.get_queryset().only(
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
                "jd_quality_score", "raw_payload",
                "job_keywords", "title_keywords",
                "company_industry", "company_size",
                "word_count",
            )
            paginator = Paginator(qs, self.paginate_by)
            try:
                page_num = int(request.GET.get("page", 1))
                page_obj = paginator.page(page_num)
            except Exception:
                return JsonResponse({"jobs": [], "has_next": False, "total": 0})

            jobs_data = []
            for job in page_obj.object_list:
                jd_gate = evaluate_raw_job_resume_gate(job)
                detected_country = infer_country_from_location(
                    location_raw=job.location_raw or "",
                    state=job.state or "",
                    country=job.country or "",
                )
                jobs_data.append({
                    "id": job.pk,
                    "company_name": (job.company_name or "")[:30],
                    "platform_slug": job.platform_slug or "",
                    "title": (job.title or "")[:60],
                    "job_category": job.job_category or "",
                    "job_domain": job.job_domain or "",
                    "original_url": job.original_url or "",
                    "location_raw": (job.location_raw or "")[:30],
                    "is_remote": job.is_remote,
                    "employment_type": job.employment_type or "",
                    "experience_level": job.experience_level or "",
                    "salary_min": float(job.salary_min) if job.salary_min else None,
                    "salary_max": float(job.salary_max) if job.salary_max else None,
                    "salary_raw": (job.salary_raw or "")[:20],
                    "posted_date": str(job.posted_date) if job.posted_date else "",
                    "fetched_at": job.fetched_at.strftime("%b %d, %H:%M") if job.fetched_at else "",
                    "fetched_at_iso": job.fetched_at.isoformat() if job.fetched_at else "",
                    "sync_status": job.sync_status or "",
                    "has_jd": job.has_description,
                    "jd_label": "yes" if job.has_description else ("expired" if not job.is_active else "no"),
                    "is_active": bool(job.is_active),
                    "department": (job.department_normalized or job.department or "")[:40],
                    "state": (job.state or "")[:48],
                    "country": (detected_country or "")[:48],
                    "education_required": job.education_required or "",
                    "languages_required": (job.languages_required or [])[:3],
                    "licenses_required": (job.licenses_required or [])[:3],
                    "clearance_required": bool(job.clearance_required),
                    "clearance_level": (job.clearance_level or "")[:64],
                    "schedule_type": (job.schedule_type or "")[:32],
                    "shift_schedule": (job.shift_schedule or "")[:64],
                    "travel_pct_min": job.travel_pct_min,
                    "travel_pct_max": job.travel_pct_max,
                    "resume_ready_score": job.resume_ready_score,
                    "classification_confidence": job.classification_confidence,
                    "quality_score": job.quality_score,
                    "jd_quality_score": job.jd_quality_score,
                    "certifications": (job.certifications or [])[:3],
                    "keywords": (job.title_keywords or job.job_keywords or [])[:4],
                    "company_industry": (job.company_industry or "")[:64],
                    "company_size": (job.company_size or "")[:32],
                    "stage": job.pipeline_stage_label(),
                    "owner_pipeline": job.owner_pipeline_label(),
                    "retry_count": job.retry_count_estimate(),
                    "last_error": job.last_error_text(),
                    "detail_url": reverse("harvest-rawjob-detail", args=[job.pk]),
                    "link_health_reason": (((job.raw_payload or {}).get("link_health") or {}).get("reason", ""))[:140],
                    "link_health_checked_at": (((job.raw_payload or {}).get("link_health") or {}).get("checked_at", ""))[:40],
                    "resume_jd_usable": jd_gate.usable,
                    "resume_jd_reason_code": jd_gate.reason_code,
                    "resume_jd_reason_text": jd_gate.reason_text,
                    "word_count": jd_gate.word_count,
                })

            return JsonResponse({
                "jobs": jobs_data,
                "has_next": page_obj.has_next(),
                "next_page": page_obj.next_page_number() if page_obj.has_next() else None,
                "total": paginator.count,
                "num_pages": paginator.num_pages,
            })
        # HTML Raw Jobs command center is consolidated into /jobs/pipeline/?tab=raw
        # while this endpoint remains for legacy XHR table consumers.
        query_pairs: list[tuple[str, str]] = [("tab", "raw")]
        for key, values in request.GET.lists():
            if key in {"tab", "_subtab"}:
                continue
            for value in values:
                value_s = str(value or "").strip()
                if value_s:
                    query_pairs.append((key, value_s))
        target = reverse("jobs-pipeline")
        if query_pairs:
            target = f"{target}?{urlencode(query_pairs)}"
        return redirect(target)

    def get_queryset(self):
        # No select_related — JOINs add 20x overhead on 122k rows; all displayed
        # fields (company_name, platform_slug) are denormalised directly on RawJob.
        qs = RawJob.objects.order_by("-fetched_at")
        return _svc_apply_rawjob_filters(qs, self.request.GET)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "rawjobs"

        # Display fallback country immediately from location text, even before
        # a full backfill writes inferred country into DB rows.
        for job in ctx.get("object_list", []):
            detected_country = infer_country_from_location(
                location_raw=job.location_raw or "",
                state=job.state or "",
                country=job.country or "",
            )
            setattr(job, "country_display", detected_country)

        # Unified KPI aggregation with short TTL + invalidation on writes.
        stats = _load_rawjobs_dashboard_stats(force_refresh=False)

        ctx["total_jobs"] = stats["total"]
        ctx["active_jobs"] = stats["active"]
        ctx["remote_jobs"] = stats["remote"]
        ctx["synced_jobs"] = stats["synced"]
        ctx["pending_jobs"] = stats["pending"]
        ctx["failed_jobs"] = stats["failed"]
        ctx["new_today"] = stats["new_today"]
        ctx["missing_description_jobs"] = stats["missing_jd"]
        ctx["missing_jd_expired_jobs"] = stats["expired_missing"]


        # Platform breakdown
        ctx["platform_stats"] = (
            _svc_production_rawjobs_queryset().values("platform_slug")
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
        ctx["education_required_choices"] = [
            (val, label) for val, label in RawJob._meta.get_field("education_required").choices if val
        ]

        # Filter state comes from shared query-contract helper.
        ctx.update(_svc_rawjob_filter_state(self.request.GET))
        paginator = ctx.get("paginator")
        ctx["jobs_total_filtered"] = paginator.count if paginator else 0

        # Workflow analytics for new control tabs.
        ctx["raw_insights"] = _raw_jobs_workflow_insights(stale_pending_hours=6)

        # Running batch check (for live polling)
        ctx["has_running_batch"] = FetchBatch.objects.filter(status="RUNNING").exists()

        ctx.update(_full_crawl_cooldown_ctx())
        # Engine config: expose to template so Raw Controls tooltips show live values
        from .models import HarvestEngineConfig
        ctx["engine_config"] = HarvestEngineConfig.get()
        return ctx


class RawJobDetailView(SuperuserRequiredMixin, DetailView):
    model = RawJob
    template_name = "harvest/rawjob_detail.html"
    context_object_name = "rawjob"  # template uses {{ rawjob.* }}

    def get_queryset(self):
        return RawJob.objects.select_related("company", "job_platform", "platform_label")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["resume_jd_gate"] = evaluate_raw_job_resume_gate(self.object)
        ctx["payload_snapshots"] = self.object.payload_snapshots.all()[:8]
        ctx["payload_snapshot_count"] = self.object.payload_snapshots.count()
        ctx["ops_timeline"] = _build_rawjob_ops_timeline(self.object)
        return ctx


class RawJobCheckLiveStatusView(SuperuserRequiredMixin, View):
    """POST — recheck a single raw-job posting URL and update is_active immediately."""

    def post(self, request, pk):
        from .url_health import check_job_posting_live, is_definitive_inactive

        raw_job = get_object_or_404(RawJob, pk=pk)
        url = (raw_job.original_url or "").strip()
        if not url:
            messages.error(request, "No source URL available for this row.")
            return redirect("harvest-rawjob-detail", pk=pk)

        result = check_job_posting_live(url, platform_slug=(raw_job.platform_slug or ""))
        payload = dict(raw_job.raw_payload or {})
        payload["link_health"] = {
            "is_live": bool(result.is_live),
            "reason": result.reason,
            "status_code": int(result.status_code or 0),
            "checked_at": timezone.now().isoformat(),
            "final_url": result.final_url,
            "decisive": bool((not result.is_live) and is_definitive_inactive(result)),
        }
        # Only flip inactive on definitive evidence to avoid transient false negatives.
        if result.is_live:
            raw_job.is_active = True
        elif is_definitive_inactive(result):
            raw_job.is_active = False
        raw_job.raw_payload = payload
        raw_job.save(update_fields=["is_active", "raw_payload", "updated_at"])
        _invalidate_rawjobs_dashboard_cache()

        if result.is_live:
            messages.success(
                request,
                f"Link health check: ACTIVE ({result.reason}, HTTP {result.status_code}).",
            )
        else:
            messages.warning(
                request,
                f"Link health check: INACTIVE ({result.reason}, HTTP {result.status_code}).",
            )
        return redirect("harvest-rawjob-detail", pk=pk)


@method_decorator(never_cache, name="dispatch")
class RawJobResumeProfileView(SuperuserRequiredMixin, View):
    """JSON export used by resume generation pipeline."""

    def get(self, request, pk):
        raw_job = get_object_or_404(
            RawJob.objects.select_related("company", "job_platform", "platform_label"),
            pk=pk,
        )
        jd_gate = evaluate_raw_job_resume_gate(raw_job)
        if not jd_gate.usable:
            return JsonResponse(
                {
                    "ok": False,
                    "raw_job_id": raw_job.pk,
                    "error": "JD is not usable for resume generation.",
                    "reason_code": jd_gate.reason_code,
                    "reason_text": jd_gate.reason_text,
                    "word_count": jd_gate.word_count,
                    "min_words": jd_gate.min_words,
                },
                status=422,
            )
        return JsonResponse(
            {
                "ok": True,
                "raw_job_id": raw_job.pk,
                "profile": build_resume_job_profile(raw_job),
                "resume_jd_gate": jd_gate.asdict(),
            }
        )


class FetchBatchListView(SuperuserRequiredMixin, View):
    """
    Legacy compatibility endpoint.
    - XHR: returns recent batch JSON.
    - HTML: redirects to unified Jobs Pipeline Raw tab.
    """

    def get(self, request, *args, **kwargs):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            qs = (
                FetchBatch.objects.prefetch_related("company_runs")
                .order_by("-created_at")[:50]
            )
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
                    "run_kind": ((b.audit_payload or {}).get("queue") or {}).get("run_kind"),
                    "audit_has_completion": bool((b.audit_payload or {}).get("completion")),
                })
            return JsonResponse({"batches": batches})
        # HTML batch history is consolidated into Jobs Pipeline (tab=raw).
        return redirect(f"{reverse('jobs-pipeline')}?tab=raw")


class FetchBatchDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    """Per-batch drill-down: every company run + platform rollup (staff visibility)."""

    model = FetchBatch
    template_name = "harvest/fetch_batch_detail.html"
    context_object_name = "batch"

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, "role", None) in (
            User.Role.ADMIN,
            User.Role.EMPLOYEE,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        batch = self.object
        status_filter = (self.request.GET.get("status") or "").strip().upper()
        runs = batch.company_runs.select_related(
            "label__company",
            "label__platform",
        ).order_by("label__company__name", "pk")
        valid_status = {choice[0] for choice in CompanyFetchRun.Status.choices}
        if status_filter in valid_status:
            runs = runs.filter(status=status_filter)

        ctx["runs"] = runs
        ctx["status_filter"] = status_filter
        ctx["valid_run_statuses"] = sorted(valid_status)

        ctx["batch_platform_rows"] = (
            batch.company_runs.values("label__platform__slug")
            .annotate(
                total=Count("id"),
                ok=Count(
                    "id",
                    filter=Q(
                        status__in=[
                            CompanyFetchRun.Status.SUCCESS,
                            CompanyFetchRun.Status.PARTIAL,
                        ]
                    ),
                ),
                bad=Count(
                    "id",
                    filter=Q(
                        status__in=[
                            CompanyFetchRun.Status.FAILED,
                            CompanyFetchRun.Status.SKIPPED,
                        ]
                    ),
                ),
                running=Count("id", filter=Q(status=CompanyFetchRun.Status.RUNNING)),
            )
            .order_by("-total")
        )

        snapshot_ids = list(batch.filter_snapshots.values_list("snapshot_id", flat=True))
        filter_qs = RawJob.objects.filter(filter_snapshot_id__in=snapshot_ids) if snapshot_ids else RawJob.objects.none()
        filter_counts = {
            row["filter_decision"] or "UNCLASSIFIED": row["n"]
            for row in filter_qs.values("filter_decision").annotate(n=Count("id"))
        }
        list_rows = sum(filter_counts.values())
        jd_skipped = filter_qs.filter(jd_fetch_skipped=True).count()
        sampled = HarvestSkippedTitle.objects.filter(batch_id=batch.pk, is_sampled=True).count()
        ctx["filter_stats"] = {
            "list_rows": list_rows,
            "strong": filter_counts.get("STRONG", 0),
            "possible": filter_counts.get("POSSIBLE", 0),
            "unknown": filter_counts.get("UNKNOWN", 0),
            "cold": filter_counts.get("COLD", 0),
            "no_match": filter_counts.get("NO_MATCH", 0),
            "jd_fetches_avoided": jd_skipped,
            "sampled": sampled,
            "snapshot_count": len(snapshot_ids),
        }

        since_7d = timezone.now() - timedelta(days=7)
        ctx["platform_health_7d"] = (
            CompanyFetchRun.objects.filter(started_at__gte=since_7d)
            .values("label__platform__slug")
            .annotate(
                total=Count("id"),
                ok=Count(
                    "id",
                    filter=Q(
                        status__in=[
                            CompanyFetchRun.Status.SUCCESS,
                            CompanyFetchRun.Status.PARTIAL,
                        ]
                    ),
                ),
                bad=Count(
                    "id",
                    filter=Q(
                        status__in=[
                            CompanyFetchRun.Status.FAILED,
                            CompanyFetchRun.Status.SKIPPED,
                        ]
                    ),
                ),
            )
            .order_by("-bad")[:30]
        )

        ctx["recent_batches"] = FetchBatch.objects.order_by("-created_at")[:15]
        return ctx


class HarvestBatchActivityView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """Centralized harvest batch timeline + rolled-up metrics (Quick / Full / smoke)."""

    model = FetchBatch
    template_name = "harvest/batch_activity.html"
    context_object_name = "batches"
    paginate_by = 40

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, "role", None) in (
            User.Role.ADMIN,
            User.Role.EMPLOYEE,
        )

    def get_queryset(self):
        return FetchBatch.objects.order_by("-created_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        since_24h = now - timedelta(hours=24)
        since_7d = now - timedelta(days=7)

        recent_24h = list(FetchBatch.objects.filter(created_at__gte=since_24h))
        recent_7d = FetchBatch.objects.filter(created_at__gte=since_7d)

        kind_counts: dict[str, int] = {}
        skipped_fresh_sum = eligible_sum = queued_sum = 0
        jobs_new_sum = jobs_found_sum = 0
        completion_batches = 0

        cr_agg = CompanyFetchRun.objects.filter(started_at__gte=since_24h).aggregate(
            ok_24h=Count(
                "id",
                filter=Q(
                    status__in=[
                        CompanyFetchRun.Status.SUCCESS,
                        CompanyFetchRun.Status.PARTIAL,
                    ]
                ),
            ),
            bad_24h=Count(
                "id",
                filter=Q(
                    status__in=[
                        CompanyFetchRun.Status.FAILED,
                        CompanyFetchRun.Status.SKIPPED,
                    ]
                ),
            ),
        )

        for b in recent_24h:
            q = (b.audit_payload or {}).get("queue") or {}
            rk = q.get("run_kind") or "unknown"
            kind_counts[rk] = kind_counts.get(rk, 0) + 1
            if q.get("eligible_labels") is not None:
                eligible_sum += int(q["eligible_labels"])
            if q.get("skipped_fresh") is not None:
                skipped_fresh_sum += int(q["skipped_fresh"])
            qc = q.get("queued_companies")
            if qc is not None:
                queued_sum += int(qc)
            c = (b.audit_payload or {}).get("completion") or {}
            if c:
                completion_batches += 1
            jobs_new_sum += int(b.total_jobs_new or 0)
            jobs_found_sum += int(b.total_jobs_found or 0)

        ctx["metrics_24h"] = {
            "batch_count": len(recent_24h),
            "run_kind_counts": dict(sorted(kind_counts.items(), key=lambda x: -x[1])),
            "eligible_labels_sum": eligible_sum,
            "skipped_fresh_sum": skipped_fresh_sum,
            "queued_companies_sum": queued_sum,
            "completion_logged_batches": completion_batches,
            "jobs_new_sum": jobs_new_sum,
            "jobs_found_sum": jobs_found_sum,
            "company_runs_ok_24h": int(cr_agg.get("ok_24h") or 0),
            "company_runs_bad_24h": int(cr_agg.get("bad_24h") or 0),
        }
        ctx["metrics_7d_batch_count"] = recent_7d.count()
        ctx["grep_hints"] = ["[HARVEST_AUDIT queue]", "[HARVEST_AUDIT done]", "[HARVEST_AUDIT ops_queue]", "[HARVEST_AUDIT ops_done]"]
        ctx["latest_fetch_batch"] = FetchBatch.objects.order_by("-created_at").first()

        ops_since = HarvestOpsRun.objects.filter(created_at__gte=since_24h)
        ctx["metrics_24h"]["ops_run_count"] = ops_since.count()
        ctx["metrics_24h"]["ops_by_operation"] = {
            row["operation"]: row["n"]
            for row in ops_since.values("operation").annotate(n=Count("id")).order_by()
        }
        ctx["recent_ops_runs"] = HarvestOpsRun.objects.order_by("-created_at")[:40]
        return ctx


class DismissOpsRunView(SuperuserRequiredMixin, View):
    """
    POST /harvest/raw-jobs/ops-runs/<pk>/dismiss/
    Marks a PARTIAL or FAILED HarvestOpsRun as SKIPPED so it disappears
    from the "Needs action" section of the Live Ops Monitor.
    Safe — does NOT touch any RawJob data, only flips the audit status.
    """

    def post(self, request, pk):
        from django.utils import timezone as _tz

        run = get_object_or_404(HarvestOpsRun, pk=pk)
        if run.status not in (HarvestOpsRun.Status.PARTIAL, HarvestOpsRun.Status.FAILED):
            return JsonResponse({"ok": False, "error": "Run is not PARTIAL or FAILED."}, status=400)

        payload = dict(run.audit_payload or {})
        payload["dismissed_at"] = _tz.now().isoformat()
        payload["dismissed_by"] = request.user.email or str(request.user)

        HarvestOpsRun.objects.filter(pk=pk).update(
            status=HarvestOpsRun.Status.SKIPPED,
            audit_payload=payload,
            finished_at=_tz.now(),
        )
        messages.success(
            request,
            f"Ops run #{pk} ({run.get_operation_display()}) dismissed — removed from Needs Action.",
        )
        return_to = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
        return redirect(return_to)


class RerunOpsRunView(SuperuserRequiredMixin, View):
    """
    POST /harvest/raw-jobs/ops-runs/<pk>/rerun/
    Dismisses a PARTIAL/FAILED HarvestOpsRun and re-queues the same
    operation with sensible defaults.  Supported operations:
      EVALUATE_SCOPE  → reevaluate_cold_scope_jobs_task
      BACKFILL_JD     → backfill_descriptions_task (via RunBackfillDescriptionsView logic)
      CLASSIFY_DOMAINS → classify_job_domains_task
      BACKFILL_ROLES  → backfill_job_marketing_roles_task
      SYNC_POOL       → sync_harvested_to_pool_task
      VALIDATE_URLS   → validate_raw_job_urls_task
      CLEANUP         → cleanup_harvested_jobs_task
    Other operations redirect back with an error message.
    """

    # Maps HarvestOpsRun.Operation → tasks.py function name.
    # Only include operations that are driven by a Celery task (not management commands).
    # CLASSIFY_DOMAINS and BACKFILL_ROLES are management-command-only; those show an error.
    RERUN_MAP: dict[str, str] = {
        HarvestOpsRun.Operation.EVALUATE_SCOPE: "reevaluate_cold_scope_jobs_task",
        HarvestOpsRun.Operation.BACKFILL_JD: "backfill_descriptions_task",
        HarvestOpsRun.Operation.SYNC_POOL: "sync_harvested_to_pool_task",
        HarvestOpsRun.Operation.VALIDATE_URLS: "validate_raw_job_urls_task",
        HarvestOpsRun.Operation.CLEANUP: "cleanup_harvested_jobs_task",
        HarvestOpsRun.Operation.BACKFILL_ROLES: "backfill_job_marketing_roles_task",
    }

    def post(self, request, pk):
        from django.utils import timezone as _tz

        run = get_object_or_404(HarvestOpsRun, pk=pk)
        if run.status not in (HarvestOpsRun.Status.PARTIAL, HarvestOpsRun.Status.FAILED):
            messages.error(request, f"Run #{pk} is {run.status}, not PARTIAL/FAILED — nothing to re-run.")
            return_to = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
            return redirect(return_to)

        task_func_name = self.RERUN_MAP.get(run.operation)
        return_to = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"

        if not task_func_name:
            messages.error(
                request,
                f"Re-run is not supported for operation '{run.get_operation_display()}'. "
                "Dismiss it and trigger manually from the Raw Controls panel.",
            )
            return redirect(return_to)

        # Dismiss the old PARTIAL run first
        payload = dict(run.audit_payload or {})
        payload["dismissed_at"] = _tz.now().isoformat()
        payload["dismissed_by"] = request.user.email or str(request.user)
        payload["rerun_triggered"] = True
        HarvestOpsRun.objects.filter(pk=pk).update(
            status=HarvestOpsRun.Status.SKIPPED,
            audit_payload=payload,
            finished_at=_tz.now(),
        )

        # Queue the new task
        from . import tasks as harvest_tasks
        task_func = getattr(harvest_tasks, task_func_name)
        task = task_func.delay()

        messages.success(
            request,
            f"✓ Re-running {run.get_operation_display()} — old run #{pk} dismissed. "
            f"New task: {task.id[:8]}… — watch the Live Ops Monitor.",
        )
        return redirect(return_to)


class HarvestOpsRunDetailView(LoginRequiredMixin, UserPassesTestMixin, DetailView):
    """JSON-shaped audit for a single non-batch pipeline op."""

    model = HarvestOpsRun
    template_name = "harvest/ops_run_detail.html"
    context_object_name = "ops_run"

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, "role", None) in (
            User.Role.ADMIN,
            User.Role.EMPLOYEE,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["audit_json"] = json.dumps(self.object.audit_payload or {}, indent=2, default=str)
        return ctx


class OpsRunLiveApiView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    JSON feed for the Live Ops Monitor panel.

    Returns two sections:
      batches — active/recent FetchBatch rows (Full Fetch, Quick Fetch)
      runs    — recent HarvestOpsRun rows, SKIPPED collapsed into a count

    Live Celery PROGRESS is merged in for any RUNNING run.
    Polled every 3 s (active) / 10 s (idle) by the front-end.
    """

    def test_func(self):
        u = self.request.user
        return u.is_superuser or getattr(u, "role", None) in (
            User.Role.ADMIN,
            User.Role.EMPLOYEE,
        )

    def get(self, request, *args, **kwargs):
        from datetime import timedelta

        from celery.result import AsyncResult
        from django.utils import timezone as _tz

        from .models import CompanyFetchRun, FetchBatch
        from .ops_audit import mark_stale_fetch_batches, mark_stale_running_ops

        now = _tz.now()
        since_24h = now - timedelta(hours=24)
        stale_marked = 0
        stale_marked += mark_stale_running_ops(
            HarvestOpsRun.Operation.VALIDATE_URLS,
            stale_after_minutes=45,
            reason="live_ops_monitor",
        )
        stale_marked += mark_stale_running_ops(
            HarvestOpsRun.Operation.BACKFILL_JD,
            stale_after_minutes=90,
            reason="live_ops_monitor",
        )
        stale_marked += mark_stale_running_ops(
            exclude_operations=[
                HarvestOpsRun.Operation.VALIDATE_URLS,
                HarvestOpsRun.Operation.BACKFILL_JD,
            ],
            stale_after_minutes=60,
            reason="live_ops_monitor",
        )
        stale_fetch = mark_stale_fetch_batches(
            stale_after_minutes=120,
            reason="live_ops_monitor",
        )

        # ── FetchBatch (Full Fetch / Quick Fetch) ────────────────────────────
        # Show: true active batches, the latest resumable checkpoint, plus the
        # latest finished batch as history. Older failed/cancelled checkpoints
        # stay on the batch detail page instead of cluttering the monitor.
        batches = []
        active_batches = list(
            FetchBatch.objects.filter(
                status__in=["RUNNING", "PENDING"]
            ).order_by("-created_at")
        )
        resumable_batches = list(
            FetchBatch.objects
            .annotate(_done_companies=F("completed_companies") + F("failed_companies"))
            .filter(status="PARTIAL", total_companies__gt=F("_done_companies"))
            .order_by("-created_at")[:1]
        )
        finished_partials = list(
            FetchBatch.objects
            .annotate(_done_companies=F("completed_companies") + F("failed_companies"))
            .filter(status="PARTIAL", total_companies__lte=F("_done_companies"))
            .order_by("-created_at")[:1]
        )
        last_finished = list(
            FetchBatch.objects.filter(
                status__in=["COMPLETED", "CANCELLED", "FAILED"]
            ).order_by("-created_at")[:1]
        )
        batch_qs = []
        for b in active_batches + resumable_batches + finished_partials + last_finished:
            if b not in batch_qs:
                batch_qs.append(b)

        for b in batch_qs:
            done = b.completed_companies + b.failed_companies
            total_c = b.total_companies or 0
            pct = b.progress_pct
            elapsed = None
            runtime = None
            if b.started_at:
                elapsed = int((now - b.started_at).total_seconds())
            if b.started_at and b.completed_at:
                runtime = int((b.completed_at - b.started_at).total_seconds())

            run_kind = (b.audit_payload or {}).get("queue", {}).get("run_kind", "") or ""
            if "full" in run_kind:
                label = "Full Fetch"
                op_color = "full_fetch"
            elif "platform_smoke" in run_kind:
                label = "Test Fetch"
                op_color = "test_fetch"
            else:
                label = "Quick Fetch"
                op_color = "quick_fetch"

            # For running batches, count live company runs
            running_companies = 0
            recent_company_msgs = []
            if b.status == FetchBatch.Status.RUNNING:
                running_companies = CompanyFetchRun.objects.filter(
                    batch=b, status=CompanyFetchRun.Status.RUNNING
                ).count()

                # ── Zombie detection ──────────────────────────────────────────
                # If no workers are active and the batch hasn't progressed in
                # 10+ minutes, the Celery tasks were lost (worker crash / laptop
                # closed).  Auto-mark PARTIAL so the monitor reflects reality.
                _elapsed_min = elapsed / 60 if elapsed else 0
                if (
                    running_companies == 0
                    and _elapsed_min > 10
                    and done < total_c
                ):
                    b.status = FetchBatch.Status.PARTIAL
                    b.completed_at = now
                    b.save(update_fields=["status", "completed_at"])
                    running_companies = 0  # reflect updated state
                recent_done = (
                    CompanyFetchRun.objects
                    .filter(batch=b, status__in=[
                        CompanyFetchRun.Status.SUCCESS,
                        CompanyFetchRun.Status.FAILED,
                        CompanyFetchRun.Status.PARTIAL,
                        CompanyFetchRun.Status.EMPTY,
                    ])
                    .select_related("label__company", "label__platform")
                    .order_by("-completed_at")[:5]
                )
                for cr in recent_done:
                    company_name = (
                        cr.label.company.name if cr.label and cr.label.company else "?"
                    )[:40]
                    platform = (
                        cr.label.platform.name if cr.label and cr.label.platform else "?"
                    )[:20]
                    status_icon = "✓" if cr.status == CompanyFetchRun.Status.SUCCESS else (
                        "○" if cr.status == CompanyFetchRun.Status.EMPTY else "✗"
                    )
                    recent_company_msgs.append(
                        f"{status_icon} {company_name} ({platform}) +{cr.jobs_new} new"
                    )

            msg_parts = []
            if total_c:
                msg_parts.append(f"{done:,}/{total_c:,} companies")
            if b.total_jobs_new:
                msg_parts.append(f"{b.total_jobs_new:,} new jobs")
            if b.failed_companies:
                msg_parts.append(f"{b.failed_companies} failed")
            if running_companies:
                msg_parts.append(f"{running_companies} workers active")
            main_msg = " · ".join(msg_parts)

            batches.append({
                "id": b.pk,
                "kind": "fetch_batch",
                "op_color": op_color,
                "label": label,
                "status": b.status,
                "pct": pct,
                "total_companies": total_c,
                "done_companies": done,
                "failed_companies": b.failed_companies,
                "total_jobs_new": b.total_jobs_new,
                "total_jobs_found": b.total_jobs_found,
                "running_workers": running_companies,
                "message": main_msg,
                "recent_company_log": recent_company_msgs,
                "elapsed_seconds": elapsed,
                "runtime_seconds": runtime,
                "detail_url": f"/harvest/raw-jobs/batches/{b.pk}/",
                "created_at": b.created_at.isoformat() if b.created_at else "",
            })

        # ── HarvestOpsRun (Backfill JD, Detect, Validate, Cleanup, …) ────────
        runs = []
        skipped_counts: dict[str, int] = {}  # op → count of SKIPPED grouped

        # Load more rows than we'll show — to collapse SKIPPED groups
        shown_success_ops: set[str] = set()
        for run in HarvestOpsRun.objects.select_related("triggered_by_user").order_by("-created_at")[:60]:
            if run.status == HarvestOpsRun.Status.SKIPPED:
                skipped_counts[run.operation] = skipped_counts.get(run.operation, 0) + 1
                continue  # don't surface individual SKIPPED rows

            if run.status == HarvestOpsRun.Status.SUCCESS:
                if run.operation in shown_success_ops:
                    skipped_counts[run.operation] = skipped_counts.get(run.operation, 0) + 1
                    continue
                shown_success_ops.add(run.operation)

            # Hide trivial "nothing to do" completions — Beat fires every hour and
            # creates a SUCCESS run in ~0 s when 0 jobs are eligible.  These add
            # zero information and flood the panel.
            if (
                run.status == HarvestOpsRun.Status.SUCCESS
                and run.finished_at
                and run.created_at
                and (run.finished_at - run.created_at).total_seconds() < 5
                and (run.progress_total or 0) == 0
            ):
                skipped_counts[run.operation] = skipped_counts.get(run.operation, 0) + 1
                continue

            total = run.progress_total or 0
            current = run.progress_current or 0
            message = run.progress_message or ""
            pct = int(100 * current / total) if total else (
                100 if run.status == HarvestOpsRun.Status.SUCCESS else 0
            )

            # For RUNNING runs, merge in live Celery PROGRESS
            live_pct = pct
            live_msg = message
            stuck_warning = False
            if run.status == HarvestOpsRun.Status.RUNNING and run.celery_task_id:
                try:
                    res = AsyncResult(run.celery_task_id)
                    if res.state == "PROGRESS" and isinstance(res.info, dict):
                        info = res.info
                        c = int(info.get("current") or current)
                        t = int(info.get("total") or total) or total
                        live_pct = int(100 * c / t) if t else live_pct
                        live_msg = (info.get("message") or message)[:200]
                        if c != current or live_msg != message:
                            HarvestOpsRun.objects.filter(pk=run.pk).update(
                                progress_current=c,
                                progress_total=t,
                                progress_message=live_msg[:256],
                            )
                            current, total, message, pct = c, t, live_msg, live_pct
                except Exception:
                    pass
                # Flag tasks stuck at 0% for > 5 min as potentially stale
                elapsed_run = int((now - run.created_at).total_seconds()) if run.created_at else 0
                if elapsed_run > 300 and live_pct == 0 and not live_msg.strip():
                    stuck_warning = True

            elapsed = int((now - run.created_at).total_seconds()) if run.created_at else None
            runtime = (
                int((run.finished_at - run.created_at).total_seconds())
                if run.finished_at and run.created_at else None
            )
            payload = run.audit_payload or {}
            completion = payload.get("completion") or {}
            runs.append({
                "id": run.pk,
                "operation": run.operation,
                "operation_label": run.get_operation_display(),
                "status": run.status,
                "celery_task_id": (run.celery_task_id or "")[:16],
                "elapsed_seconds": elapsed,
                "runtime_seconds": runtime,
                "progress_current": current,
                "progress_total": total,
                "progress_pct": live_pct,
                "progress_message": live_msg[:200],
                "completion": completion,
                # In-depth context: exact params the run started with, who started it,
                # and why it was stale-marked — so the monitor explains itself.
                "params": payload.get("queue") or {},
                "triggered_by": (run.triggered_by_user.username
                                 if run.triggered_by_user_id else "scheduler"),
                "stale_info": payload.get("stale") or {},
                "stuck_warning": stuck_warning,
                "stale_marked": bool(payload.get("stale")),
                "detail_url": f"/harvest/raw-jobs/ops-runs/{run.pk}/",
                "created_at": run.created_at.isoformat() if run.created_at else "",
            })
            if len(runs) >= 15:
                break

        active_batches = sum(1 for b in batches if b["status"] == FetchBatch.Status.RUNNING)
        active_ops = sum(1 for r in runs if r["status"] == HarvestOpsRun.Status.RUNNING)
        active_count = active_batches + active_ops

        return JsonResponse({
            "batches": batches,
            "runs": runs,
            "skipped_counts": skipped_counts,
            "active_count": active_count,
            "stale_marked": stale_marked,
            "stale_fetch_marked": stale_fetch,
            "ts": now.isoformat(),
        })


class CompanyFetchStatusView(SuperuserRequiredMixin, View):
    """
    Legacy compatibility endpoint.
    - XHR: returns recent company fetch run JSON (optionally filtered).
    - HTML: redirects to unified Jobs Pipeline Raw tab.
    """

    def _get_queryset(self, request):
        qs = CompanyFetchRun.objects.select_related(
            "label__company", "label__platform", "batch"
        ).order_by("-started_at")

        status_f = request.GET.get("status", "").strip()
        if status_f:
            qs = qs.filter(status=status_f)

        platform_f = request.GET.get("platform", "").strip()
        if platform_f:
            qs = qs.filter(label__platform__slug=platform_f)

        return qs

    def get(self, request, *args, **kwargs):
        # JSON response for legacy AJAX clients
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            qs = self._get_queryset(request)[:100]
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
        # HTML company status view is consolidated into Jobs Pipeline (tab=raw).
        return redirect(f"{reverse('jobs-pipeline')}?tab=raw")


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


class FetchCooldownStatusView(SuperuserRequiredMixin, View):
    """GET — JSON: cooldown status for the Full Crawl button."""
    def get(self, request):
        cd = _full_crawl_cooldown_ctx()
        last_full = cd["last_full_batch"]
        remaining = cd["cooldown_remaining_sec"]
        if not last_full:
            return JsonResponse({"on_cooldown": False, "remaining_sec": 0, "last_batch_at": None})
        return JsonResponse({
            "on_cooldown": remaining > 0,
            "remaining_sec": remaining,
            "last_batch_at": last_full.created_at.isoformat(),
            "last_batch_name": last_full.name or f"Batch #{last_full.pk}",
            "last_batch_status": last_full.status,
        })


class TriggerBatchFetchView(SuperuserRequiredMixin, View):
    """POST — triggers a batch raw job fetch for all or filtered companies.

    fetch_mode:
      "quick"  → incremental (since_hours=25, no fetch_all) — fast daily sync
      "full"   → full crawl (fetch_all=True, all pages) — slow, 2hr cooldown enforced
      "test"   → test mode (test_mode=1, companies_per_platform, test_max_jobs)
      ""       → filtered batch (platform_slug selector form)
    """

    def post(self, request):
        from .tasks import fetch_raw_jobs_batch_task
        fetch_mode = request.POST.get("fetch_mode", "").strip()  # "quick" | "full" | "test" | ""
        platform_slug = request.POST.get("platform_slug", "").strip() or None
        batch_name = request.POST.get("batch_name", "").strip() or None
        test_mode = request.POST.get("test_mode", "") in ("1", "true", "True", "yes")
        test_max_jobs = int(request.POST.get("test_max_jobs", "10") or "10")
        companies_per_platform = int(request.POST.get("companies_per_platform", "1") or "1")

        # skip_platforms: comma-separated string OR multiple hidden inputs
        skip_raw = request.POST.get("skip_platforms", "").strip()
        skip_platforms = [s.strip() for s in skip_raw.split(",") if s.strip()] if skip_raw else []

        # ── Guard: one active batch at a time ────────────────────────────────
        # RUNNING or PARTIAL means work is either in progress or resumable.
        # Don't let a new batch pile on top — force Stop or Resume first.
        if fetch_mode in ("quick", "full", ""):
            from .ops_audit import mark_stale_fetch_batches

            mark_stale_fetch_batches(
                stale_after_minutes=120,
                reason="start_batch_preflight",
            )
            active_batch = (
                FetchBatch.objects
                .annotate(_done_companies=F("completed_companies") + F("failed_companies"))
                .filter(Q(status="RUNNING") | Q(status="PARTIAL", total_companies__gt=F("_done_companies")))
                .order_by("-created_at")
                .first()
            )
            if active_batch:
                status_word = "running" if active_batch.status == "RUNNING" else "paused (PARTIAL)"
                done = active_batch.completed_companies + active_batch.failed_companies
                remaining = active_batch.total_companies - done
                messages.error(
                    request,
                    f"⚠ Batch #{active_batch.pk} is already {status_word} "
                    f"({done}/{active_batch.total_companies} done, {remaining} remaining). "
                    f"Stop it first or use ▶ Resume to continue from checkpoint.",
                )
                return_to, _ = _resolve_return_target(request, default_view="jobs-pipeline")
                return redirect(return_to)

        # ── Mode: Quick Sync ─────────────────────────────────────────────────
        if fetch_mode == "quick":
            ts = timezone.now().strftime("%Y-%m-%d %H:%M")
            task = fetch_raw_jobs_batch_task.delay(
                platform_slug=platform_slug or None,
                batch_name=batch_name or f"Quick Sync (25h) — {ts}",
                triggered_user_id=request.user.id,
                test_mode=False,
                fetch_all=False,        # incremental: since_hours=25 only
                min_hours_since_fetch=6,
                run_kind="quick_sync",
            )
            messages.success(
                request,
                f"Quick Sync started — fetching new/updated jobs from the last 25h "
                f"(Task: {task.id[:8]}…). Much faster than a full crawl.",
            )
            return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
            return redirect_with_task_progress(
                return_to,
                task.id,
                "Quick Sync (25h)",
                extra_query=extra_query,
            )

        # ── Mode: Full Crawl — enforce configurable cooldown ─────────────────
        if fetch_mode == "full":
            cooldown_mins = _full_crawl_cooldown_minutes()
            cooldown_secs = cooldown_mins * 60

            # Cache-layer guard: enforced even on direct API calls (bypasses view cooldown check)
            from django.core.cache import cache as _cache
            if not _cache.add(_FULL_CRAWL_LOCK_KEY, "1", cooldown_secs):
                # Already in cooldown — check DB for human-readable message
                cd = _full_crawl_cooldown_ctx()
                last_full = cd["last_full_batch"]
                remaining = cd["cooldown_remaining_sec"]
                elapsed_sec = (timezone.now() - last_full.created_at).total_seconds() if last_full else 0
                mins = remaining // 60
                secs = remaining % 60
                messages.error(
                    request,
                    f"⏱ Full Crawl on cooldown — last batch ran {int(elapsed_sec // 60)} min ago. "
                    f"Wait {mins}m {secs}s before starting another full crawl "
                    f"(configured cooldown: {cooldown_mins} min). "
                    f"Use Quick Sync (25h) for an incremental update now.",
                )
                return_to, _ = _resolve_return_target(request, default_view="jobs-pipeline")
                return redirect(return_to)

            # Also check DB-level cooldown for any stale cache
            cd = _full_crawl_cooldown_ctx()
            last_full = cd["last_full_batch"]
            remaining = cd["cooldown_remaining_sec"]
            if last_full and remaining > 0:
                _cache.delete(_FULL_CRAWL_LOCK_KEY)  # release cache lock since we're blocking on DB
                elapsed_sec = (timezone.now() - last_full.created_at).total_seconds()
                mins = remaining // 60
                secs = remaining % 60
                messages.error(
                    request,
                    f"⏱ Full Crawl on cooldown — last batch ran {int(elapsed_sec // 60)} min ago. "
                    f"Wait {mins}m {secs}s before starting another full crawl "
                    f"(configured cooldown: {cooldown_mins} min). "
                    f"Use Quick Sync (25h) for an incremental update now.",
                )
                return_to, _ = _resolve_return_target(request, default_view="jobs-pipeline")
                return redirect(return_to)

            ts = timezone.now().strftime("%Y-%m-%d %H:%M")
            task = fetch_raw_jobs_batch_task.delay(
                platform_slug=platform_slug or None,
                batch_name=batch_name or f"Full Crawl — {ts}",
                triggered_user_id=request.user.id,
                test_mode=False,
                fetch_all=True,         # full pagination — all pages, all companies
                min_hours_since_fetch=6,
                run_kind="full_crawl_platform" if platform_slug else "full_crawl_all",
            )
            messages.success(
                request,
                f"Full Crawl started — fetching ALL jobs from every platform "
                f"(Task: {task.id[:8]}…). This may take 30–60+ minutes.",
            )
            return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
            return redirect_with_task_progress(
                return_to,
                task.id,
                "Full Crawl",
                extra_query=extra_query,
            )

        # ── Mode: Test / Platform Check ───────────────────────────────────────
        if test_mode or fetch_mode == "test":
            task = fetch_raw_jobs_batch_task.delay(
                platform_slug=platform_slug,
                batch_name=batch_name,
                triggered_user_id=request.user.id,
                test_mode=True,
                test_max_jobs=test_max_jobs,
                companies_per_platform=companies_per_platform,
                skip_platforms=skip_platforms or None,
                fetch_all=False,
                run_kind="platform_smoke",
            )
            skip_note = f", skip: {', '.join(skip_platforms)}" if skip_platforms else ""
            messages.success(
                request,
                f"Platform check started — {companies_per_platform} co/platform, up to {test_max_jobs} jobs each{skip_note} (Task: {task.id[:8]}…)",
            )
            return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
            return redirect_with_task_progress(
                return_to,
                task.id,
                f"Platform check ({test_max_jobs} jobs/platform)",
                extra_query=extra_query,
            )

        # ── Mode: Filtered Batch (platform selector form) ─────────────────────
        filtered_rk = "full_crawl_platform" if platform_slug else "full_crawl_all"
        task = fetch_raw_jobs_batch_task.delay(
            platform_slug=platform_slug,
            batch_name=batch_name,
            triggered_user_id=request.user.id,
            test_mode=False,
            skip_platforms=skip_platforms or None,
            fetch_all=True,
            run_kind=filtered_rk,
        )
        messages.success(
            request,
            f"Raw jobs batch fetch started"
            + (f" for platform '{platform_slug}'" if platform_slug else " for all platforms")
            + f" (Task: {task.id[:8]}...)",
        )
        return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
        return redirect_with_task_progress(
            return_to,
            task.id,
            "Raw jobs batch fetch",
            extra_query=extra_query,
        )


class StopBatchView(SuperuserRequiredMixin, View):
    """
    POST — stop a batch that is RUNNING or PARTIAL.

    Two-pronged approach that handles both already-running and queued tasks:
    1. Set batch.stop_requested = True  →  every task that picks up from the
       queue checks this flag at startup and exits immediately without doing work.
       This handles countdown/ETA tasks that haven't started yet (have no
       CompanyFetchRun record, so we can't revoke them by task ID).
    2. Revoke + SIGTERM any tasks that are *already* RUNNING  →  kills the
       active HTTP fetch in the worker process right now.

    Result: the queue drains to zero in seconds (tasks bail on flag check),
    and any mid-flight company fetch is killed. Batch → CANCELLED.
    Resume button becomes available to restart from checkpoint.
    """

    def get(self, request):
        return redirect(f"{reverse('jobs-pipeline')}?tab=raw")

    def post(self, request):
        from celery import current_app

        batch_id = request.POST.get("batch_id") or None
        if batch_id:
            batch = get_object_or_404(FetchBatch, pk=int(batch_id))
        else:
            # Stop the most recently started active batch
            batch = (
                FetchBatch.objects.filter(status__in=["RUNNING", "PARTIAL"])
                .order_by("-created_at")
                .first()
            )

        if not batch:
            msg = "No active batch found."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": msg}, status=404)
            messages.warning(request, msg)
            return redirect(f"{reverse('jobs-pipeline')}?tab=raw")

        stoppable = {"RUNNING", "PARTIAL", "PENDING"}
        if batch.status not in stoppable:
            msg = f"Batch #{batch.pk} is {batch.status} — nothing to stop."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": msg})
            messages.warning(request, msg)
            return redirect(f"{reverse('jobs-pipeline')}?tab=raw")

        now = timezone.now()

        # ── 1. Set stop_requested flag — queued tasks see this and bail fast ──
        batch.stop_requested = True
        batch.status = FetchBatch.Status.CANCELLED
        batch.completed_at = batch.completed_at or now
        batch.save(update_fields=["stop_requested", "status", "completed_at"])

        # ── 2. Revoke the orchestrator task (if any) ──────────────────────────
        if batch.task_id:
            try:
                current_app.control.revoke(batch.task_id, terminate=True, signal="SIGTERM")
            except Exception:
                pass

        # ── 3. Revoke tasks that are already RUNNING in a worker ──────────────
        #  (tasks still in the queue with countdown don't have a CompanyFetchRun
        #   yet — they'll be stopped by the stop_requested flag instead.)
        active_runs = CompanyFetchRun.objects.filter(
            batch=batch, status="RUNNING"
        ).exclude(task_id="").exclude(task_id=None)
        task_ids = list(active_runs.values_list("task_id", flat=True))
        if task_ids:
            try:
                current_app.control.revoke(task_ids, terminate=True, signal="SIGTERM")
            except Exception:
                pass
            active_runs.update(status="SKIPPED")

        # ── 4. Audit ──────────────────────────────────────────────────────────
        try:
            ap = dict(batch.audit_payload or {})
            ap["cancelled"] = {
                "at": now.isoformat(),
                "by": request.user.username,
                "revoked_running_tasks": len(task_ids),
                "note": "Remaining queued tasks will bail on stop_requested flag.",
            }
            FetchBatch.objects.filter(pk=batch.pk).update(audit_payload=ap)
        except Exception:
            logger.exception("StopBatchView: failed to write audit_payload")

        logger.info(
            "[HARVEST] Batch #%s CANCELLED by %s — stop_requested set, %d running task(s) revoked",
            batch.pk, request.user.username, len(task_ids),
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({
                "ok": True,
                "batch_id": batch.pk,
                "revoked_running": len(task_ids),
                "message": "Batch cancelled. Queued tasks will drain in seconds.",
            })

        messages.success(
            request,
            f"Batch #{batch.pk} stopping — queued tasks will drain in seconds. "
            f"Use Resume to continue from checkpoint.",
        )
        return redirect(f"{reverse('jobs-pipeline')}?tab=raw")


class ResumeBatchView(SuperuserRequiredMixin, View):
    """POST — resume a PARTIAL/interrupted FetchBatch from its checkpoint.

    Finds companies that have no finished CompanyFetchRun in this batch
    and re-dispatches only those — no restart from scratch needed.
    """

    def post(self, request):
        from .tasks import resume_fetch_batch_task

        batch_id = request.POST.get("batch_id")
        if not batch_id:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": "batch_id required"}, status=400)
            messages.error(request, "batch_id required.")
            return redirect(f"{reverse('jobs-pipeline')}?tab=raw")

        batch = get_object_or_404(FetchBatch, pk=batch_id)

        if batch.status not in ("PARTIAL", "RUNNING", "FAILED", "CANCELLED"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": f"Batch is {batch.status} — nothing to resume."})
            messages.warning(request, f"Batch #{batch.pk} is {batch.status} — nothing to resume.")
            return redirect(f"{reverse('jobs-pipeline')}?tab=raw")

        # Clear stop_requested so tasks dispatched by the resume actually run.
        if batch.stop_requested:
            batch.stop_requested = False
            batch.save(update_fields=["stop_requested"])

        task = resume_fetch_batch_task.apply_async(
            kwargs={"batch_id": batch.pk},
            queue="batches",
        )

        logger.info(
            "[HARVEST] Batch #%s resume triggered by %s (task=%s)",
            batch.pk, request.user.username, task.id,
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "batch_id": batch.pk, "task_id": task.id})

        messages.success(request, f"Batch #{batch.pk} resuming — only unfinished companies will be re-fetched.")
        return redirect(f"{reverse('jobs-pipeline')}?tab=raw")


class RunEnrichExistingView(SuperuserRequiredMixin, View):
    """POST — run enrichment engine on all jobs already in DB (no HTTP)."""

    def post(self, request):
        from .tasks import enrich_existing_jobs_task

        platform_slug  = request.POST.get("platform_slug", "").strip() or None
        batch_size     = int(request.POST.get("batch_size", "2000") or "2000")
        only_unenriched = request.POST.get("only_unenriched", "1") not in ("0", "false", "False")

        task = enrich_existing_jobs_task.delay(
            batch_size=batch_size,
            platform_slug=platform_slug,
            only_unenriched=only_unenriched,
        )
        label = f"platform={platform_slug}" if platform_slug else "all platforms"
        messages.success(
            request,
            f"Enrichment started ({label}, batch={batch_size:,}, unenriched_only={only_unenriched}) — Task {task.id[:8]}…",
        )
        return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
        return redirect_with_task_progress(
            return_to,
            task.id,
            f"Enrich existing jobs ({label})",
            extra_query=extra_query,
        )


class RunBackfillResumeContractView(SuperuserRequiredMixin, View):
    """POST — backfill expanded resume-classification contract fields."""

    def post(self, request):
        from .tasks import backfill_resume_contract_task

        batch_size = int(request.POST.get("batch_size", "1500") or "1500")
        offset = int(request.POST.get("offset", "0") or "0")
        task = backfill_resume_contract_task.delay(batch_size=batch_size, offset=offset)
        messages.success(
            request,
            f"Resume contract backfill started (batch={batch_size:,}, offset={offset:,}) — Task {task.id[:8]}…",
        )
        return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
        return redirect_with_task_progress(
            return_to,
            task.id,
            "Backfill resume contract fields",
            extra_query=extra_query,
        )


class RunBackfillDescriptionsView(SuperuserRequiredMixin, View):
    """POST — launch backfill_descriptions_task to fetch JDs for jobs that have none.

    All parameters fall back to HarvestEngineConfig values when not supplied:
      parallel_workers → config.backfill_jd_workers
      reset_locks      → config.backfill_jd_reset_locks
      include_cold     → config.backfill_jd_include_cold
    """

    def post(self, request):
        from .models import HarvestEngineConfig
        from .tasks import backfill_descriptions_task

        cfg = HarvestEngineConfig.get()
        platform_slug = request.POST.get("platform_slug", "").strip() or None
        batch_size = min(int(request.POST.get("batch_size", "100") or "100"), 100)
        offset = int(request.POST.get("offset", "0") or "0")
        force_jarvis = request.POST.get("force_jarvis", "") in ("1", "true", "True")

        # If caller didn't supply these, pass None → task reads config
        raw_workers = (request.POST.get("parallel_workers") or "").strip()
        parallel_workers = 1 if raw_workers.isdigit() else None

        raw_reset = request.POST.get("reset_locks", "")
        reset_locks = None if raw_reset == "" else (raw_reset in ("1", "true", "True"))

        raw_cold = request.POST.get("include_cold", "")
        include_cold = None if raw_cold == "" else (raw_cold in ("1", "true", "True"))

        task = backfill_descriptions_task.delay(
            batch_size=batch_size,
            parallel_workers=parallel_workers,
            platform_slug=platform_slug,
            offset=offset,
            force_jarvis=force_jarvis,
            reset_locks=reset_locks,
            include_cold=include_cold,
        )
        label = f"platform={platform_slug}" if platform_slug else "all platforms"
        mode = "Deep Scan (Jarvis)" if force_jarvis else "backfill"
        # Display resolved worker count (config default if not supplied)
        display_workers = 1
        cold_note = " +cold" if (include_cold if include_cold is not None else cfg.backfill_jd_include_cold) else ""
        messages.success(
            request,
            f"Description {mode} started ({label}, batch={batch_size}, workers={display_workers}{cold_note}) — Task {task.id[:8]}…",
        )
        return_to, extra_query = _resolve_return_target(request, default_view="jobs-pipeline")
        return redirect_with_task_progress(
            return_to,
            task.id,
            f"{'Deep Scan' if force_jarvis else 'Backfill'} descriptions ({label})",
            extra_query=extra_query,
        )


class RawJobCompanyBreakdownView(SuperuserRequiredMixin, View):
    """GET ?filter=pending|missing_jd — company-level breakdown for a stat filter."""

    def get(self, request):
        filter_type = request.GET.get("filter", "").strip()

        if filter_type == "pending":
            qs = _svc_production_rawjobs_queryset().filter(sync_status="PENDING")
        elif filter_type == "missing_jd":
            qs = RawJob.objects.missing_jd().filter(is_test_run=False)
        else:
            return JsonResponse({"error": "Invalid filter. Use pending or missing_jd."}, status=400)

        companies = list(
            qs.values("company_name", "platform_slug")
            .annotate(count=Count("id"))
            .order_by("-count")[:200]
        )
        return JsonResponse({"filter": filter_type, "total": qs.count(), "companies": companies})


@method_decorator(never_cache, name="dispatch")
class RawJobStatsView(SuperuserRequiredMixin, View):
    """Raw Jobs stats page + JSON endpoint for dashboard polling."""
    def get(self, request):
        # Running batch info
        running_batch = FetchBatch.objects.filter(status="RUNNING").order_by("-created_at").first()
        running_company_fetch = CompanyFetchRun.objects.filter(
            status=CompanyFetchRun.Status.RUNNING
        ).exists()
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

        # Force refresh while a batch or Jarvis company fetch is running.
        stats = _load_rawjobs_dashboard_stats(force_refresh=bool(running_batch or running_company_fetch))

        payload = {
            "total_jobs": stats["total"],
            "active_jobs": stats["active"],
            "remote_jobs": stats["remote"],
            "synced_jobs": stats["synced"],
            "pending_jobs": stats["pending"],
            "failed_jobs": stats["failed"],
            "new_today": stats["new_today"],
            "missing_description_jobs": stats["missing_jd"],
            "missing_jd_expired_jobs": stats["expired_missing"],
            "running_batch": batch_data,
            "platform_stats": list(
                _svc_production_rawjobs_queryset().values("platform_slug")
                .annotate(count=Count("id"))
                .order_by("-count")
                .values("platform_slug", "count")
            ),
            "insights": _raw_jobs_workflow_insights(stale_pending_hours=6),
            "meta": {
                "cache": "fresh" if (running_batch or running_company_fetch) else "short_ttl",
                "new_today_basis": "last_24h_created",
            },
        }

        fmt = (request.GET.get("format") or "").strip().lower()
        accept = (request.headers.get("Accept") or "").lower()
        is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        wants_json = (fmt == "json") or is_xhr or ("application/json" in accept)

        if wants_json:
            return JsonResponse(payload)

        # HTML stats view is consolidated into Jobs Pipeline (tab=raw).
        return redirect(f"{reverse('jobs-pipeline')}?tab=raw")


@method_decorator(never_cache, name="dispatch")
class RawJobWorkflowInsightsView(SuperuserRequiredMixin, View):
    """JSON endpoint — queue/quality/funnel/platform-health insights for control tabs."""

    def get(self, request):
        stale_hours_raw = (request.GET.get("stale_hours") or "6").strip()
        stale_hours = int(stale_hours_raw) if stale_hours_raw.isdigit() else 6
        stale_hours = max(1, min(72, stale_hours))
        return JsonResponse(
            {
                "ok": True,
                "insights": _raw_jobs_workflow_insights(stale_pending_hours=stale_hours),
            }
        )


@method_decorator(never_cache, name="dispatch")
class BoardAnalyticsDashboardView(SuperuserRequiredMixin, View):
    """
    GET /harvest/board-analytics/ — HTML dashboard with sortable platform table.
    """

    def get(self, request):
        from .board_analytics import get_board_analytics
        try:
            window = int(request.GET.get("window_days", 30))
            window = max(1, min(90, window))
        except (ValueError, TypeError):
            window = 30

        from .board_analytics import MIN_RUNS_FOR_RISK
        force_refresh = request.GET.get("refresh") == "1"
        data = get_board_analytics(window_days=window, force_refresh=force_refresh)
        return render(request, "harvest/board_analytics.html", {
            "data": data,
            "window_choices": [7, 14, 30, 60, 90],
            "min_runs_for_risk": MIN_RUNS_FOR_RISK,
            "force_refresh": force_refresh,
        })


class BoardAnalyticsView(SuperuserRequiredMixin, View):
    """
    GET /harvest/api/board-analytics/?window_days=30

    Returns unified per-platform analytics as JSON.  All "blocked" and "jobs" metrics
    share the same denominator (total RawJob rows) so impossible ratios cannot occur.

    Query params:
      window_days  – run-history look-back window (default 30, max 90)
    """

    def get(self, request):
        from .board_analytics import get_board_analytics
        try:
            window = int(request.GET.get("window_days", 30))
            window = max(1, min(90, window))
        except (ValueError, TypeError):
            window = 30

        force_refresh = request.GET.get("refresh") == "1"
        data = get_board_analytics(window_days=window, force_refresh=force_refresh)
        return JsonResponse({"ok": True, **data})


class BoardDrillDownView(SuperuserRequiredMixin, View):
    """
    GET /harvest/board-analytics/<slug>/
    Per-platform drill-down: run history, field coverage trend, recent error messages.
    """

    def get(self, request, slug: str):
        from .models import CompanyFetchRun, RawJob, JobBoardPlatform
        from .board_capabilities import get_capabilities
        from datetime import timedelta
        from django.utils import timezone
        from django.db.models import Count, Avg, Max, Q

        try:
            window = int(request.GET.get("window_days", 30))
            window = max(1, min(90, window))
        except (ValueError, TypeError):
            window = 30

        now = timezone.now()
        run_window = now - timedelta(days=window)

        # Platform meta
        platform = JobBoardPlatform.objects.filter(slug=slug).first()

        # Recent runs for this platform
        recent_runs = (
            CompanyFetchRun.objects
            .filter(started_at__gte=run_window, label__platform__slug=slug)
            .order_by("-started_at")
            .values(
                "id", "status", "error_type", "issue_code", "error_message",
                "jobs_found", "jobs_new", "jobs_updated", "jobs_duplicate",
                "jobs_total_available", "is_test_run", "jobs_cap_applied",
                "started_at", "completed_at", "field_presence",
                "label__company__name",
            )[:100]
        )

        # Aggregate stats from runs
        run_agg = (
            CompanyFetchRun.objects
            .filter(started_at__gte=run_window, label__platform__slug=slug)
            .aggregate(
                total=Count("id"),
                success=Count("id", filter=Q(status="SUCCESS")),
                empty=Count("id", filter=Q(status="EMPTY")),
                failed=Count("id", filter=Q(status="FAILED")),
                partial=Count("id", filter=Q(status="PARTIAL")),
                avg_jobs=Avg("jobs_found"),
            )
        )

        # RawJob totals for this platform
        job_qs = RawJob.objects.filter(platform_slug=slug)
        job_totals = job_qs.aggregate(
            total=Count("id"),
            synced=Count("id", filter=Q(sync_status="SYNCED")),
            pending=Count("id", filter=Q(sync_status="PENDING")),
            failed=Count("id", filter=Q(sync_status="FAILED")),
            inactive=Count("id", filter=Q(is_active=False)),
            missing_jd=Count("id", filter=Q(has_description=False)),
            has_salary=Count("id", filter=Q(salary_min__isnull=False) | Q(salary_max__isnull=False)),
        )

        # Blocker breakdown
        _rj_fields = {f.name for f in RawJob._meta.get_fields()}
        blockers = {}
        if "sync_skip_reason" in _rj_fields:
            from django.db.models import Count as C
            blockers = (
                job_qs
                .exclude(sync_skip_reason="")
                .values("sync_skip_reason")
                .annotate(count=Count("id"))
                .order_by("-count")
            )
            blockers = {b["sync_skip_reason"]: b["count"] for b in blockers}

        # Most recent errors
        recent_errors = list(
            CompanyFetchRun.objects
            .filter(
                started_at__gte=run_window,
                label__platform__slug=slug,
                status__in=["FAILED", "PARTIAL"],
            )
            .exclude(error_message="")
            .order_by("-started_at")
            .values("label__company__name", "error_message", "error_type", "issue_code", "started_at")
            [:20]
        )

        caps = get_capabilities(slug)

        context = {
            "slug": slug,
            "platform": platform,
            "window_days": window,
            "window_choices": [7, 14, 30, 60, 90],
            "run_agg": run_agg,
            "recent_runs": list(recent_runs),
            "job_totals": job_totals,
            "blockers": blockers,
            "recent_errors": recent_errors,
            "capabilities": caps,
        }
        return render(request, "harvest/board_drilldown.html", context)


# ── Job Jarvis ─────────────────────────────────────────────────────────────────

@method_decorator(never_cache, name="dispatch")
class JarvisView(SuperuserRequiredMixin, TemplateView):
    """
    GET  → show paste form
    POST → queue jarvis_ingest_task, return JSON {"task_id": "..."}
    """
    template_name = "harvest/jarvis.html"

    def get_context_data(self, **kwargs):
        from django.utils import timezone
        from django.db.models import Count
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "jarvis"

        jarvis_qs = RawJob.objects.filter(platform_slug="jarvis")
        today = timezone.now().date()

        # Core metrics
        ctx["jarvis_total"]   = jarvis_qs.count()
        ctx["jarvis_today"]   = jarvis_qs.filter(fetched_at__date=today).count()
        ctx["jarvis_synced"]  = jarvis_qs.filter(sync_status="SYNCED").count()
        ctx["jarvis_pending"] = jarvis_qs.filter(sync_status="PENDING").count()
        ctx["jarvis_failed"]  = jarvis_qs.filter(sync_status="FAILED").count()
        ctx["jarvis_skipped"] = jarvis_qs.filter(sync_status="SKIPPED").count()
        total = ctx["jarvis_total"] or 1
        ctx["jarvis_success_rate"] = round(ctx["jarvis_synced"] / total * 100)

        # This week
        week_start = today - timezone.timedelta(days=today.weekday())
        ctx["jarvis_this_week"] = jarvis_qs.filter(fetched_at__date__gte=week_start).count()

        # Platform breakdown — single aggregation query, no Python loop over model instances
        ctx["jarvis_platform_breakdown"] = [
            (row["job_platform__name"] or "Unknown", row["count"])
            for row in jarvis_qs
            .values("job_platform__name")
            .annotate(count=Count("id"))
            .order_by("-count")[:6]
        ]

        # Recent imports (more items for the new sidebar)
        ctx["recent_jarvis"] = list(
            jarvis_qs
            .select_related("company", "job_platform")
            .order_by("-fetched_at")[:15]
        )

        ctx["platforms_supported"] = [
            "Greenhouse", "Lever", "Ashby", "Workday", "Workable",
            "Dayforce", "LinkedIn", "Indeed", "SmartRecruiters", "BambooHR", "Any career page",
        ]
        return ctx

    def post(self, request, *args, **kwargs):
        from .tasks import jarvis_ingest_task
        url = request.POST.get("url", "").strip()
        if not url:
            return JsonResponse({"ok": False, "error": "Please paste a job URL."}, status=400)
        if not url.startswith(("http://", "https://")):
            return JsonResponse({"ok": False, "error": "URL must start with http:// or https://"}, status=400)

        task = jarvis_ingest_task.delay(url, request.user.id)
        return JsonResponse({"ok": True, "task_id": task.id, "url": url})


@method_decorator(never_cache, name="dispatch")
class JarvisStatusView(SuperuserRequiredMixin, View):
    """
    GET ?task_id=xxx → poll task state.

    Returns JSON:
      { state, percent, message, result }   # while running
      { state:"SUCCESS", result:{...} }     # when done
      { state:"FAILURE", error:"..." }      # on error
    """

    def get(self, request, *args, **kwargs):
        from celery.result import AsyncResult
        task_id = request.GET.get("task_id", "").strip()
        if not task_id:
            return JsonResponse({"error": "Missing task_id"}, status=400)

        res = AsyncResult(task_id)
        state = res.state  # PENDING / PROGRESS / SUCCESS / FAILURE

        if state == "PENDING":
            return JsonResponse({"state": "PENDING", "percent": 0, "message": "Queued…"})

        if state == "STARTED":
            return JsonResponse({"state": "STARTED", "percent": 12, "message": "Started…"})

        if state == "RETRY":
            return JsonResponse({"state": "RETRY", "percent": 18, "message": "Retrying…"})

        if state == "PROGRESS":
            meta = res.info or {}
            return JsonResponse({
                "state": "PROGRESS",
                "percent": meta.get("percent", 0),
                "message": meta.get("message", "Working…"),
            })

        if state == "SUCCESS":
            result = res.result or {}
            if not isinstance(result, dict):
                result = {"ok": False, "error": str(result)}
            # Fetch fresh raw_job data if saved
            raw_job_data = None
            if result.get("raw_job_id"):
                try:
                    from .harvesters import get_harvester

                    job = RawJob.objects.select_related(
                        "company",
                        "job_platform",
                        "platform_label",
                        "platform_label__platform",
                    ).get(
                        pk=result["raw_job_id"]
                    )
                    payload = job.raw_payload if isinstance(job.raw_payload, dict) else {}
                    existing_live_job = _find_existing_live_job_for_rawjob(job)
                    tenant_id = (
                        payload.get("jarvis_tenant_id")
                        or (job.platform_label.tenant_id if job.platform_label else "")
                        or ""
                    )
                    company_jobs_url = (
                        payload.get("jarvis_company_jobs_url")
                        or (job.platform_label.career_page_url if job.platform_label else "")
                        or ""
                    )
                    support_slug = (
                        payload.get("jarvis_detected_ats")
                        or (job.platform_label.platform.slug if job.platform_label and job.platform_label.platform else "")
                        or (job.job_platform.slug if job.job_platform else "")
                        or ""
                    )
                    fetch_all_supported = bool(
                        tenant_id and support_slug and get_harvester(support_slug) is not None
                    )
                    raw_job_data = {
                        "id": job.pk,
                        "title": job.title,
                        "company_name": job.company.name if job.company else job.company_name,
                        "company_id": job.company_id,
                        "company_url": f"/companies/{job.company_id}/" if job.company_id else "",
                        "location_raw": job.location_raw,
                        "is_remote": job.is_remote,
                        "location_type": job.location_type,
                        "employment_type": job.get_employment_type_display(),
                        "experience_level": job.get_experience_level_display(),
                        "department": job.department,
                        "salary_raw": job.salary_raw,
                        "salary_min": str(job.salary_min) if job.salary_min else None,
                        "salary_max": str(job.salary_max) if job.salary_max else None,
                        "salary_currency": job.salary_currency,
                        "salary_period": job.salary_period,
                        "quality_score": job.quality_score,
                        "description": (job.description or "")[:3000],
                        "platform_slug": job.platform_slug,
                        "platform_name": job.job_platform.name if job.job_platform else (payload.get("jarvis_detected_ats") or job.platform_slug).title(),
                        "detected_ats": payload.get("jarvis_detected_ats", ""),
                        "original_url": job.original_url,
                        "apply_url": job.apply_url,
                        "posted_date": job.posted_date.isoformat() if job.posted_date else "",
                        "sync_status": job.sync_status,
                        "detail_url": f"/harvest/raw-jobs/{job.pk}/",
                        "duplicate_job_id": existing_live_job.pk if existing_live_job else None,
                        "live_job_id": existing_live_job.pk if existing_live_job else None,
                        "live_job_url": f"/jobs/{existing_live_job.pk}/" if existing_live_job else "",
                        "platform_label_id": job.platform_label_id,
                        "tenant_id": tenant_id,
                        "company_jobs_url": company_jobs_url,
                        "fetch_all_supported": fetch_all_supported,
                    }
                except RawJob.DoesNotExist:
                    pass
            return JsonResponse({
                "state": "SUCCESS",
                "result": result,
                "raw_job": raw_job_data,
            })

        if state == "FAILURE":
            return JsonResponse({
                "state": "FAILURE",
                "error": str(res.result) if res.result else "Task failed",
            })

        # REVOKED or other
        return JsonResponse({"state": state})


class JarvisApproveView(SuperuserRequiredMixin, View):
    """POST { raw_job_id } -> sync RawJob to pool then approve to live in one step."""

    def post(self, request, *args, **kwargs):
        from jobs.models import Job

        raw_job_id = (request.POST.get("raw_job_id") or "").strip()
        if not raw_job_id.isdigit():
            return JsonResponse({"ok": False, "error": "Missing or invalid raw_job_id."}, status=400)

        try:
            raw_job = RawJob.objects.select_related("company", "job_platform").get(pk=int(raw_job_id))
        except RawJob.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Raw job not found."}, status=404)

        try:
            job, created_new = _sync_rawjob_to_pool(raw_job, posted_by=request.user)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("Jarvis approve sync failed for raw_job=%s", raw_job.pk)
            return JsonResponse({"ok": False, "error": f"Failed to sync job: {exc}"}, status=500)

        approved_now = False
        if job.status == Job.Status.POOL:
            job.status = Job.Status.OPEN
            job.validated_by = request.user
            job.validation_run_at = timezone.now()
            job.save(update_fields=["status", "validated_by", "validation_run_at", "updated_at"])
            approved_now = True
            try:
                from jobs.notify import notify_new_open_job_to_consultants, notify_job_pool_status

                notify_new_open_job_to_consultants(job)
                notify_job_pool_status(job, approved=True, actor=request.user)
            except Exception:
                logger.exception("Jarvis approve notifications failed for job=%s", job.pk)
            try:
                from jobs.tasks import generate_job_matches_task

                generate_job_matches_task.delay(job.pk, notify=True)
            except Exception:
                logger.exception("Jarvis approve match task dispatch failed for job=%s", job.pk)

        if job.status not in (Job.Status.OPEN, Job.Status.POOL):
            return JsonResponse(
                {
                    "ok": False,
                    "error": f"Job exists but cannot be auto-approved from status {job.status}.",
                    "job_id": job.pk,
                    "job_url": f"/jobs/{job.pk}/",
                },
                status=409,
            )

        return JsonResponse(
            {
                "ok": True,
                "raw_job_id": raw_job.pk,
                "job_id": job.pk,
                "job_url": f"/jobs/{job.pk}/",
                "created_job": created_new,
                "approved_now": approved_now,
                "job_status": job.status,
            }
        )


@method_decorator(never_cache, name="dispatch")
class JarvisFetchCompanyJobsView(SuperuserRequiredMixin, View):
    """
    POST { raw_job_id } → fetch all jobs for the detected company board.

    Uses the existing `fetch_raw_jobs_for_company_task(fetch_all=True)` pipeline
    so results are deduped/upserted into RawJob.
    """

    def post(self, request, *args, **kwargs):
        from .harvesters import get_harvester
        from .tasks import fetch_raw_jobs_for_company_task, _jarvis_ensure_company_platform_label

        raw_job_id = (request.POST.get("raw_job_id") or "").strip()
        if not raw_job_id.isdigit():
            return JsonResponse({"ok": False, "error": "Missing or invalid raw_job_id."}, status=400)

        try:
            raw_job = RawJob.objects.select_related("company", "job_platform").get(pk=int(raw_job_id))
        except RawJob.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Raw job not found."}, status=404)

        if not raw_job.company_id:
            return JsonResponse({"ok": False, "error": "Company mapping is missing. Re-run Jarvis analyze."}, status=400)

        payload = raw_job.raw_payload if isinstance(raw_job.raw_payload, dict) else {}
        detected_ats = (
            payload.get("jarvis_detected_ats")
            or (raw_job.job_platform.slug if raw_job.job_platform else "")
            or ""
        )
        label, board_ctx = _jarvis_ensure_company_platform_label(
            company=raw_job.company,
            detected_ats=detected_ats,
            source_url=raw_job.original_url or payload.get("jarvis_source_url") or "",
            job_platform=raw_job.job_platform,
        )

        if not label or not label.platform:
            return JsonResponse(
                {"ok": False, "error": "Could not resolve ATS platform for this company URL."},
                status=400,
            )
        if not (label.tenant_id or "").strip():
            return JsonResponse(
                {"ok": False, "error": "Tenant could not be derived from this URL yet."},
                status=400,
            )
        if get_harvester(label.platform.slug) is None:
            return JsonResponse(
                {"ok": False, "error": f"Fetch-all is not supported for platform '{label.platform.slug}' yet."},
                status=400,
            )

        # Fetch-all for large Workday boards can exceed the default 8-minute
        # soft limit. Override per-task limits for Jarvis-triggered full crawls.
        task = fetch_raw_jobs_for_company_task.apply_async(
            kwargs={
                "label_pk": label.pk,
                "batch_id": None,
                "triggered_by": "JARVIS",
                "fetch_all": True,
            },
            soft_time_limit=1800,
            time_limit=2100,
        )

        # Keep payload enriched for UI render
        payload["jarvis_platform_label_id"] = label.pk
        payload["jarvis_tenant_id"] = board_ctx.get("tenant_id") or label.tenant_id
        payload["jarvis_company_jobs_url"] = (
            board_ctx.get("company_jobs_url")
            or label.career_page_url
            or ""
        )
        payload["jarvis_fetch_all_supported"] = True
        raw_job.raw_payload = payload
        raw_job.platform_label = label
        raw_job.save(update_fields=["raw_payload", "platform_label", "updated_at"])

        progress_qs = urlencode(
            {
                "task_id": task.id,
                "label_pk": label.pk,
                "raw_job_id": raw_job.pk,
                "company_name": raw_job.company.name if raw_job.company else (raw_job.company_name or ""),
                "platform_slug": label.platform.slug if label.platform else "",
                "company_jobs_url": payload.get("jarvis_company_jobs_url", ""),
            }
        )
        progress_url = f"{reverse('harvest-jarvis-fetch-all-progress')}?{progress_qs}"

        return JsonResponse(
            {
                "ok": True,
                "task_id": task.id,
                "label_pk": label.pk,
                "platform_slug": label.platform.slug,
                "tenant_id": payload.get("jarvis_tenant_id", ""),
                "company_jobs_url": payload.get("jarvis_company_jobs_url", ""),
                "company_id": raw_job.company_id,
                "company_name": raw_job.company.name if raw_job.company else raw_job.company_name,
                "progress_url": progress_url,
            }
        )


@method_decorator(never_cache, name="dispatch")
class JarvisFetchProgressView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/jarvis_fetch_progress.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active_tab"] = "jarvis"
        ctx["task_id"] = self.request.GET.get("task_id", "").strip()
        ctx["label_pk"] = self.request.GET.get("label_pk", "").strip()
        ctx["raw_job_id"] = self.request.GET.get("raw_job_id", "").strip()
        ctx["company_name"] = self.request.GET.get("company_name", "").strip()
        ctx["platform_slug"] = self.request.GET.get("platform_slug", "").strip()
        ctx["company_jobs_url"] = self.request.GET.get("company_jobs_url", "").strip()
        ctx["jarvis_url"] = reverse("harvest-jarvis")
        ctx["rawjobs_url"] = f"{reverse('jobs-pipeline')}?tab=raw"
        ctx["progress_api_url"] = reverse("harvest-jarvis-fetch-all-progress-api")
        return ctx


@method_decorator(never_cache, name="dispatch")
class JarvisFetchProgressApiView(SuperuserRequiredMixin, View):
    """Live status payload for the Jarvis fetch-all progress page."""

    def get(self, request, *args, **kwargs):
        from celery.result import AsyncResult

        task_id = request.GET.get("task_id", "").strip()
        if not task_id:
            return JsonResponse({"ok": False, "error": "Missing task_id."}, status=400)

        async_res = AsyncResult(task_id)
        celery_state = (async_res.state or "").upper()
        run = (
            CompanyFetchRun.objects.select_related("label__company", "label__platform")
            .filter(task_id=task_id)
            .order_by("-started_at")
            .first()
        )

        if run:
            state = run.status
            running = state == CompanyFetchRun.Status.RUNNING
            done = state in {
                CompanyFetchRun.Status.SUCCESS,
                CompanyFetchRun.Status.PARTIAL,
                CompanyFetchRun.Status.FAILED,
                CompanyFetchRun.Status.SKIPPED,
            }
            found = int(run.jobs_found or 0)
            processed = int(run.jobs_new + run.jobs_updated + run.jobs_duplicate + run.jobs_failed)
            if done:
                percent = 100
            elif found > 0:
                percent = max(8, min(95, int((processed / max(found, 1)) * 100)))
            elif celery_state == "STARTED":
                percent = 10
            elif celery_state == "RETRY":
                percent = 18
            elif celery_state == "PROGRESS" and isinstance(async_res.info, dict):
                percent = int(async_res.info.get("percent", 15) or 15)
            else:
                percent = 4

            if done:
                if state == CompanyFetchRun.Status.SUCCESS:
                    message = (
                        "Company fetch complete."
                        if found > 0
                        else "Company fetch complete (no jobs returned from board)."
                    )
                elif state == CompanyFetchRun.Status.PARTIAL:
                    if int(run.jobs_failed or 0) > 0:
                        message = f"Company fetch completed with partial failures ({run.jobs_failed} failed)."
                    elif run.error_message:
                        message = run.error_message
                    else:
                        message = "Company fetch completed with warnings."
                elif state == CompanyFetchRun.Status.SKIPPED:
                    message = run.error_message or "Company fetch skipped."
                else:
                    message = run.error_message or "Company fetch failed."
            else:
                if found > 0:
                    message = f"Processing jobs… {processed}/{found}"
                else:
                    message = "Discovering jobs from company board…"

            recent_qs = RawJob.objects.filter(platform_label_id=run.label_id)
            if run.started_at:
                recent_qs = recent_qs.filter(updated_at__gte=run.started_at - timedelta(seconds=3))
            recent_jobs = list(
                recent_qs.order_by("-updated_at").values(
                    "id",
                    "title",
                    "company_name",
                    "location_raw",
                    "sync_status",
                    "updated_at",
                    "original_url",
                )[:20]
            )
            jobs_payload = [
                {
                    "id": row["id"],
                    "title": row["title"] or "Untitled role",
                    "company_name": row["company_name"] or (run.label.company.name if run.label and run.label.company else ""),
                    "location_raw": row["location_raw"] or "",
                    "sync_status": row["sync_status"] or "PENDING",
                    "detail_url": f"/harvest/raw-jobs/{row['id']}/",
                    "original_url": row["original_url"] or "",
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
                }
                for row in recent_jobs
            ]

            rawjobs_qs = {
                "tab": "raw",
                "q": run.label.company.name if run.label and run.label.company else "",
            }
            if run.label and run.label.company_id:
                rawjobs_qs["company_id"] = str(run.label.company_id)
            if run.label and run.label.platform:
                rawjobs_qs["platform"] = run.label.platform.slug
            if run.label_id:
                rawjobs_qs["label_pk"] = str(run.label_id)
            rawjobs_url = f"{reverse('jobs-pipeline')}?{urlencode(rawjobs_qs)}"

            return JsonResponse(
                {
                    "ok": True,
                    "task_id": task_id,
                    "celery_state": celery_state,
                    "state": state,
                    "running": running,
                    "done": done,
                    "percent": percent,
                    "message": message,
                    "company_name": run.label.company.name if run.label and run.label.company else "",
                    "platform_slug": run.label.platform.slug if run.label and run.label.platform else "",
                    "company_jobs_url": run.label.career_page_url or "",
                    "counts": {
                        "found": found,
                        "new": int(run.jobs_new),
                        "updated": int(run.jobs_updated),
                        "duplicate": int(run.jobs_duplicate),
                        "skipped": int(run.jobs_duplicate),
                        "failed": int(run.jobs_failed),
                    },
                    "run": {
                        "id": run.pk,
                        "label_pk": run.label_id,
                        "started_at": run.started_at.isoformat() if run.started_at else "",
                        "completed_at": run.completed_at.isoformat() if run.completed_at else "",
                        "error_message": run.error_message or "",
                    },
                    "recent_jobs": jobs_payload,
                    "rawjobs_url": rawjobs_url,
                }
            )

        # Fallback before run record is created.
        if celery_state in {"PENDING", "STARTED", "RETRY", "PROGRESS"}:
            percent_map = {"PENDING": 2, "STARTED": 10, "RETRY": 18}
            percent = percent_map.get(celery_state, 15)
            message = "Queued…" if celery_state == "PENDING" else "Starting company fetch…"
            if celery_state == "PROGRESS" and isinstance(async_res.info, dict):
                percent = int(async_res.info.get("percent", percent) or percent)
                message = async_res.info.get("message", message)
            return JsonResponse(
                {
                    "ok": True,
                    "task_id": task_id,
                    "state": celery_state,
                    "celery_state": celery_state,
                    "running": True,
                    "done": False,
                    "percent": max(1, min(95, percent)),
                    "message": message,
                    "counts": {"found": 0, "new": 0, "updated": 0, "duplicate": 0, "skipped": 0, "failed": 0},
                    "recent_jobs": [],
                    "rawjobs_url": f"{reverse('jobs-pipeline')}?tab=raw",
                }
            )

        if celery_state == "SUCCESS":
            result = async_res.result if isinstance(async_res.result, dict) else {}
            return JsonResponse(
                {
                    "ok": True,
                    "task_id": task_id,
                    "state": "SUCCESS",
                    "celery_state": "SUCCESS",
                    "running": False,
                    "done": True,
                    "percent": 100,
                    "message": "Company fetch complete.",
                    "counts": {
                        "found": int(result.get("jobs_found", 0) or 0),
                        "new": int(result.get("jobs_new", 0) or 0),
                        "updated": int(result.get("jobs_updated", 0) or 0),
                        "duplicate": 0,
                        "skipped": 0,
                        "failed": int(result.get("jobs_failed", 0) or 0),
                    },
                    "recent_jobs": [],
                    "rawjobs_url": f"{reverse('jobs-pipeline')}?tab=raw",
                }
            )

        err = str(async_res.result) if async_res.result else "Company fetch failed."
        return JsonResponse(
            {
                "ok": True,
                "task_id": task_id,
                "state": celery_state or "FAILURE",
                "celery_state": celery_state or "FAILURE",
                "running": False,
                "done": True,
                "percent": 100,
                "message": err,
                "counts": {"found": 0, "new": 0, "updated": 0, "duplicate": 0, "skipped": 0, "failed": 1},
                "recent_jobs": [],
                "rawjobs_url": f"{reverse('jobs-pipeline')}?tab=raw",
            }
        )


class JarvisReScrapeView(SuperuserRequiredMixin, View):
    """POST { url } → re-run Jarvis on a fresh URL without saving (preview only)."""

    def post(self, request, *args, **kwargs):
        """Synchronous quick-preview — no DB write, just extract + return JSON."""
        from .jarvis import JobJarvis
        url = request.POST.get("url", "").strip()
        if not url:
            return JsonResponse({"ok": False, "error": "Missing URL"}, status=400)

        try:
            data = JobJarvis().ingest(url)
        except Exception as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=500)

        # Strip raw_payload (too large) from response
        data.pop("raw_payload", None)
        return JsonResponse({"ok": not bool(data.get("error")), "data": data})


# ── Setup Celery Beat Schedule ─────────────────────────────────────────────────

class SetupScheduleView(SuperuserRequiredMixin, View):
    """POST — create/update Celery Beat PeriodicTasks for daily auto-harvest."""

    def post(self, request):
        try:
            from django_celery_beat.models import CrontabSchedule, PeriodicTask
            import json as _json

            created = []
            updated = []

            # 1. Daily Quick Sync — 02:00 UTC every day (incremental, since_hours=25)
            quick_cron, _ = CrontabSchedule.objects.get_or_create(
                minute="0", hour="2", day_of_week="*",
                day_of_month="*", month_of_year="*",
            )
            _, was_created = PeriodicTask.objects.update_or_create(
                name="harvest.daily_quick_sync",
                defaults={
                    "crontab": quick_cron,
                    "task": "harvest.fetch_raw_jobs_batch",
                    "kwargs": _json.dumps({"fetch_all": False, "min_hours_since_fetch": 6, "batch_name": "Daily Quick Sync (auto)"}),
                    "enabled": True,
                    "description": "Daily incremental harvest — fetch new/updated jobs from last 25h. Runs at 02:00 UTC.",
                },
            )
            (created if was_created else updated).append("Daily Quick Sync (02:00 UTC)")

            # 2. Weekly Full Crawl — Sunday 03:00 UTC (fetch_all=True, respects 2hr cooldown on GUI only)
            full_cron, _ = CrontabSchedule.objects.get_or_create(
                minute="0", hour="3", day_of_week="0",
                day_of_month="*", month_of_year="*",
            )
            _, was_created = PeriodicTask.objects.update_or_create(
                name="harvest.weekly_full_crawl",
                defaults={
                    "crontab": full_cron,
                    "task": "harvest.fetch_raw_jobs_batch",
                    "kwargs": _json.dumps({"fetch_all": True, "min_hours_since_fetch": 0, "batch_name": "Weekly Full Crawl (auto)"}),
                    "enabled": True,
                    "description": "Weekly full crawl — paginate all jobs from all companies. Runs Sunday 03:00 UTC.",
                },
            )
            (created if was_created else updated).append("Weekly Full Crawl (Sun 03:00 UTC)")

            # 3. Daily sync to pool — 04:00 UTC (after harvest settles)
            sync_cron, _ = CrontabSchedule.objects.get_or_create(
                minute="0", hour="4", day_of_week="*",
                day_of_month="*", month_of_year="*",
            )
            _, was_created = PeriodicTask.objects.update_or_create(
                name="harvest.daily_pool_sync",
                defaults={
                    "crontab": sync_cron,
                    "task": "harvest.sync_harvested_to_pool",
                    "kwargs": _json.dumps({"max_jobs": 5000}),
                    "enabled": True,
                    "description": "Daily pool sync — promote up to 5,000 pending RawJobs to the job pool. Runs at 04:00 UTC.",
                },
            )
            (created if was_created else updated).append("Daily Pool Sync (04:00 UTC)")

            # 4. Daily JD backfill — 05:00 UTC
            backfill_cron, _ = CrontabSchedule.objects.get_or_create(
                minute="0", hour="5", day_of_week="*",
                day_of_month="*", month_of_year="*",
            )
            _, was_created = PeriodicTask.objects.update_or_create(
                name="harvest.daily_jd_backfill",
                defaults={
                    "crontab": backfill_cron,
                    "task": "harvest.backfill_descriptions",
                    "kwargs": _json.dumps({"batch_size": 200, "parallel_workers": 1}),
                    "enabled": True,
                    "description": "Daily JD backfill — fill missing descriptions. Runs at 05:00 UTC.",
                },
            )
            (created if was_created else updated).append("Daily JD Backfill (05:00 UTC)")

            msg_parts = []
            if created:
                msg_parts.append(f"Created: {', '.join(created)}")
            if updated:
                msg_parts.append(f"Updated: {', '.join(updated)}")
            messages.success(request, "Schedule configured. " + " | ".join(msg_parts))

        except ImportError:
            messages.error(request, "django-celery-beat is not installed. Add it to requirements.txt.")
        except Exception as exc:
            messages.error(request, f"Schedule setup failed: {exc}")

        return redirect("harvest-schedule")


# ── Harvest Engine Config ──────────────────────────────────────────────────────

class EngineConfigView(SuperuserRequiredMixin, View):
    """GET = show engine config GUI. POST = save + broadcast rate limit live."""
    template_name = "harvest/settings_engine.html"

    @staticmethod
    def _geocoding_stats(cfg) -> dict:
        """Build the dashboard data for the Location Resolver & Mapbox card.

        Returns:
            {
              "provider": "mapbox" | "google" | "none",
              "provider_enabled": bool,
              "monthly_limit": int,
              "provider_monthly_used": int,
              "provider_monthly_pct": float,    # for progress bar
              "tone": "good" | "warn" | "bad",
              "token_present": bool,
              "token_env_var": "MAPBOX_ACCESS_TOKEN",
              "cache_total": int,
              "cache_resolved": int,
              "cache_unknown": int,
              "cache_failed": int,
              "by_source": {"rules": N, "city_dict": N, "provider": N, ...},
              "by_country_top": [{"country_code": "US", "count": N}, ...],
              "rate_remaining": int,
              "rate_remaining_pct": float,
            }
        """
        import os
        from django.db.models import Count
        from .location_resolver import provider_quota_status
        from .models import LocationCache

        provider = (cfg.geocoding_provider or "none").strip().lower()
        provider_enabled = bool(cfg.geocoding_provider_enabled)

        # Provider call counter (per calendar month) — only counts rows that
        # actually hit a provider (provider_place_id non-empty).
        try:
            quota = provider_quota_status(cfg)
        except Exception:
            quota = {
                "monthly_limit": int(cfg.geocoding_monthly_limit or 0),
                "monthly_used": 0,
                "monthly_pct": 0.0,
                "hourly_limit": int(getattr(cfg, "geocoding_hourly_limit", 0) or 0),
                "hourly_used": 0,
                "hourly_pct": 0.0,
                "warning_pct": int(getattr(cfg, "geocoding_warning_pct", 80) or 80),
                "monthly_warning": False,
                "hourly_warning": False,
                "monthly_exhausted": False,
                "hourly_exhausted": False,
            }
        monthly_limit = int(quota.get("monthly_limit") or 0)
        used = int(quota.get("monthly_used") or 0)
        pct = float(quota.get("monthly_pct") or 0.0)
        hourly_limit = int(quota.get("hourly_limit") or 0)
        hourly_used = int(quota.get("hourly_used") or 0)
        hourly_pct = float(quota.get("hourly_pct") or 0.0)
        warning_pct = int(quota.get("warning_pct") or 80)
        if quota.get("monthly_exhausted") or quota.get("hourly_exhausted") or pct >= 90 or hourly_pct >= 90:
            tone = "bad"
        elif quota.get("monthly_warning") or quota.get("hourly_warning") or pct >= 70 or hourly_pct >= 70:
            tone = "warn"
        else:
            tone = "good"

        # Token presence — check DB first, env var second. Never expose value.
        token_env_var = "MAPBOX_ACCESS_TOKEN" if provider == "mapbox" else (
            "GOOGLE_MAPS_API_KEY" if provider == "google" else ""
        )
        db_token_raw = (cfg.geocoding_provider_token or "").strip()
        env_token_present = bool(os.getenv(token_env_var, "").strip()) if token_env_var else False
        db_token_present = bool(db_token_raw)
        token_present = db_token_present or env_token_present
        if db_token_present:
            token_source = "db"
        elif env_token_present:
            token_source = "env"
        else:
            token_source = "none"
        # Mask the DB token for display: first 6 + last 4 chars only.
        if db_token_present and len(db_token_raw) >= 12:
            token_masked = f"{db_token_raw[:6]}…{db_token_raw[-4:]}"
        elif db_token_present:
            token_masked = "•" * len(db_token_raw)
        else:
            token_masked = ""

        # LocationCache stats — single aggregate query, no per-row work.
        try:
            cache_qs = LocationCache.objects.all()
            cache_total = cache_qs.count()
            by_status = dict(
                cache_qs.values("status").annotate(c=Count("id")).values_list("status", "c")
            )
            by_source = dict(
                cache_qs.exclude(source="")
                .values("source").annotate(c=Count("id")).values_list("source", "c")
            )
            by_country_top = list(
                cache_qs.exclude(country_code="")
                .values("country_code").annotate(c=Count("id"))
                .order_by("-c")[:8]
            )
            for row in by_country_top:
                row["count"] = row.pop("c")
        except Exception:
            cache_total = 0
            by_status, by_source, by_country_top = {}, {}, []

        return {
            "provider": provider,
            "provider_enabled": provider_enabled,
            "monthly_limit": monthly_limit,
            "provider_monthly_used": used,
            "provider_monthly_pct": pct,
            "hourly_limit": hourly_limit,
            "provider_hourly_used": hourly_used,
            "provider_hourly_pct": hourly_pct,
            "warning_pct": warning_pct,
            "monthly_warning": bool(quota.get("monthly_warning")),
            "hourly_warning": bool(quota.get("hourly_warning")),
            "monthly_exhausted": bool(quota.get("monthly_exhausted")),
            "hourly_exhausted": bool(quota.get("hourly_exhausted")),
            "tone": tone,
            "token_present": token_present,
            "token_env_var": token_env_var,
            "token_source": token_source,         # "db" | "env" | "none"
            "token_masked": token_masked,         # safe to display, e.g. "pk.eyJ…SFmQ"
            "db_token_present": db_token_present,
            "env_token_present": env_token_present,
            "cache_total": cache_total,
            "cache_resolved": by_status.get("RESOLVED", 0),
            "cache_unknown": by_status.get("UNKNOWN", 0),
            "cache_failed": by_status.get("FAILED", 0),
            "cache_rate_limited": by_status.get("RATE_LIMITED", 0),
            "by_source": by_source,
            "by_country_top": by_country_top,
            "rate_remaining": max(0, monthly_limit - used) if monthly_limit else 0,
            "hourly_remaining": max(0, hourly_limit - hourly_used) if hourly_limit else 0,
            "rate_remaining_pct": round(100.0 - pct, 1) if monthly_limit else 0.0,
        }

    def get(self, request, *args, **kwargs):
        import os
        from django.template.response import TemplateResponse
        cfg = HarvestEngineConfig.get()

        # Detect server CPU count for the advisory note
        cpu_count = os.cpu_count() or 2
        recommended_concurrency = min(2, max(1, cpu_count))

        # Try to inspect running Celery workers
        worker_stats = {}
        try:
            from celery import current_app
            inspect = current_app.control.inspect(timeout=1.5)
            stats = inspect.stats() or {}
            for worker_name, info in stats.items():
                pool = info.get("pool", {})
                worker_stats[worker_name] = {
                    "concurrency": pool.get("max-concurrency", "?"),
                    "processes": len(pool.get("processes", [])),
                    "queues": [q["name"] for q in info.get("consumer", {}).get("queues", [])],
                }
        except Exception:
            pass

        country_options = [
            ("US", "United States"),
            ("IN", "India"),
            ("GB", "United Kingdom"),
            ("AU", "Australia"),
            ("CA", "Canada"),
        ]
        selected_countries = cfg.get_target_countries()
        geocoding_stats = self._geocoding_stats(cfg)

        ctx = {
            "cfg": cfg,
            "cpu_count": cpu_count,
            "recommended_concurrency": recommended_concurrency,
            "worker_stats": worker_stats,
            "active_tab": "engine",
            "concurrency_presets": [1, 2],
            "country_options": country_options,
            "selected_countries": selected_countries,
            "geocoding_stats": geocoding_stats,
            # Backwards-compat for existing template fragments:
            "provider_monthly_used": geocoding_stats["provider_monthly_used"],
            **_full_crawl_cooldown_ctx(),
        }
        return TemplateResponse(request, self.template_name, ctx)

    def post(self, request, *args, **kwargs):
        cfg = HarvestEngineConfig.get()

        # Integer fields
        int_fields = [
            "worker_concurrency", "task_rate_limit",
            "api_stagger_ms", "scraper_stagger_ms",
            "min_hours_since_fetch", "task_soft_time_limit_secs",
            "resume_jd_min_words", "resume_jd_min_chars",
            "geocoding_monthly_limit", "geocoding_hourly_limit", "geocoding_warning_pct",
            "jd_backfill_lock_stale_minutes", "portal_health_failure_threshold",
            "zero_tech_threshold", "zero_tech_skip_ttl_days", "cold_no_match_sample_rate_pct",
        ]
        errors = []
        for field in int_fields:
            val = request.POST.get(field, "").strip()
            if val:
                try:
                    setattr(cfg, field, int(val))
                except (ValueError, TypeError):
                    errors.append(f"{field}: must be a whole number")

        # Float fields
        float_fields = ["resume_jd_min_classification_confidence", "ready_stage_min_confidence"]
        for field in float_fields:
            val = request.POST.get(field, "").strip()
            if val:
                try:
                    fval = float(val)
                    if field in {"resume_jd_min_classification_confidence", "ready_stage_min_confidence"} and not (0.0 <= fval <= 1.0):
                        raise ValueError
                    setattr(cfg, field, fval)
                except (ValueError, TypeError):
                    errors.append(f"{field}: must be a number (0 to 1)")

        # Boolean (checkbox) fields — unchecked checkboxes send no value, so
        # we must explicitly set False when the key is absent from POST.
        bool_fields = [
            "auto_backfill_jd", "auto_enrich", "auto_sync_to_pool",
            "process_unknown_country_with_target_domain",
            "geocoding_cache_enabled", "geocoding_provider_enabled",
            "legacy_hash_bridge_enabled",
            "rescope_on_target_country_change",
            "selective_filter_enabled", "filter_audit_mode",
        ]
        for field in bool_fields:
            setattr(cfg, field, field in request.POST)

        hard_negatives_raw = request.POST.get("hard_negative_phrases", "")
        if hard_negatives_raw:
            cfg.hard_negative_phrases = [
                line.strip().lower()
                for line in hard_negatives_raw.splitlines()
                if line.strip()
            ]

        target_countries = [
            code.strip().upper()
            for code in request.POST.getlist("target_countries")
            if code.strip()
        ]
        cfg.target_countries = target_countries

        provider = (request.POST.get("geocoding_provider") or "none").strip().lower()
        if provider in {"none", "mapbox", "google"}:
            cfg.geocoding_provider = provider
        else:
            errors.append("geocoding_provider: unsupported provider")

        # ── Provider token (rotatable from GUI) ────────────────────────────
        # Three actions on the token:
        #   action=keep    — leave existing token unchanged (default if no value)
        #   action=update  — replace with the value in geocoding_provider_token
        #   action=clear   — wipe DB token (resolver falls back to env var)
        token_action = (request.POST.get("geocoding_provider_token_action") or "keep").strip().lower()
        if token_action == "clear":
            cfg.geocoding_provider_token = ""
        elif token_action == "update":
            new_token = (request.POST.get("geocoding_provider_token") or "").strip()
            if not new_token:
                errors.append("geocoding_provider_token: cannot save empty token (use Clear instead)")
            elif len(new_token) > 512:
                errors.append("geocoding_provider_token: too long (max 512 chars)")
            elif provider == "mapbox" and not (new_token.startswith("pk.") or new_token.startswith("sk.")):
                errors.append("geocoding_provider_token: Mapbox tokens start with 'pk.' or 'sk.'")
            else:
                cfg.geocoding_provider_token = new_token
        # "keep" → no-op

        if errors:
            messages.error(request, " | ".join(errors))
        else:
            cfg.updated_by = request.user
            cfg.save()  # triggers Celery broadcast for rate_limit
            messages.success(
                request,
                "Engine config saved. Rate limit applied to running workers immediately. "
                "Resume JD gate thresholds apply immediately. "
                "Pipeline funnel toggles and stagger changes apply on the next batch run.",
            )

        return redirect("harvest-engine-config")


# ── Selective Harvest Review UI ──────────────────────────────────────────────

class SelectiveRoleCategoryListView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/role_categories.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        since_7d = timezone.now() - timedelta(days=7)
        since_30d = timezone.now() - timedelta(days=30)
        stats_7d = {
            row["role_category"]: row["n"]
            for row in RawJob.objects.filter(fetched_at__gte=since_7d)
            .exclude(role_category__isnull=True)
            .values("role_category")
            .annotate(n=Count("id"))
        }
        stats_30d = {
            row["role_category"]: row["n"]
            for row in RawJob.objects.filter(fetched_at__gte=since_30d)
            .exclude(role_category__isnull=True)
            .values("role_category")
            .annotate(n=Count("id"))
        }
        categories = []
        for cat in HarvestRoleCategory.objects.order_by("priority", "name"):
            categories.append({
                "obj": cat,
                "include_count": len(cat.include_phrases or []),
                "exclude_count": len(cat.exclude_phrases or []),
                "matched_7d": stats_7d.get(cat.slug, 0),
                "matched_30d": stats_30d.get(cat.slug, 0),
            })
        ctx["categories"] = categories
        # Missed-titles review: what the filter skipped recently, grouped —
        # one-click 'add as phrase' turns misses into phrase-bank improvements.
        from datetime import timedelta as _td
        from .models import HarvestSkippedTitle
        ctx["missed_titles"] = list(
            HarvestSkippedTitle.objects.filter(
                skipped_at__gte=timezone.now() - _td(days=14))
            .values("job_title")
            .annotate(n=Count("id"))
            .order_by("-n")[:25]
        )
        return ctx


class SelectiveRoleCategoryCreateView(SuperuserRequiredMixin, CreateView):
    model = HarvestRoleCategory
    form_class = HarvestRoleCategoryForm
    template_name = "harvest/role_category_form.html"
    success_url = reverse_lazy("harvest-role-categories")

    def form_valid(self, form):
        messages.success(self.request, "Role category created.")
        return super().form_valid(form)


class SelectiveRoleCategoryUpdateView(SuperuserRequiredMixin, UpdateView):
    model = HarvestRoleCategory
    form_class = HarvestRoleCategoryForm
    template_name = "harvest/role_category_form.html"
    success_url = reverse_lazy("harvest-role-categories")

    def form_valid(self, form):
        messages.success(self.request, "Role category saved.")
        return super().form_valid(form)


def _queue_job_domain_downstream_apply() -> tuple[str, str]:
    """Start the full taxonomy/role refresh, reusing the Jobs classify lock."""
    from django.core.cache import cache
    from jobs.tasks import (
        CLASSIFY_ACTIVE_TASK_KEY,
        CLASSIFY_LOCK_KEY,
        _classify_lock_ttl,
        classify_jobs_task,
    )

    active_task_id = cache.get(CLASSIFY_ACTIVE_TASK_KEY) or cache.get(CLASSIFY_LOCK_KEY)
    if active_task_id:
        return "already_running", str(active_task_id)
    result = classify_jobs_task.apply_async(kwargs={"force_reclassify": True, "active_only": True})
    cache.set(CLASSIFY_ACTIVE_TASK_KEY, result.id, _classify_lock_ttl())
    return "started", result.id


def _job_domain_impact_snapshot(limit: int = 8) -> dict:
    from jobs.models import Job
    from users.models import MarketingRole
    from .enrichments import CURRENT_DOMAIN_VERSION

    active_slugs = list(JobDomain.objects.filter(is_active=True).values_list("slug", flat=True))
    active_role_slugs = set(MarketingRole.objects.filter(is_active=True).values_list("slug", flat=True))
    inactive_role_slugs = set(MarketingRole.objects.filter(is_active=False).values_list("slug", flat=True))

    raw_base = RawJob.objects.filter(is_test_run=False, is_active=True)
    stale_q = Q(job_domain="")
    if active_slugs:
        stale_q |= (~Q(job_domain__in=active_slugs) & ~Q(job_domain=""))

    active_jobs = Job.objects.filter(
        is_archived=False,
        status__in=[Job.Status.POOL, Job.Status.OPEN],
    )
    samples = list(
        raw_base.filter(stale_q)
        .order_by("-fetched_at", "-updated_at", "-id")
        .values("id", "title", "company_name", "job_domain", "sync_status")[:limit]
    )
    return {
        "active_rules": len(active_slugs),
        "rules_missing_marketing_role": JobDomain.objects.filter(is_active=True)
        .exclude(slug__in=active_role_slugs)
        .count(),
        "rules_with_inactive_marketing_role": JobDomain.objects.filter(
            is_active=True,
            slug__in=inactive_role_slugs,
        ).count(),
        "active_raw_jobs_to_recheck": raw_base.count(),
        "raw_jobs_without_current_rule": raw_base.filter(stale_q).count(),
        "raw_jobs_with_old_domain_version": raw_base.exclude(domain_version=CURRENT_DOMAIN_VERSION).count(),
        "synced_raw_jobs_to_refresh": raw_base.filter(sync_status=RawJob.SyncStatus.SYNCED).count(),
        "open_or_pool_jobs_to_refresh": active_jobs.filter(source_raw_job__isnull=False).distinct().count(),
        "open_or_pool_jobs_without_roles": active_jobs.filter(marketing_roles__isnull=True).distinct().count(),
        "samples": samples,
    }


class JobDomainListView(SuperuserRequiredMixin, ListView):
    """List role routing rules backed by JobDomain regex patterns."""
    model = JobDomain
    template_name = "harvest/job_domain_list.html"
    context_object_name = "domains"
    paginate_by = 50

    def get_queryset(self):
        qs = JobDomain.objects.all().order_by("priority", "slug")
        q = (self.request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(slug__icontains=q)
                | Q(regex_pattern__icontains=q)
                | Q(notes__icontains=q)
            )
        category = (self.request.GET.get("category") or "").strip()
        if category:
            qs = qs.filter(top_category=category)
        status = (self.request.GET.get("status") or "").strip()
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "paused":
            qs = qs.filter(is_active=False)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from users.models import MarketingRole

        domains = list(ctx["domains"])
        slugs = [d.slug for d in domains]
        raw_counts = {
            row["job_domain"]: row["count"]
            for row in RawJob.objects.filter(is_test_run=False, job_domain__in=slugs)
            .values("job_domain")
            .annotate(count=Count("id"))
        }
        synced_counts = {
            row["job_domain"]: row["count"]
            for row in RawJob.objects.filter(
                is_test_run=False,
                sync_status=RawJob.SyncStatus.SYNCED,
                job_domain__in=slugs,
            )
            .values("job_domain")
            .annotate(count=Count("id"))
        }
        role_map = {
            role.slug: role
            for role in MarketingRole.objects.filter(slug__in=slugs)
            .annotate(job_count=Count("jobs", distinct=True), consultant_count=Count("consultants", distinct=True))
        }
        for domain in domains:
            role = role_map.get(domain.slug)
            domain.raw_count = raw_counts.get(domain.slug, 0)
            domain.synced_count = synced_counts.get(domain.slug, 0)
            domain.marketing_role = role
            domain.marketing_role_state = "missing"
            domain.marketing_role_job_count = 0
            domain.marketing_role_consultant_count = 0
            if role:
                domain.marketing_role_state = "ready" if role.is_active else "inactive"
                domain.marketing_role_job_count = role.job_count
                domain.marketing_role_consultant_count = role.consultant_count
        ctx["domains"] = domains
        ctx["top_category_choices"] = JobDomain.TopCategory.choices
        ctx["active_count"] = JobDomain.objects.filter(is_active=True).count()
        ctx["paused_count"] = JobDomain.objects.filter(is_active=False).count()
        ctx["total_count"] = JobDomain.objects.count()
        ctx["impact"] = _job_domain_impact_snapshot(limit=5)
        ctx["filters"] = {
            "q": self.request.GET.get("q", ""),
            "category": self.request.GET.get("category", ""),
            "status": self.request.GET.get("status", ""),
        }
        return ctx


class JobDomainApplyDownstreamView(SuperuserRequiredMixin, View):
    def post(self, request):
        status, task_id = _queue_job_domain_downstream_apply()
        if status == "already_running":
            messages.warning(request, "A taxonomy refresh is already running. Progress will resume on this page.")
        else:
            messages.success(
                request,
                f"Applying routing rules to existing RawJobs and synced Jobs in the background (task {task_id[:8]}).",
            )
        return redirect("harvest-job-domains")


class JobDomainCreateView(SuperuserRequiredMixin, CreateView):
    model = JobDomain
    form_class = JobDomainForm
    template_name = "harvest/job_domain_form.html"
    success_url = reverse_lazy("harvest-job-domains")

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.POST.get("apply_downstream"):
            status, task_id = _queue_job_domain_downstream_apply()
            if status == "already_running":
                messages.warning(self.request, f'Rule "{self.object.name}" saved. A taxonomy refresh is already running.')
            else:
                messages.success(
                    self.request,
                    f'Rule "{self.object.name}" created and downstream refresh started (task {task_id[:8]}).',
                )
        else:
            messages.success(self.request, f'Rule "{self.object.name}" created and synced to Marketing Roles.')
        return response

    def form_invalid(self, form):
        messages.error(self.request, "Fix the errors below before saving.")
        return super().form_invalid(form)


class JobDomainUpdateView(SuperuserRequiredMixin, UpdateView):
    model = JobDomain
    form_class = JobDomainForm
    template_name = "harvest/job_domain_form.html"
    success_url = reverse_lazy("harvest-job-domains")

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.POST.get("apply_downstream"):
            status, task_id = _queue_job_domain_downstream_apply()
            if status == "already_running":
                messages.warning(self.request, f'Rule "{self.object.name}" saved. A taxonomy refresh is already running.')
            else:
                messages.success(
                    self.request,
                    f'Rule "{self.object.name}" saved and downstream refresh started (task {task_id[:8]}).',
                )
        else:
            messages.success(self.request, f'Rule "{self.object.name}" saved and synced to Marketing Roles.')
        return response

    def form_invalid(self, form):
        messages.error(self.request, "Fix the errors below — invalid regex will not be saved.")
        return super().form_invalid(form)


class JobDomainDeleteView(SuperuserRequiredMixin, DeleteView):
    model = JobDomain
    template_name = "harvest/job_domain_confirm_delete.html"
    success_url = reverse_lazy("harvest-job-domains")

    def form_valid(self, form):
        messages.success(self.request, f'Rule "{self.object.name}" deleted.')
        return super().form_valid(form)


class JobDomainQuickUpdateView(SuperuserRequiredMixin, View):
    """
    Inline edits from the Role Routing Rules list — no full-page round trip.
      POST action=toggle   → flip is_active (propagates to the MarketingRole)
      POST priority=<int>  → set match priority
    Returns JSON so the row updates in place, including the live downstream state
    (whether the linked role is active and how many consultants target it).
    """
    def post(self, request, pk):
        from users.models import MarketingRole

        domain = get_object_or_404(JobDomain, pk=pk)
        changed = []

        if (request.POST.get("action") or "").strip() == "toggle":
            domain.is_active = not domain.is_active
            changed.append("is_active")

        new_priority = request.POST.get("priority")
        if new_priority not in (None, ""):
            try:
                domain.priority = max(1, min(9999, int(new_priority)))
                changed.append("priority")
            except (TypeError, ValueError):
                return JsonResponse({"ok": False, "error": "Priority must be a whole number."}, status=400)

        if not changed:
            return JsonResponse({"ok": False, "error": "Nothing to update."}, status=400)

        # save() re-validates the regex, busts caches, and re-syncs the MarketingRole
        # (including propagating the active/paused state down the chain).
        domain.save()

        role = (
            MarketingRole.objects.filter(slug=domain.slug)
            .annotate(consultant_count=Count("consultants", distinct=True))
            .first()
        )
        return JsonResponse({
            "ok": True,
            "is_active": domain.is_active,
            "priority": domain.priority,
            "role_active": bool(role and role.is_active),
            "consultant_count": role.consultant_count if role else 0,
        })


class JobDomainImpactApiView(SuperuserRequiredMixin, View):
    def get(self, request):
        return JsonResponse(_job_domain_impact_snapshot(limit=10))


class JobDomainTestApiView(SuperuserRequiredMixin, View):
    """
    GET /harvest/job-domains/test/?title=...
    Tests a job title against all active role routing patterns and returns matches.
    Used by the Live Title Tester on the Role Routing Rules page.
    """
    def get(self, request):
        from .enrichments import detect_job_domains
        title = request.GET.get("title", "").strip()
        if not title:
            return JsonResponse({"matches": [], "error": "No title provided."})
        matches = detect_job_domains(title, "", "", "", max_matches=10)
        # Also show which pattern matched
        details = []
        try:
            patterns = JobDomain.compiled_patterns()
            title_lower = title.lower()
            for slug, compiled in patterns:
                if compiled.search(title_lower):
                    domain = JobDomain.objects.filter(slug=slug).first()
                    details.append({
                        "slug": slug,
                        "name": domain.name if domain else slug,
                        "matched_on": "title",
                    })
        except Exception:
            pass
        return JsonResponse({"matches": matches, "details": details, "title": title})


class SelectiveTitleTestApiView(SuperuserRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        from .role_filter import classify_title

        title = request.GET.get("title", "")
        department = request.GET.get("department", "")
        label_id = request.GET.get("label_id", "")
        custom_phrases: list[str] = []
        if label_id:
            label = CompanyPlatformLabel.objects.filter(pk=label_id).first()
            if label:
                custom_phrases = label.custom_include_phrases or []
        cfg = HarvestEngineConfig.get()
        categories = list(
            HarvestRoleCategory.objects.filter(is_active=True)
            .order_by("priority", "name")
            .values("name", "slug", "priority", "include_phrases", "exclude_phrases")
        )
        hard_negatives = cfg.hard_negative_phrases if isinstance(cfg.hard_negative_phrases, list) else []
        result = classify_title(
            title=title,
            department=department,
            categories=categories,
            hard_negatives=hard_negatives,
            custom_phrases=custom_phrases,
            snapshot_id=None,
        )
        return JsonResponse({
            "decision": result.decision,
            "category": result.category,
            "matched_phrase": result.matched_phrase,
            "matched_negative": result.matched_negative,
            "reason": result.reason,
            "snapshot_id": result.snapshot_id,
        })


class SkippedTitlesAuditView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/skipped_titles.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = (self.request.GET.get("q") or "").strip()
        platform = (self.request.GET.get("platform") or "").strip()
        decision = (self.request.GET.get("decision") or "").strip().upper()
        sampled = (self.request.GET.get("sampled") or "").strip()
        try:
            days = int((self.request.GET.get("days") or "30") or 30)
        except ValueError:
            days = 30
        days = max(1, min(days, 365))

        qs = HarvestSkippedTitle.objects.select_related("raw_job").filter(
            skipped_at__gte=timezone.now() - timedelta(days=days)
        )
        if q:
            qs = qs.filter(
                Q(job_title__icontains=q)
                | Q(company_name__icontains=q)
                | Q(filter_reason__icontains=q)
                | Q(matched_negative__icontains=q)
            )
        if platform:
            qs = qs.filter(platform_slug=platform)
        if decision in {"COLD", "NO_MATCH"}:
            qs = qs.filter(filter_decision=decision)
        if sampled == "1":
            qs = qs.filter(is_sampled=True)

        paginator = Paginator(qs.order_by("-skipped_at"), 50)
        page_obj = paginator.get_page(self.request.GET.get("page"))
        ctx.update({
            "page_obj": page_obj,
            "rows": page_obj.object_list,
            "q": q,
            "platform": platform,
            "decision": decision,
            "sampled": sampled,
            "days": days,
            "platforms": (
                HarvestSkippedTitle.objects.exclude(platform_slug="")
                .values_list("platform_slug", flat=True)
                .distinct()
                .order_by("platform_slug")
            ),
            "summary": qs.values("filter_decision").annotate(n=Count("id")).order_by("filter_decision"),
        })
        return ctx


class SkippedTitleRecoverView(SuperuserRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        skipped = get_object_or_404(HarvestSkippedTitle.objects.select_related("raw_job"), pk=pk)
        raw_job = skipped.raw_job
        if not raw_job:
            messages.error(request, "Cannot recover this row because its RawJob link is missing.")
            return redirect("harvest-skipped-titles")

        RawJob.objects.filter(pk=raw_job.pk).update(
            is_cold=False,
            jd_fetch_skipped=False,
            filter_decision="POSSIBLE",
            filter_reason="Manually recovered from skipped-title audit",
            jd_backfill_locked_at=None,
        )
        from .tasks import backfill_single_rawjob_description_task

        task = backfill_single_rawjob_description_task.delay(raw_job.pk)
        messages.success(
            request,
            f"RawJob #{raw_job.pk} marked POSSIBLE and queued for single JD fetch ({task.id}).",
        )
        return redirect(request.META.get("HTTP_REFERER") or "harvest-skipped-titles")


class ZeroTechCompaniesView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/zero_tech_companies.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = (self.request.GET.get("q") or "").strip()
        platform = (self.request.GET.get("platform") or "").strip()
        qs = CompanyPlatformLabel.objects.select_related("company", "platform").filter(
            Q(consecutive_zero_tech_fetches__gt=0) | Q(skip_in_selective_harvest=True)
        )
        if q:
            qs = qs.filter(company__name__icontains=q)
        if platform:
            qs = qs.filter(platform__slug=platform)

        paginator = Paginator(
            qs.order_by("-skip_in_selective_harvest", "-consecutive_zero_tech_fetches", "company__name"),
            50,
        )
        page_obj = paginator.get_page(self.request.GET.get("page"))
        labels = list(page_obj.object_list)
        title_map: dict[tuple[str, str], list[HarvestSkippedTitle]] = {}
        if labels:
            company_names = [label.company.name for label in labels if label.company_id]
            platform_slugs = [label.platform.slug for label in labels if label.platform_id]
            recent_titles = (
                HarvestSkippedTitle.objects
                .filter(company_name__in=company_names, platform_slug__in=platform_slugs)
                .order_by("-skipped_at")[:500]
            )
            for row in recent_titles:
                key = (row.company_name, row.platform_slug)
                bucket = title_map.setdefault(key, [])
                if len(bucket) < 10:
                    bucket.append(row)
            for label in labels:
                label.recent_skipped_titles = title_map.get(
                    (label.company.name, label.platform.slug if label.platform_id else ""),
                    [],
                )
        ctx.update({
            "rows": labels,
            "page_obj": page_obj,
            "q": q,
            "platform": platform,
            "platforms": JobBoardPlatform.objects.filter(labels__isnull=False).distinct().order_by("slug"),
        })
        return ctx

    def post(self, request, *args, **kwargs):
        label = get_object_or_404(CompanyPlatformLabel, pk=request.POST.get("label_id"))
        action = request.POST.get("action")
        if action == "skip":
            try:
                days = int(request.POST.get("days") or 30)
            except ValueError:
                days = 30
            days = max(1, min(days, 365))
            label.skip_in_selective_harvest = True
            label.skip_expires_at = timezone.now() + timedelta(days=days)
            label.zero_tech_last_flagged_at = timezone.now()
            label.save(update_fields=["skip_in_selective_harvest", "skip_expires_at", "zero_tech_last_flagged_at"])
            messages.success(request, f"{label.company.name} excluded from selective harvest for {days} days.")
        elif action == "reset":
            label.skip_in_selective_harvest = False
            label.skip_expires_at = None
            label.consecutive_zero_tech_fetches = 0
            label.zero_tech_last_flagged_at = None
            label.save(update_fields=[
                "skip_in_selective_harvest",
                "skip_expires_at",
                "consecutive_zero_tech_fetches",
                "zero_tech_last_flagged_at",
            ])
            messages.success(request, f"{label.company.name} reset and re-included.")
        else:
            messages.error(request, "Unsupported zero-tech action.")
        return redirect(request.META.get("HTTP_REFERER") or "harvest-zero-tech-companies")


class FilterSnapshotListView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/filter_snapshots.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = HarvestFilterSnapshot.objects.select_related("batch").order_by("-taken_at")
        paginator = Paginator(qs, 50)
        page_obj = paginator.get_page(self.request.GET.get("page"))
        ctx.update({
            "rows": page_obj.object_list,
            "page_obj": page_obj,
        })
        return ctx


class FilterSnapshotDetailView(SuperuserRequiredMixin, DetailView):
    model = HarvestFilterSnapshot
    template_name = "harvest/filter_snapshot_detail.html"
    context_object_name = "snapshot"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["categories"] = self.object.get_categories()
        ctx["hard_negatives"] = self.object.get_hard_negatives()
        return ctx


# ── Duplicate Engine Views ────────────────────────────────────────────────────

class DuplicateListView(SuperuserRequiredMixin, TemplateView):
    template_name = "harvest/duplicates.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from django.db.models import Count

        label_filter      = self.request.GET.get("label", "")
        resolution_filter = self.request.GET.get("resolution", "PENDING")
        search            = self.request.GET.get("q", "").strip()

        qs = RawJobDuplicatePair.objects.select_related(
            "primary", "duplicate", "resolved_by",
        )
        if resolution_filter and resolution_filter != "all":
            qs = qs.filter(resolution=resolution_filter)
        if label_filter:
            qs = qs.filter(label=label_filter)
        if search:
            qs = qs.filter(
                Q(primary__title__icontains=search)
                | Q(primary__company_name__icontains=search)
                | Q(duplicate__title__icontains=search)
            )

        # Summary stats
        stats = (
            RawJobDuplicatePair.objects.values("label")
            .annotate(cnt=Count("id"))
            .order_by("-cnt")
        )
        pending_count = RawJobDuplicatePair.objects.filter(
            resolution=DuplicateResolution.PENDING
        ).count()

        ctx.update({
            "pairs":            qs[:200],
            "total_pairs":      RawJobDuplicatePair.objects.count(),
            "pending_count":    pending_count,
            "label_stats":      {s["label"]: s["cnt"] for s in stats},
            "label_choices":    DuplicateLabel.choices,
            "resolution_choices": DuplicateResolution.choices,
            "label_filter":     label_filter,
            "resolution_filter": resolution_filter,
            "search":           search,
            "q":                search,
        })
        return ctx


class DuplicateRunView(SuperuserRequiredMixin, View):
    def post(self, request):
        from .tasks import run_duplicate_detection_task
        raw_limit = request.POST.get("limit", "5000").strip()
        try:
            limit = max(100, min(int(raw_limit), 200_000))
        except (ValueError, TypeError):
            limit = 5000
        company_slug = request.POST.get("company_slug", "").strip()
        task = run_duplicate_detection_task.delay(limit=limit, company_slug=company_slug)
        messages.success(
            request,
            f"Duplicate detection is running in the background (task {task.id[:8]}…). "
            "This may take a few minutes — refresh the page to see new pairs.",
        )
        return redirect_with_task_progress("harvest-duplicates", task.id, "Duplicate detection")


class DuplicateResolveView(SuperuserRequiredMixin, View):
    def post(self, request, pk):
        pair   = get_object_or_404(RawJobDuplicatePair, pk=pk)
        action = request.POST.get("action", "")
        notes  = request.POST.get("notes", "")

        if action == "merge":
            from .duplicate_engine import merge_pair
            result = merge_pair(pair, resolved_by=request.user)
            fields = ", ".join(result["backfilled_fields"]) or "none"
            messages.success(request, f"Merged — duplicate deactivated. Backfilled: {fields}.")

        elif action == "dismiss":
            from .duplicate_engine import dismiss_pair
            dismiss_pair(pair, resolved_by=request.user, notes=notes)
            messages.success(request, "Marked as Keep Both.")

        elif action == "confirm":
            from .duplicate_engine import confirm_pair
            confirm_pair(pair, resolved_by=request.user)
            messages.success(request, "Confirmed as duplicate.")

        elif action == "reopen":
            pair.resolution = DuplicateResolution.PENDING
            pair.resolved_at = None
            pair.resolved_by = None
            pair.save(update_fields=["resolution", "resolved_at", "resolved_by"])
            messages.info(request, "Pair reopened for review.")

        return redirect(request.POST.get("next", "harvest-duplicates"))


class DuplicateBulkResolveView(SuperuserRequiredMixin, View):
    def post(self, request):
        action  = request.POST.get("action", "")
        ids     = request.POST.getlist("pair_ids")
        next_url = request.POST.get("next") or reverse("harvest-duplicates")

        if not ids:
            messages.warning(request, "No pairs selected.")
            return redirect(next_url)

        pairs = RawJobDuplicatePair.objects.filter(pk__in=ids, resolution=DuplicateResolution.PENDING)

        if action == "bulk_merge":
            from .duplicate_engine import merge_pair
            count = 0
            failed = 0
            for pair in pairs:
                try:
                    merge_pair(pair, resolved_by=request.user)
                    count += 1
                except Exception as e:
                    failed += 1
                    logger.warning("merge_pair failed for pair %s: %s", pair.pk, e)
            if count:
                messages.success(request, f"Merged {count} pair{'s' if count != 1 else ''}.")
            if failed:
                messages.warning(request, f"{failed} pair{'s' if failed != 1 else ''} could not be merged.")

        elif action == "bulk_dismiss":
            from django.utils import timezone
            count = pairs.update(
                resolution=DuplicateResolution.DISMISSED,
                resolved_at=timezone.now(),
                resolved_by=request.user,
            )
            messages.success(request, f"Kept both for {count} pair{'s' if count != 1 else ''}.")

        return redirect(next_url)


class UnknownCountryReviewView(SuperuserRequiredMixin, View):
    """Review and action RawJobs with scope_status=REVIEW_UNKNOWN_COUNTRY."""

    template_name = "harvest/unknown_country_review.html"
    PAGE_SIZE = 100

    def _base_qs(self):
        return RawJob.objects.filter(
            scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY
        ).order_by("-fetched_at")

    def get(self, request):
        qs = self._base_qs()

        platform_f = (request.GET.get("platform") or "").strip()
        if platform_f:
            qs = qs.filter(platform_slug=platform_f)

        total = qs.count()

        # Platform breakdown
        by_platform = (
            self._base_qs()
            .values("platform_slug")
            .annotate(count=Count("id"))
            .order_by("-count")[:20]
        )

        # Top location_raw patterns (stripped to first 60 chars)
        from django.db.models.functions import Left
        top_locations = (
            qs.exclude(location_raw="")
            .values("location_raw")
            .annotate(count=Count("id"))
            .order_by("-count")[:30]
        )

        # Pagination
        from django.core.paginator import Paginator
        paginator = Paginator(
            qs.only(
                "id", "company_name", "platform_slug", "title",
                "location_raw", "location_candidates", "country_code",
                "scope_reason", "fetched_at", "original_url",
            ),
            self.PAGE_SIZE,
        )
        try:
            page_obj = paginator.page(int(request.GET.get("page", 1)))
        except Exception:
            page_obj = paginator.page(1)

        platforms = (
            RawJob.objects.filter(scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
            .values_list("platform_slug", flat=True)
            .distinct()
            .order_by("platform_slug")
        )

        return render(request, self.template_name, {
            "total": total,
            "by_platform": by_platform,
            "top_locations": top_locations,
            "page_obj": page_obj,
            "paginator": paginator,
            "platforms": platforms,
            "selected_platform": platform_f,
        })

    def post(self, request):
        action = request.POST.get("action", "")
        ids = [int(x) for x in request.POST.getlist("ids") if x.isdigit()]
        platform_f = (request.POST.get("platform") or "").strip()
        redirect_url = reverse("harvest-unknown-country-review")
        if platform_f:
            redirect_url += f"?platform={platform_f}"

        if not ids and action not in ("refetch_all_provider",):
            messages.warning(request, "No jobs selected.")
            return redirect(redirect_url)

        if action == "mark_target":
            from django.utils import timezone
            updated = RawJob.objects.filter(pk__in=ids).update(
                scope_status=RawJob.ScopeStatus.PRIORITY_TARGET,
                is_priority=True,
                last_scope_evaluated_at=timezone.now(),
            )
            messages.success(request, f"Marked {updated} job(s) as Priority Target.")

        elif action == "mark_cold":
            from django.utils import timezone
            updated = RawJob.objects.filter(pk__in=ids).update(
                scope_status=RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY,
                is_priority=False,
                last_scope_evaluated_at=timezone.now(),
            )
            messages.success(request, f"Marked {updated} job(s) as Cold.")

        elif action == "re_evaluate":
            from .location_resolver import evaluate_rawjob_scope
            from django.utils import timezone
            jobs = RawJob.objects.filter(pk__in=ids).only(
                "id", "location_raw", "location_candidates", "country_codes",
                "country_code", "scope_status", "platform_slug",
            )
            updated = 0
            for job in jobs:
                result = evaluate_rawjob_scope(job, use_provider=False, save=True)
                if result:
                    updated += 1
            messages.success(request, f"Re-evaluated {updated} job(s) (no provider).")

        elif action == "re_evaluate_provider":
            from .location_resolver import evaluate_rawjob_scope
            jobs = RawJob.objects.filter(pk__in=ids).only(
                "id", "location_raw", "location_candidates", "country_codes",
                "country_code", "scope_status", "platform_slug",
            )
            resolved = 0
            for job in jobs:
                result = evaluate_rawjob_scope(job, use_provider=True, save=True)
                if result and result.get("scope_status") == RawJob.ScopeStatus.PRIORITY_TARGET:
                    resolved += 1
            messages.success(
                request,
                f"Provider re-evaluated {len(ids)} job(s). {resolved} resolved to Priority Target.",
            )

        else:
            messages.error(request, f"Unknown action: {action}")


# ── Vet Gate Config ──────────────────────────────────────────────────────────

def _vet_gate_preview_count(cfg) -> dict:
    """Compute how many RawJobs would qualify for sync with these settings."""
    from .models import RawJob
    from django.db.models import Q, Value, F
    from django.db.models.functions import Length, Coalesce

    scope_statuses = [RawJob.ScopeStatus.PRIORITY_TARGET]
    if cfg.allow_unknown_country:
        scope_statuses.append(RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)

    qs = (
        RawJob.objects.filter(
            sync_status="PENDING",
            is_active=True,
            company__isnull=False,
            is_priority=True,
            scope_status__in=scope_statuses,
        )
        .exclude(original_url="")
        .exclude(Q(is_cold=True) | Q(filter_decision="NO_MATCH") | Q(jd_fetch_skipped=True))
    )

    if not cfg.allow_possible_filter:
        qs = qs.filter(filter_decision="STRONG")

    if cfg.blocked_domains and isinstance(cfg.blocked_domains, list):
        qs = qs.exclude(job_domain__in=cfg.blocked_domains)

    if cfg.require_description:
        qs = qs.filter(has_description=True, word_count__gte=cfg.min_word_count)
        qs = qs.annotate(
            _jd_len=Length(Coalesce(F("description_clean"), F("description"), Value("")))
        ).filter(_jd_len__gte=cfg.min_char_count)

    total = qs.count()
    return {"total": total}


class VetGateConfigView(SuperuserRequiredMixin, View):
    """GET = show vet gate config GUI. POST = save."""
    template_name = "harvest/vet_gate_config.html"

    def get(self, request, *args, **kwargs):
        from .models import VetGateConfig
        cfg = VetGateConfig.get()
        preview = _vet_gate_preview_count(cfg)
        return render(request, self.template_name, {
            "cfg": cfg,
            "preview": preview,
            "active_tab": "engine",
        })

    def post(self, request, *args, **kwargs):
        from .models import VetGateConfig
        cfg = VetGateConfig.get()
        cfg.allow_unknown_country = request.POST.get("allow_unknown_country") == "on"
        cfg.allow_possible_filter = request.POST.get("allow_possible_filter") == "on"
        cfg.require_description = request.POST.get("require_description") == "on"
        try:
            cfg.min_word_count = int(request.POST.get("min_word_count") or 80)
        except (ValueError, TypeError):
            cfg.min_word_count = 80
        try:
            cfg.min_char_count = int(request.POST.get("min_char_count") or 400)
        except (ValueError, TypeError):
            cfg.min_char_count = 400
        try:
            cfg.auto_lane_min_vet_priority = float(request.POST.get("auto_lane_min_vet_priority") or 0.75)
        except (ValueError, TypeError):
            cfg.auto_lane_min_vet_priority = 0.75
        try:
            cfg.auto_lane_min_data_quality = float(request.POST.get("auto_lane_min_data_quality") or 0.72)
        except (ValueError, TypeError):
            cfg.auto_lane_min_data_quality = 0.72
        try:
            cfg.auto_lane_min_trust = float(request.POST.get("auto_lane_min_trust") or 0.70)
        except (ValueError, TypeError):
            cfg.auto_lane_min_trust = 0.70
        raw_domains = request.POST.get("blocked_domains_json") or "[]"
        try:
            parsed = json.loads(raw_domains)
            cfg.blocked_domains = parsed if isinstance(parsed, list) else []
        except Exception:
            cfg.blocked_domains = []
        try:
            cfg.default_chunk_size = int(request.POST.get("default_chunk_size") or 500)
        except (ValueError, TypeError):
            cfg.default_chunk_size = 500
        cfg.auto_sync_after_harvest = request.POST.get("auto_sync_after_harvest") == "on"
        cfg.save()
        messages.success(request, "Vet Gate Config saved.")
        return redirect("harvest-vet-gate-config")


class VetGatePreviewView(SuperuserRequiredMixin, View):
    """AJAX endpoint — returns live preview count as JSON."""

    def post(self, request, *args, **kwargs):
        from .models import VetGateConfig

        cfg = VetGateConfig.get()
        # Temporarily apply posted values without saving
        cfg.allow_unknown_country = request.POST.get("allow_unknown_country") == "on"
        cfg.allow_possible_filter = request.POST.get("allow_possible_filter") == "on"
        cfg.require_description = request.POST.get("require_description") == "on"
        try:
            cfg.min_word_count = max(1, int(request.POST.get("min_word_count") or 80))
        except (ValueError, TypeError):
            pass
        try:
            cfg.min_char_count = max(1, int(request.POST.get("min_char_count") or 400))
        except (ValueError, TypeError):
            pass
        raw_domains = request.POST.get("blocked_domains_json") or "[]"
        try:
            parsed = json.loads(raw_domains)
            cfg.blocked_domains = parsed if isinstance(parsed, list) else []
        except Exception:
            cfg.blocked_domains = []

        preview = _vet_gate_preview_count(cfg)
        return JsonResponse(preview)

        return redirect(redirect_url)


# ─── Role Targeting Studio APIs ───────────────────────────────────────────────

class RolePhraseQuickAddView(SuperuserRequiredMixin, View):
    """POST JSON: add/remove a single include/exclude phrase on a category.
    Powers inline chip editing + 'add from missed title' actions."""

    def post(self, request):
        import json as _json
        from .models import HarvestRoleCategory
        from .role_filter import normalize_phrase
        try:
            body = _json.loads(request.body)
        except Exception:
            return JsonResponse({"ok": False, "error": "invalid json"}, status=400)
        try:
            cat = HarvestRoleCategory.objects.get(pk=body.get("category_id"))
        except HarvestRoleCategory.DoesNotExist:
            return JsonResponse({"ok": False, "error": "category not found"}, status=404)
        list_name = "exclude_phrases" if body.get("list") == "exclude" else "include_phrases"
        phrase = normalize_phrase(str(body.get("phrase") or ""))
        if not phrase or len(phrase) < 2:
            return JsonResponse({"ok": False, "error": "phrase too short"}, status=400)
        phrases = list(getattr(cat, list_name) or [])
        if body.get("action") == "remove":
            phrases = [p for p in phrases if p != phrase]
        else:
            if phrase not in phrases:
                phrases.append(phrase)
        setattr(cat, list_name, phrases)
        cat.save(update_fields=[list_name, "updated_at"])
        return JsonResponse({"ok": True, "category": cat.slug, "list": list_name,
                             "phrases": phrases, "phrase": phrase})


class RolePhraseImpactView(SuperuserRequiredMixin, View):
    """POST JSON {phrase} -> how many recent titles this phrase would match.
    Word-boundary regex on normalized_title (90d) + recent skipped titles (14d),
    with samples — a phrase's blast radius is visible BEFORE saving it."""

    def post(self, request):
        import json as _json
        import re as _re
        from datetime import timedelta as _td
        from .models import RawJob, HarvestSkippedTitle
        from .role_filter import normalize_phrase
        try:
            body = _json.loads(request.body)
        except Exception:
            return JsonResponse({"ok": False, "error": "invalid json"}, status=400)
        phrase = normalize_phrase(str(body.get("phrase") or ""))
        if not phrase or len(phrase) < 2:
            return JsonResponse({"ok": False, "error": "phrase too short"}, status=400)
        now = timezone.now()
        rx = r"\m" + _re.escape(phrase) + r"\M"   # Postgres word boundaries
        base = RawJob.objects.filter(fetched_at__gte=now - _td(days=90))
        try:
            qs = base.filter(normalized_title__iregex=rx)
            count = qs.count()
            samples = list(qs.values_list("title", flat=True)[:5])
        except Exception:
            qs = base.filter(normalized_title__icontains=phrase)
            count = qs.count()
            samples = list(qs.values_list("title", flat=True)[:5])
        skipped = HarvestSkippedTitle.objects.filter(
            skipped_at__gte=now - _td(days=14),
            job_title__icontains=phrase,
        ).count()
        return JsonResponse({"ok": True, "phrase": phrase, "matched_90d": count,
                             "skipped_14d": skipped, "samples": samples})


class RoleReclassifyApplyView(SuperuserRequiredMixin, View):
    """POST — queue reclassification of existing RawJobs with the current
    phrase bank ('Apply to existing' button)."""

    def post(self, request):
        from .tasks import reclassify_stale_rawjobs_task
        task = reclassify_stale_rawjobs_task.delay()
        messages.success(
            request,
            f"Re-classification queued (Task {task.id[:8]}…) — existing jobs will "
            "pick up your phrase changes. Watch the Live Ops Monitor.",
        )
        return redirect(request.POST.get("next") or "harvest-role-categories")

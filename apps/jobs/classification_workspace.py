import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Max, Q
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, TemplateView, UpdateView

from core.feature_flags import feature_enabled_for
from users.models import User

from .dual_classification.effective import effective_raw_job_classification
from .models import (
    RawJobClassificationConflict,
    RawJobClassificationSnapshot,
    RawJobClassifierRun,
)
from .rollout import (
    classification_metrics_v2_enabled,
    classification_settings_v2_enabled,
    classification_workspace_v2_enabled,
)


def _dc_nested_get(payload: dict | None, *path):
    current = payload or {}
    for segment in path:
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current


def _dc_display_value(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "—"
    return str(value)


def _classification_compare_rows(snapshot):
    raw_job = snapshot.raw_job
    backend = snapshot.backend_run.normalized_output if snapshot.backend_run else {}
    secondary = snapshot.secondary_run.normalized_output if snapshot.secondary_run else {}
    merged = snapshot.merged_output or {}
    approved = snapshot.approved_output or {}
    field_provenance = getattr(raw_job, "field_provenance", {}) or {}
    classification_source = getattr(raw_job, "classification_source", "") or "raw_job"
    conflicts_by_path = {conflict.field_path: conflict for conflict in snapshot.field_conflicts.all()}

    fields = [
        ("classification.job_category", "Job Category", ("classification", "job_category"), "job_category"),
        ("classification.job_domain", "Job Domain", ("classification", "job_domain"), "job_domain"),
        (
            "classification.department_normalized",
            "Department",
            ("classification", "department_normalized"),
            "department_normalized",
        ),
        ("classification.role_category", "Role Category", ("classification", "role_category"), "role_category"),
        ("location.country", "Country", ("location", "country"), "country"),
        ("location.country_codes", "Country Codes", ("location", "country_codes"), "country_codes"),
        ("location.location_type", "Location Type", ("location", "location_type"), "location_type"),
        ("requirements.years_required", "Years Required", ("requirements", "years_required"), "years_required"),
        (
            "requirements.education_required",
            "Education",
            ("requirements", "education_required"),
            "education_required",
        ),
        ("skills.skills", "Skills", ("skills", "skills"), "skills"),
        ("skills.tech_stack", "Tech Stack", ("skills", "tech_stack"), "tech_stack"),
    ]

    rows = []
    for field_path, label, path, raw_field in fields:
        conflict = conflicts_by_path.get(field_path)
        rows.append(
            {
                "field_path": field_path,
                "label": label,
                "backend": _dc_display_value(_dc_nested_get(backend, *path)),
                "secondary": _dc_display_value(_dc_nested_get(secondary, *path)),
                "merged": _dc_display_value(_dc_nested_get(merged, *path)),
                "approved": _dc_display_value(_dc_nested_get(approved, *path)),
                "current_source": field_provenance.get(raw_field) or classification_source,
                "resolution": conflict.resolution if conflict else "",
                "severity": conflict.severity if conflict else "",
            }
        )
    return rows


def _failure_taxonomy_code(error_message: str) -> str:
    msg = (error_message or "").strip().lower()
    if not msg:
        return "unknown"
    if "timeout" in msg:
        return "timeout"
    if "json" in msg or "parse" in msg:
        return "malformed_json"
    if "schema" in msg or "validation" in msg or "required field" in msg or "enum" in msg:
        return "schema_validation"
    if "auth" in msg or "api key" in msg or "401" in msg or "403" in msg or "forbidden" in msg:
        return "auth_or_access"
    if "merge" in msg:
        return "merge_failure"
    if "unavailable" in msg or "connection" in msg or "502" in msg or "503" in msg:
        return "runtime_unavailable"
    return "other"


def _failure_taxonomy_label(code: str) -> str:
    return {
        "timeout": "Timeout",
        "malformed_json": "Malformed JSON",
        "schema_validation": "Schema validation",
        "auth_or_access": "Auth / access",
        "merge_failure": "Merge failure",
        "runtime_unavailable": "Runtime unavailable",
        "unknown": "Unknown",
        "other": "Other",
    }.get(code, code.replace("_", " ").title())


def _parse_task_result_payload(task_result):
    payload = task_result.result
    if isinstance(payload, dict):
        return payload
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except Exception:
        return {}


class ClassificationWorkspaceRequiredMixin(UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if not (user.is_superuser or getattr(user, "role", None) in (User.Role.EMPLOYEE, User.Role.ADMIN)):
            return False
        if not feature_enabled_for(user, "employee_job_pool"):
            return False
        return classification_workspace_v2_enabled(user)


class ClassificationSettingsRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if not (user.is_superuser or getattr(user, "role", None) == User.Role.ADMIN):
            return False
        return classification_settings_v2_enabled(user)


class ClassificationMetricsRequiredMixin(UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if not (user.is_superuser or getattr(user, "role", None) in (User.Role.EMPLOYEE, User.Role.ADMIN)):
            return False
        if not feature_enabled_for(user, "employee_job_pool"):
            return False
        return classification_metrics_v2_enabled(user)


class ClassificationQueueV2View(LoginRequiredMixin, ClassificationWorkspaceRequiredMixin, TemplateView):
    template_name = "classification/queue.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queue_tab = (self.request.GET.get("queue") or "needs_review").strip().lower()
        if queue_tab not in {"needs_review", "approved_not_pushed", "pushed_with_warnings", "all"}:
            queue_tab = "needs_review"
        q = (self.request.GET.get("q") or "").strip()
        provider = (self.request.GET.get("provider") or "").strip()

        base_qs = (
            RawJobClassificationSnapshot.objects.select_related(
                "raw_job",
                "approved_by",
                "pushed_to_vetting_by",
                "pushed_job",
                "backend_run",
                "secondary_run",
            )
            .annotate(conflict_count=Count("field_conflicts"))
            .order_by("-updated_at")
        )
        if q:
            base_qs = base_qs.filter(
                Q(raw_job__title__icontains=q)
                | Q(raw_job__company_name__icontains=q)
                | Q(raw_job__original_url__icontains=q)
            )
        if provider:
            base_qs = base_qs.filter(secondary_run__provider=provider)

        counts = {
            "needs_review": RawJobClassificationSnapshot.objects.filter(needs_review=True).count(),
            "approved_not_pushed": RawJobClassificationSnapshot.objects.exclude(approved_output={}).filter(
                approval_is_stale=False,
                pushed_to_vetting_at__isnull=True
            ).count(),
            "pushed_with_warnings": RawJobClassificationSnapshot.objects.filter(
                pushed_to_vetting_with_warnings=True
            ).count(),
            "all": RawJobClassificationSnapshot.objects.count(),
        }
        if queue_tab == "needs_review":
            queue_qs = base_qs.filter(needs_review=True)
        elif queue_tab == "approved_not_pushed":
            queue_qs = base_qs.exclude(approved_output={}).filter(
                approval_is_stale=False,
                pushed_to_vetting_at__isnull=True,
            )
        elif queue_tab == "pushed_with_warnings":
            queue_qs = base_qs.filter(pushed_to_vetting_with_warnings=True)
        else:
            queue_qs = base_qs

        paginator = Paginator(queue_qs, 50)
        page_obj = paginator.get_page(self.request.GET.get("page") or 1)

        total_snapshots = RawJobClassificationSnapshot.objects.count()
        reviewed_count = RawJobClassificationSnapshot.objects.exclude(
            approval_state=RawJobClassificationSnapshot.ApprovalState.UNREVIEWED
        ).count()
        pushed_count = RawJobClassificationSnapshot.objects.filter(pushed_to_vetting_at__isnull=False).count()
        secondary_completed = RawJobClassificationSnapshot.objects.filter(
            secondary_run__status=RawJobClassifierRun.Status.COMPLETED
        ).count()
        review_conflicts = RawJobClassificationConflict.objects.filter(
            resolution=RawJobClassificationConflict.Resolution.REVIEW
        ).count()

        agreement_rate = 0
        if secondary_completed:
            agreement_rate = round(
                100
                * RawJobClassificationSnapshot.objects.filter(
                    secondary_run__status=RawJobClassifierRun.Status.COMPLETED,
                    needs_review=False,
                ).count()
                / secondary_completed,
                1,
            )

        context.update(
            {
                "queue_tab": queue_tab,
                "queue_q": q,
                "queue_provider": provider,
                "queue_page_obj": page_obj,
                "queue_counts": counts,
                "queue_total": queue_qs.count(),
                "provider_choices": [
                    RawJobClassifierRun.Provider.CODEX,
                    RawJobClassifierRun.Provider.CLAUDE,
                ],
                "queue_metrics": {
                    "total_snapshots": total_snapshots,
                    "reviewed_count": reviewed_count,
                    "pushed_count": pushed_count,
                    "secondary_completed": secondary_completed,
                    "review_conflicts": review_conflicts,
                    "agreement_rate": agreement_rate,
                },
                "classification_metrics_v2": classification_metrics_v2_enabled(self.request.user),
            }
        )
        return context


class ClassificationDetailV2View(LoginRequiredMixin, ClassificationWorkspaceRequiredMixin, DetailView):
    template_name = "classification/detail.html"
    context_object_name = "snapshot"

    def get_queryset(self):
        return (
            RawJobClassificationSnapshot.objects.select_related(
                "raw_job",
                "approved_by",
                "pushed_to_vetting_by",
                "pushed_job",
                "backend_run",
                "secondary_run",
            )
            .prefetch_related("field_conflicts")
            .order_by("-updated_at")
        )

    def _pretty_json(self, value):
        return json.dumps(value or {}, indent=2, sort_keys=True, default=str)

    def _summary_cards(self, snapshot):
        raw_job = snapshot.raw_job
        effective = effective_raw_job_classification(raw_job)
        classification = effective.get("classification") or {}
        location = effective.get("location") or {}
        requirements = effective.get("requirements") or {}
        skills = effective.get("skills") or {}
        if isinstance(skills, dict):
            skill_values = skills.get("skills") or raw_job.skills or []
        elif isinstance(skills, (list, tuple)):
            skill_values = list(skills) or raw_job.skills or []
        else:
            skill_values = raw_job.skills or []
        return [
            ("Domain", classification.get("job_domain") or raw_job.job_domain or "—"),
            ("Category", classification.get("job_category") or raw_job.job_category or "—"),
            (
                "Department",
                classification.get("department_normalized")
                or raw_job.department_normalized
                or raw_job.department
                or "—",
            ),
            ("Location", location.get("country") or raw_job.country or "—"),
            ("Work Mode", location.get("location_type") or raw_job.location_type or "—"),
            (
                "Experience",
                requirements.get("years_required")
                if requirements.get("years_required") is not None
                else (raw_job.years_required if raw_job.years_required is not None else "—"),
            ),
            ("Skills", ", ".join(skill_values[:6]) or "—"),
        ]

    def get_context_data(self, **kwargs):
        from core.models import LLMConfig
        from jobs.dual_classification.config import (
            allow_push_with_warnings,
            default_secondary_provider,
            secondary_prompt_version,
            secondary_runtime_enabled,
        )
        from jobs.dual_classification.schema import build_raw_job_input

        context = super().get_context_data(**kwargs)
        snapshot = context["snapshot"]
        raw_job = snapshot.raw_job
        verifier_summary = snapshot.verifier_summary or {}
        warnings = verifier_summary.get("warnings") or []
        errors = verifier_summary.get("errors") or []
        critical_conflicts = [
            conflict
            for conflict in snapshot.field_conflicts.all()
            if conflict.severity == RawJobClassificationConflict.Severity.CRITICAL
        ]
        llm_config = LLMConfig.load()
        secondary_provider_default = default_secondary_provider()
        secondary_runtime_ready = bool(
            secondary_runtime_enabled()
            and secondary_provider_default in {RawJobClassifierRun.Provider.CODEX, RawJobClassifierRun.Provider.CLAUDE}
            and (llm_config.validation_model or llm_config.active_model or "").strip()
        )
        current_page_url = reverse("jobs-classification-detail", args=[snapshot.pk])
        effective_seed = {}
        if snapshot.approved_output and not snapshot.approval_is_stale:
            effective_seed = snapshot.approved_output
        elif snapshot.merged_output:
            effective_seed = snapshot.merged_output
        elif snapshot.backend_run:
            effective_seed = snapshot.backend_run.normalized_output or {}

        context.update(
            {
                "raw_job": raw_job,
                "summary_cards": self._summary_cards(snapshot),
                "compare_rows": _classification_compare_rows(snapshot),
                "critical_conflicts": critical_conflicts,
                "verifier_warnings": warnings,
                "verifier_errors": errors,
                "backend_run_json": self._pretty_json(
                    snapshot.backend_run.normalized_output if snapshot.backend_run else {}
                ),
                "secondary_run_json": self._pretty_json(
                    snapshot.secondary_run.normalized_output if snapshot.secondary_run else {}
                ),
                "merged_output_json": self._pretty_json(snapshot.merged_output),
                "approved_output_json": self._pretty_json(snapshot.approved_output),
                "verifier_summary_json": self._pretty_json(snapshot.verifier_summary),
                "classification_provenance_json": self._pretty_json(
                    getattr(raw_job, "classification_provenance", {}) or {}
                ),
                "field_provenance_json": self._pretty_json(getattr(raw_job, "field_provenance", {}) or {}),
                "secondary_provider_choices": [
                    RawJobClassifierRun.Provider.CODEX,
                    RawJobClassifierRun.Provider.CLAUDE,
                ],
                "secondary_provider_default": secondary_provider_default,
                "secondary_runtime_enabled": secondary_runtime_enabled(),
                "secondary_runtime_ready": secondary_runtime_ready,
                "secondary_prompt_version": secondary_prompt_version(),
                "secondary_runtime_model": (llm_config.validation_model or llm_config.active_model or "").strip(),
                "allow_push_with_warnings_enabled": allow_push_with_warnings(),
                "v2_return_url": current_page_url,
                "secondary_prompt_context_json": self._pretty_json(
                    {
                        "raw_job_input": build_raw_job_input(raw_job),
                        "instructions": {
                            "return_schema": "canonical_dual_classification_v1",
                            "notes": [
                                "Return strict JSON only.",
                                "Do not invent unsupported skills or requirements.",
                                "Use JD evidence over title assumptions when they conflict.",
                            ],
                        },
                    }
                ),
                "secondary_payload_seed": self._pretty_json(
                    {
                        "identity": {
                            "raw_job_id": raw_job.pk,
                            "title": raw_job.title or "",
                            "company_name": raw_job.company_name or "",
                        },
                        "classification": {
                            "job_category": "",
                            "job_domain": "",
                            "department_normalized": "",
                            "role_category": "",
                        },
                        "skills": {"skills": [], "tech_stack": []},
                        "requirements": {
                            "years_required": None,
                            "years_required_max": None,
                            "education_required": "",
                            "visa_sponsorship": None,
                            "work_authorization": "",
                            "clearance_required": False,
                            "clearance_level": "",
                        },
                        "location": {
                            "country": raw_job.country or "",
                            "country_codes": raw_job.country_codes or [],
                            "location_type": raw_job.location_type or "",
                            "is_remote": bool(raw_job.is_remote),
                        },
                    }
                ),
                "manual_field_seed": {
                    "job_category": (((effective_seed.get("classification") or {}).get("job_category")) or raw_job.job_category or ""),
                    "job_domain": (((effective_seed.get("classification") or {}).get("job_domain")) or raw_job.job_domain or ""),
                    "department_normalized": (((effective_seed.get("classification") or {}).get("department_normalized")) or raw_job.department_normalized or raw_job.department or ""),
                    "role_category": (((effective_seed.get("classification") or {}).get("role_category")) or raw_job.role_category or ""),
                    "country": (((effective_seed.get("location") or {}).get("country")) or raw_job.country or ""),
                    "country_codes": ", ".join(((effective_seed.get("location") or {}).get("country_codes")) or raw_job.country_codes or []),
                    "location_type": (((effective_seed.get("location") or {}).get("location_type")) or raw_job.location_type or ""),
                    "years_required": (((effective_seed.get("requirements") or {}).get("years_required")) if (effective_seed.get("requirements") or {}).get("years_required") is not None else (raw_job.years_required if raw_job.years_required is not None else "")),
                    "education_required": (((effective_seed.get("requirements") or {}).get("education_required")) or raw_job.education_required or ""),
                    "skills": ", ".join(((effective_seed.get("skills") or {}).get("skills")) or raw_job.skills or []),
                    "tech_stack": ", ".join(((effective_seed.get("skills") or {}).get("tech_stack")) or raw_job.tech_stack or []),
                },
                "classification_metrics_v2": classification_metrics_v2_enabled(self.request.user),
            }
        )
        return context


class ClassificationSettingsV2View(ClassificationSettingsRequiredMixin, UpdateView):
    template_name = "classification/settings.html"
    success_url = reverse_lazy("jobs-classification-settings")

    def get_form_class(self):
        from core.forms import ClassificationSettingsForm

        return ClassificationSettingsForm

    def get_object(self, queryset=None):
        from core.models import PlatformConfig

        return PlatformConfig.load()

    def get_context_data(self, **kwargs):
        from core.models import LLMConfig

        context = super().get_context_data(**kwargs)
        llm_config = LLMConfig.load()
        context.update(
            {
                "settings_metrics": {
                    "needs_review": RawJobClassificationSnapshot.objects.filter(needs_review=True).count(),
                    "approved_not_pushed": RawJobClassificationSnapshot.objects.exclude(approved_output={}).filter(
                        approval_is_stale=False,
                        pushed_to_vetting_at__isnull=True
                    ).count(),
                    "pushed_with_warnings": RawJobClassificationSnapshot.objects.filter(
                        pushed_to_vetting_with_warnings=True
                    ).count(),
                    "total_snapshots": RawJobClassificationSnapshot.objects.count(),
                },
                "secondary_runtime_model": (llm_config.validation_model or llm_config.active_model or "").strip(),
                "classification_metrics_v2": classification_metrics_v2_enabled(self.request.user),
            }
        )
        return context

    def form_valid(self, form):
        messages.success(self.request, "Classification settings updated successfully.")
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, "Classification settings were not saved. Fix the highlighted fields and try again.")
        return super().form_invalid(form)


class ClassificationMetricsV2View(LoginRequiredMixin, ClassificationMetricsRequiredMixin, TemplateView):
    template_name = "classification/metrics.html"

    def get_context_data(self, **kwargs):
        from core.models import LLMConfig, PlatformConfig
        from django_celery_results.models import TaskResult
        from harvest.models import HarvestOpsRun, RawJob

        context = super().get_context_data(**kwargs)
        now = timezone.now()
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)
        stale_review_cutoff = now - timedelta(hours=48)
        stale_push_cutoff = now - timedelta(hours=24)

        snapshots = RawJobClassificationSnapshot.objects.all()
        runs = RawJobClassifierRun.objects.select_related("raw_job")
        conflicts = RawJobClassificationConflict.objects.all()

        provider_labels = dict(RawJobClassifierRun.Provider.choices)
        provider_stats = list(
            runs.values("provider")
            .annotate(
                total=Count("id"),
                completed=Count("id", filter=Q(status=RawJobClassifierRun.Status.COMPLETED)),
                failed=Count("id", filter=Q(status=RawJobClassifierRun.Status.FAILED)),
                queued=Count("id", filter=Q(status=RawJobClassifierRun.Status.QUEUED)),
                running=Count("id", filter=Q(status=RawJobClassifierRun.Status.RUNNING)),
                skipped=Count("id", filter=Q(status=RawJobClassifierRun.Status.SKIPPED)),
                avg_confidence=Avg("confidence"),
                latest_started=Max("started_at"),
                latest_completed=Max("completed_at"),
            )
            .order_by("provider")
        )
        for row in provider_stats:
            total = row["total"] or 0
            failed = row["failed"] or 0
            row["provider_label"] = provider_labels.get(row["provider"], row["provider"])
            row["failure_rate"] = round((failed / total) * 100, 1) if total else 0.0

        prompt_breakdown = list(
            runs.exclude(prompt_version="")
            .values("provider", "prompt_version")
            .annotate(total=Count("id"))
            .order_by("-total", "provider", "prompt_version")[:12]
        )
        for row in prompt_breakdown:
            row["provider_label"] = provider_labels.get(row["provider"], row["provider"])

        recent_failures = list(
            runs.filter(status=RawJobClassifierRun.Status.FAILED)
            .select_related("raw_job")
            .order_by("-updated_at")[:12]
        )
        for run in recent_failures:
            run.failure_taxonomy_code = _failure_taxonomy_code(run.error_message)
            run.failure_taxonomy_label = _failure_taxonomy_label(run.failure_taxonomy_code)
        failure_taxonomy = {}
        for error_message in runs.filter(status=RawJobClassifierRun.Status.FAILED).values_list("error_message", flat=True):
            code = _failure_taxonomy_code(error_message)
            failure_taxonomy[code] = failure_taxonomy.get(code, 0) + 1
        failure_taxonomy_rows = [
            {"code": code, "label": _failure_taxonomy_label(code), "total": total}
            for code, total in sorted(failure_taxonomy.items(), key=lambda item: (-item[1], item[0]))
        ]
        recent_ops = list(
            HarvestOpsRun.objects.filter(
                operation__in=[
                    HarvestOpsRun.Operation.CLASSIFY,
                    HarvestOpsRun.Operation.LLM_CLASSIFY,
                    HarvestOpsRun.Operation.BACKFILL_JD,
                ]
            )
            .order_by("-created_at")[:8]
        )
        dual_backfill_runs = list(
            TaskResult.objects.filter(task_name="jobs.backfill_rawjob_dual_classification")
            .order_by("-date_done", "-date_created")[:8]
        )
        for task_result in dual_backfill_runs:
            task_result.result_payload = _parse_task_result_payload(task_result)

        queue_counts = {
            "needs_review": snapshots.filter(needs_review=True).count(),
            "approved_not_pushed": snapshots.exclude(approved_output={}).filter(
                approval_is_stale=False,
                pushed_to_vetting_at__isnull=True
            ).count(),
            "pushed_with_warnings": snapshots.filter(pushed_to_vetting_with_warnings=True).count(),
            "ready_for_vetting": snapshots.filter(ready_for_vetting=True, approval_is_stale=False).count(),
            "failed": snapshots.filter(status=RawJobClassificationSnapshot.Status.FAILED).count(),
            "partial": snapshots.filter(status=RawJobClassificationSnapshot.Status.PARTIAL).count(),
            "stale_approvals": snapshots.filter(approval_is_stale=True).count(),
        }
        total_snapshots = snapshots.count()
        total_secondary_completed = snapshots.filter(
            secondary_run__status=RawJobClassifierRun.Status.COMPLETED
        ).count()
        agreement_rate = 0.0
        if total_secondary_completed:
            agreement_rate = round(
                100
                * snapshots.filter(
                    secondary_run__status=RawJobClassifierRun.Status.COMPLETED,
                    needs_review=False,
                ).count()
                / total_secondary_completed,
                1,
            )

        llm_config = LLMConfig.load()
        platform_config = PlatformConfig.load()

        context.update(
            {
                "classification_metrics_v2": True,
                "metrics_overview": {
                    "total_snapshots": total_snapshots,
                    "reviewed_count": snapshots.exclude(
                        approval_state=RawJobClassificationSnapshot.ApprovalState.UNREVIEWED
                    ).count(),
                    "approved_count": snapshots.exclude(approved_output={}).filter(approval_is_stale=False).count(),
                    "pushed_count": snapshots.filter(pushed_to_vetting_at__isnull=False).count(),
                    "secondary_completed": total_secondary_completed,
                    "agreement_rate": agreement_rate,
                },
                "queue_health": {
                    **queue_counts,
                    "stale_reviews": snapshots.filter(needs_review=True, updated_at__lt=stale_review_cutoff).count(),
                    "stale_approved_not_pushed": snapshots.exclude(approved_output={}).filter(
                        approval_is_stale=False,
                        pushed_to_vetting_at__isnull=True,
                        approved_at__lt=stale_push_cutoff,
                    ).count(),
                    "oldest_review": snapshots.filter(needs_review=True).order_by("updated_at").first(),
                    "oldest_approved_not_pushed": snapshots.exclude(approved_output={}).filter(
                        approval_is_stale=False,
                        pushed_to_vetting_at__isnull=True
                    ).order_by("approved_at", "updated_at").first(),
                    "raw_backlog_without_snapshot": RawJob.objects.filter(
                        has_description=True,
                        classification_snapshot__isnull=True,
                        is_active=True,
                    ).count(),
                },
                "throughput_metrics": {
                    "shadow_runs_24h": runs.filter(
                        provider=RawJobClassifierRun.Provider.BACKEND_RULES,
                        started_at__gte=day_ago,
                    ).count(),
                    "secondary_completed_24h": runs.filter(
                        provider_role=RawJobClassifierRun.ProviderRole.SECONDARY,
                        status=RawJobClassifierRun.Status.COMPLETED,
                        completed_at__gte=day_ago,
                    ).count(),
                    "secondary_failed_24h": runs.filter(
                        provider_role=RawJobClassifierRun.ProviderRole.SECONDARY,
                        status=RawJobClassifierRun.Status.FAILED,
                        updated_at__gte=day_ago,
                    ).count(),
                    "approved_24h": snapshots.filter(approved_at__gte=day_ago).count(),
                    "approved_7d": snapshots.filter(approved_at__gte=week_ago).count(),
                    "pushed_24h": snapshots.filter(pushed_to_vetting_at__gte=day_ago).count(),
                    "pushed_7d": snapshots.filter(pushed_to_vetting_at__gte=week_ago).count(),
                },
                "conflict_metrics": {
                    "total_conflicts": conflicts.count(),
                    "snapshots_with_conflicts": snapshots.filter(field_conflicts__isnull=False).distinct().count(),
                    "critical_conflicts": conflicts.filter(
                        severity=RawJobClassificationConflict.Severity.CRITICAL
                    ).count(),
                    "review_conflicts": conflicts.filter(
                        resolution=RawJobClassificationConflict.Resolution.REVIEW
                    ).count(),
                    "severity_breakdown": list(
                        conflicts.values("severity").annotate(total=Count("id")).order_by("-total", "severity")
                    ),
                    "resolution_breakdown": list(
                        conflicts.values("resolution").annotate(total=Count("id")).order_by("-total", "resolution")
                    ),
                },
                "provider_stats": provider_stats,
                "failure_taxonomy": failure_taxonomy_rows,
                "prompt_breakdown": prompt_breakdown,
                "recent_failures": recent_failures,
                "recent_ops": recent_ops,
                "dual_backfill_runs": dual_backfill_runs,
                "runtime_health": {
                    "shadow_enabled": platform_config.dual_classification_shadow_enabled,
                    "secondary_runtime_enabled": platform_config.dual_classification_secondary_runtime_enabled,
                    "default_provider": platform_config.dual_classification_secondary_provider_default or "—",
                    "prompt_version": platform_config.dual_classification_secondary_prompt_version or "—",
                    "backfill_batch_size": platform_config.dual_classification_backfill_batch_size,
                    "require_approval_for_sync": platform_config.dual_classification_require_approval_for_sync,
                    "allow_push_with_warnings": platform_config.dual_classification_allow_push_with_warnings,
                    "llm_provider": llm_config.get_provider_display(),
                    "llm_base_url": llm_config.effective_base_url() or "OpenAI default",
                    "llm_model": (llm_config.validation_model or llm_config.active_model or "").strip() or "Not configured",
                    "llm_ready": bool((llm_config.validation_model or llm_config.active_model or "").strip()),
                    "review_queue_url": reverse("harvest-rawjob-review-queue"),
                    "classification_settings_url": reverse("jobs-classification-settings"),
                    "llm_config_url": reverse("llm-config"),
                    "run_backfill_url": reverse("harvest-run-classification-backfill"),
                },
            }
        )
        return context


__all__ = [
    "ClassificationWorkspaceRequiredMixin",
    "ClassificationSettingsRequiredMixin",
    "ClassificationMetricsRequiredMixin",
    "ClassificationQueueV2View",
    "ClassificationDetailV2View",
    "ClassificationSettingsV2View",
    "ClassificationMetricsV2View",
]

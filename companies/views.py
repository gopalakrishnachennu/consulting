from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.views.generic import ListView, DetailView, UpdateView, CreateView, View, TemplateView
from django.urls import reverse, reverse_lazy
from django.shortcuts import redirect, get_object_or_404, render
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
import csv
import json

from .models import Company, EnrichmentLog
from .forms import (
    CompanyForm,
    CompanyCSVImportForm,
    CompanyDomainImportForm,
    CompanyLinkedInImportForm,
)
from .services import (
    build_company_action_panel,
    build_company_data_completeness,
    build_company_harvest_summary,
    build_company_link_health,
    build_company_pipeline_performance,
    build_company_pipeline_snapshot,
    build_company_recent_fetch_runs,
    build_company_role_signals,
    build_company_warnings,
    company_raw_job_queryset,
    find_potential_duplicate_companies,
    merge_companies,
    normalize_company_name,
    normalize_domain,
)
from .tasks import (
    import_companies_from_csv_task,
    import_companies_from_domains_task,
    import_companies_from_linkedin_task,
    enrich_company_task,
)
from users.models import User
from submissions.models import ApplicationSubmission, SubmissionStatusHistory, EmailEvent, Offer
from config.pagination import PAGE_SIZE_OPTIONS, get_page_size, build_pagination_window


class AdminOrEmployeeRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)


def _get_company_list_queryset(request):
    """Shared queryset for list and CSV export (search, filters, sort)."""
    qs = Company.objects.annotate(
        job_count=Count("jobs", distinct=True),
        raw_job_count=Count("raw_jobs", distinct=True),
        pending_raw_job_count=Count(
            "raw_jobs",
            filter=Q(raw_jobs__sync_status="PENDING"),
            distinct=True,
        ),
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(name__icontains=q) | qs.filter(alias__icontains=q)
    status_filter = request.GET.get("status", "").strip()
    if status_filter:
        qs = qs.filter(relationship_status__iexact=status_filter)
    blacklisted = request.GET.get("blacklisted", "")
    if blacklisted == "1":
        qs = qs.filter(is_blacklisted=True)
    elif blacklisted == "0":
        qs = qs.filter(is_blacklisted=False)
    industry_filter = request.GET.get("industry", "").strip()
    if industry_filter:
        qs = qs.filter(industry__iexact=industry_filter)
    website_valid = request.GET.get("website_valid", "").strip()
    if website_valid == "0":
        qs = qs.filter(website__isnull=False).exclude(website="").filter(website_is_valid=False)
    elif website_valid == "1":
        qs = qs.filter(website_is_valid=True)
    platform_filter = request.GET.get("platform", "").strip()
    if platform_filter == "UNDETECTED":
        qs = qs.filter(platform_label__detection_method="UNDETECTED")
    elif platform_filter:
        qs = qs.filter(platform_label__platform__slug=platform_filter)

    ats_status = request.GET.get("ats_status", "").strip()
    if ats_status == "verified":
        qs = qs.filter(platform_label__portal_alive=True)
    elif ats_status == "down":
        qs = qs.filter(platform_label__portal_alive=False)
    elif ats_status == "unchecked":
        qs = qs.filter(platform_label__portal_alive__isnull=True, platform_label__platform__isnull=False)
    elif ats_status == "no_tenant":
        qs = qs.filter(platform_label__platform__isnull=False, platform_label__tenant_id="")
    elif ats_status == "no_ats":
        qs = qs.filter(platform_label__detection_method="UNDETECTED")
    elif ats_status == "unlabeled":
        qs = qs.filter(platform_label__isnull=True)

    confidence_filter = request.GET.get("confidence", "").strip()
    if confidence_filter:
        qs = qs.filter(platform_label__confidence=confidence_filter)
    method_filter = request.GET.get("method", "").strip()
    if method_filter:
        qs = qs.filter(platform_label__detection_method=method_filter)
    verified_filter = request.GET.get("verified", "").strip()
    if verified_filter == "yes":
        qs = qs.filter(platform_label__is_verified=True)
    elif verified_filter == "no":
        qs = qs.filter(platform_label__is_verified=False)

    smart_filter = request.GET.get("smart", "").strip()
    if smart_filter == "attention":
        qs = qs.filter(
            Q(needs_review=True)
            | Q(platform_label__isnull=True)
            | Q(platform_label__portal_alive=False)
            | Q(platform_label__platform__isnull=False, platform_label__tenant_id="")
            | Q(platform_label__confidence__in=["LOW", "UNKNOWN"])
            | Q(website__gt="", website_is_valid=False)
        ).distinct()
    elif smart_filter == "ready":
        qs = qs.filter(
            platform_label__platform__isnull=False,
            platform_label__tenant_id__gt="",
        ).exclude(platform_label__portal_alive=False)
    elif smart_filter == "high_value":
        qs = qs.filter(
            Q(total_submissions__gt=0)
            | Q(total_interviews__gt=0)
            | Q(total_placements__gt=0)
            | Q(job_count__gte=10)
        ).distinct()
    elif smart_filter == "duplicates":
        qs = qs.filter(needs_review=True)
    elif smart_filter == "blocked":
        qs = qs.filter(is_blacklisted=True)
    elif smart_filter == "portal_down":
        qs = qs.filter(platform_label__portal_alive=False)
    elif smart_filter == "unlabeled":
        qs = qs.filter(platform_label__isnull=True)
    elif smart_filter == "no_tenant":
        qs = qs.filter(platform_label__platform__isnull=False, platform_label__tenant_id="")
    elif smart_filter == "no_ats":
        qs = qs.filter(platform_label__detection_method="UNDETECTED")
    elif smart_filter == "scraper_inbox":
        qs = qs.filter(
            platform_label__detection_method="URL_PATTERN",
            platform_label__is_verified=False,
        )
    elif smart_filter == "raw_pending":
        qs = qs.filter(raw_jobs__sync_status="PENDING").distinct()
    qs = qs.prefetch_related("platform_label__platform")
    sort = request.GET.get("sort", "name")
    if sort == "submissions":
        qs = qs.order_by("-total_submissions", "name")
    elif sort == "interviews":
        qs = qs.order_by("-total_interviews", "name")
    elif sort == "placements":
        qs = qs.order_by("-total_placements", "name")
    elif sort == "jobs":
        qs = qs.order_by("-job_count", "name")
    elif sort == "name_desc":
        qs = qs.order_by("-name")
    else:
        qs = qs.order_by("name")
    return qs


def _company_ats_context(request):
    """Shared context for the Company Engine command center."""
    try:
        from harvest.models import (
            CompanyFetchRun,
            CompanyPlatformLabel,
            FetchBatch,
            HarvestEngineConfig,
            HarvestOpsRun,
            JobBoardPlatform,
            RawJob,
        )
    except Exception:
        return {
            "ats_enabled": False,
            "platform_choices": [],
            "platforms_chart": [],
            "confidence_choices": [],
            "method_choices": [],
        }

    platforms = JobBoardPlatform.objects.annotate(company_count=Count("labels")).order_by("name")
    platforms_chart = platforms.filter(company_count__gt=0).order_by("-company_count")
    total_companies = Company.objects.count()
    stat_labeled = CompanyPlatformLabel.objects.exclude(detection_method="UNDETECTED").count()
    stat_undetected = CompanyPlatformLabel.objects.filter(detection_method="UNDETECTED").count()
    stat_unlabeled = Company.objects.filter(platform_label__isnull=True).count()
    stat_verified = CompanyPlatformLabel.objects.filter(is_verified=True).count()
    stat_live = CompanyPlatformLabel.objects.filter(portal_alive=True).count()
    stat_down = CompanyPlatformLabel.objects.filter(portal_alive=False).count()
    stat_unchecked = CompanyPlatformLabel.objects.filter(
        portal_alive__isnull=True,
        platform__isnull=False,
    ).count()
    stat_no_tenant = CompanyPlatformLabel.objects.filter(
        platform__isnull=False,
        tenant_id="",
    ).count()
    stat_no_ats = CompanyPlatformLabel.objects.filter(detection_method="UNDETECTED").count()
    stat_scraper_inbox = CompanyPlatformLabel.objects.filter(
        detection_method="URL_PATTERN",
        is_verified=False,
    ).count()
    stat_ready_to_fetch = (
        CompanyPlatformLabel.objects.filter(
            platform__isnull=False,
            tenant_id__gt="",
        )
        .exclude(portal_alive=False)
        .count()
    )
    stat_attention = (
        Company.objects.filter(
            Q(needs_review=True)
            | Q(platform_label__isnull=True)
            | Q(platform_label__portal_alive=False)
            | Q(platform_label__platform__isnull=False, platform_label__tenant_id="")
            | Q(platform_label__platform__isnull=False, platform_label__confidence__in=["LOW", "UNKNOWN"])
            | Q(website__gt="", website_is_valid=False)
        )
        .distinct()
        .count()
    )
    stat_high_value = (
        Company.objects.annotate(job_count=Count("jobs", distinct=True))
        .filter(
            Q(total_submissions__gt=0)
            | Q(total_interviews__gt=0)
            | Q(total_placements__gt=0)
            | Q(job_count__gte=10)
        )
        .distinct()
        .count()
    )
    stat_duplicates = Company.objects.filter(needs_review=True).count()
    stat_blocked = Company.objects.filter(is_blacklisted=True).count()
    stat_raw_pending_companies = Company.objects.filter(raw_jobs__sync_status="PENDING").distinct().count()
    raw_job_total = RawJob.objects.count()
    raw_job_pending = RawJob.objects.filter(sync_status="PENDING").count()
    raw_job_failed = RawJob.objects.filter(sync_status="FAILED").count()
    running_batches = FetchBatch.objects.filter(status=FetchBatch.Status.RUNNING).count()
    running_company_fetches = CompanyFetchRun.objects.filter(status=CompanyFetchRun.Status.RUNNING).count()
    last_batch = FetchBatch.objects.order_by("-created_at").first()
    op_labels = dict(HarvestOpsRun.Operation.choices)
    recent_ops = [
        {
            "label": op_labels.get(row["operation"], row["operation"]),
            "status": row["status"],
            "created_at": row["created_at"],
        }
        for row in HarvestOpsRun.objects.order_by("-created_at").values(
            "operation",
            "status",
            "created_at",
        )[:5]
    ]
    engine_config = None
    try:
        engine_config = HarvestEngineConfig.get()
    except Exception:
        engine_config = None
    health_denominator = stat_live + stat_down
    portal_health_pct = round((stat_live / health_denominator) * 100) if health_denominator else None
    selected_view = request.GET.get("view", "").strip()
    is_ats_view = True

    engine_queues = [
        {
            "key": "",
            "label": "All companies",
            "count": total_companies,
            "tone": "slate",
            "description": "Complete company universe",
            "url": f"{reverse('company-list')}?view=engine",
        },
        {
            "key": "attention",
            "label": "Needs attention",
            "count": stat_attention,
            "tone": "amber",
            "description": "Fix duplicates, down portals, missing ATS, tenants, or low confidence",
            "url": f"{reverse('company-list')}?view=engine&smart=attention",
        },
        {
            "key": "ready",
            "label": "Ready to fetch",
            "count": stat_ready_to_fetch,
            "tone": "emerald",
            "description": "Platform and tenant are available, portal is not marked down",
            "url": f"{reverse('company-list')}?view=engine&smart=ready",
        },
        {
            "key": "portal_down",
            "label": "Portal down",
            "count": stat_down,
            "tone": "rose",
            "description": "Career portals that failed the latest health check",
            "url": f"{reverse('company-list')}?view=engine&smart=portal_down",
        },
        {
            "key": "unlabeled",
            "label": "Unlabeled",
            "count": stat_unlabeled,
            "tone": "yellow",
            "description": "No ATS label exists yet",
            "url": f"{reverse('company-list')}?view=engine&smart=unlabeled",
        },
        {
            "key": "raw_pending",
            "label": "Pending raw companies",
            "count": stat_raw_pending_companies,
            "tone": "blue",
            "description": "Companies with raw jobs waiting to sync",
            "url": f"{reverse('company-list')}?view=engine&smart=raw_pending",
        },
        {
            "key": "high_value",
            "label": "High value",
            "count": stat_high_value,
            "tone": "indigo",
            "description": "Companies with real activity or larger job pools",
            "url": f"{reverse('company-list')}?view=engine&smart=high_value",
        },
        {
            "key": "duplicates",
            "label": "Duplicate review",
            "count": stat_duplicates,
            "tone": "pink",
            "description": "Jarvis flagged possible company duplicates",
            "url": f"{reverse('company-list')}?view=engine&smart=duplicates",
        },
        {
            "key": "blocked",
            "label": "Blocked",
            "count": stat_blocked,
            "tone": "gray",
            "description": "Blacklisted companies",
            "url": f"{reverse('company-list')}?view=engine&smart=blocked",
        },
    ]

    return {
        "ats_enabled": True,
        "is_ats_view": is_ats_view,
        "platform_choices": platforms,
        "platforms_chart": platforms_chart,
        "stat_total_companies": total_companies,
        "stat_labeled": stat_labeled,
        "stat_undetected": stat_undetected,
        "stat_unlabeled": stat_unlabeled,
        "stat_verified": stat_verified,
        "stat_live": stat_live,
        "stat_down": stat_down,
        "stat_unchecked": stat_unchecked,
        "stat_no_tenant": stat_no_tenant,
        "stat_no_ats": stat_no_ats,
        "stat_scraper_inbox": stat_scraper_inbox,
        "stat_ready_to_fetch": stat_ready_to_fetch,
        "stat_attention": stat_attention,
        "stat_high_value": stat_high_value,
        "stat_duplicates": stat_duplicates,
        "stat_blocked": stat_blocked,
        "stat_raw_pending_companies": stat_raw_pending_companies,
        "raw_job_total": raw_job_total,
        "raw_job_pending": raw_job_pending,
        "raw_job_failed": raw_job_failed,
        "running_batches": running_batches,
        "running_company_fetches": running_company_fetches,
        "last_batch": last_batch,
        "recent_ops": recent_ops,
        "engine_config": engine_config,
        "engine_queues": engine_queues,
        "portal_health_pct": portal_health_pct,
        "confidence_choices": CompanyPlatformLabel.Confidence.choices,
        "method_choices": CompanyPlatformLabel.DetectionMethod.choices,
        "selected_ats_status": request.GET.get("ats_status", ""),
        "selected_confidence": request.GET.get("confidence", ""),
        "selected_method": request.GET.get("method", ""),
        "selected_verified": request.GET.get("verified", ""),
        "selected_view": selected_view,
    }


def _build_company_engine_row_state(company):
    label = getattr(company, "platform_label", None)
    warnings = []
    now = timezone.now()

    if not label:
        warnings.append({"label": "Unlabeled", "tone": "amber"})
    else:
        if label.detection_method == label.DetectionMethod.UNDETECTED:
            warnings.append({"label": "No ATS", "tone": "slate"})
        if label.platform and not label.tenant_id:
            warnings.append({"label": "Needs tenant", "tone": "amber"})
        if label.portal_alive is False:
            warnings.append({"label": "Portal down", "tone": "rose"})
        elif label.portal_last_verified and (now - label.portal_last_verified) > timedelta(days=7):
            warnings.append({"label": "Portal stale", "tone": "blue"})
        elif label.platform and label.portal_last_verified is None:
            warnings.append({"label": "Unchecked", "tone": "blue"})
        if label.confidence in (label.Confidence.LOW, label.Confidence.UNKNOWN):
            warnings.append({"label": "Low confidence", "tone": "yellow"})
        if label.detection_method == label.DetectionMethod.URL_PATTERN and not label.is_verified:
            warnings.append({"label": "Scraper inbox", "tone": "orange"})

    if company.is_blacklisted:
        warnings.append({"label": "Blocked", "tone": "rose"})
    if company.website and company.website_is_valid is False:
        warnings.append({"label": "Website issue", "tone": "yellow"})
    if getattr(company, "pending_raw_job_count", 0):
        warnings.append({"label": "Raw pending", "tone": "indigo"})

    score_parts = [
        bool(label and label.platform),
        bool(label and label.tenant_id),
        bool(label and label.portal_alive is not False),
        bool(company.website and company.website_is_valid is not False),
        bool(getattr(company, "job_count", 0) or getattr(company, "raw_job_count", 0)),
    ]
    readiness_score = round((sum(1 for ok in score_parts if ok) / len(score_parts)) * 100)

    health_age = None
    if label and label.portal_last_verified:
        delta = now - label.portal_last_verified
        if delta.days >= 1:
            health_age = f"checked {delta.days}d ago"
        else:
            hours = max(1, int(delta.total_seconds() // 3600) or 1)
            health_age = f"checked {hours}h ago"

    return {
        "warnings": warnings[:4],
        "warning_count": len(warnings),
        "extra_warning_count": max(0, len(warnings) - 4),
        "readiness_score": readiness_score,
        "health_age": health_age,
        "show_set_ats": not label or label.detection_method == "UNDETECTED" or not label.tenant_id,
        "show_create_job": getattr(company, "job_count", 0) == 0,
    }


def _build_company_engine_filter_state(request, context):
    chips = []

    def add(label, value, key):
        if value in (None, "", "All", "All industries", "All methods", "All platforms"):
            return
        chips.append({"label": label, "value": str(value), "key": key})

    add("Search", request.GET.get("q", "").strip(), "q")
    add("Relationship", context.get("selected_status"), "status")
    add("Blacklisted", "Yes" if context.get("selected_blacklisted") == "1" else "No" if context.get("selected_blacklisted") == "0" else "", "blacklisted")
    add("Industry", context.get("selected_industry"), "industry")
    add("Website", "Valid" if context.get("selected_website_valid") == "1" else "Invalid" if context.get("selected_website_valid") == "0" else "", "website_valid")
    add("Platform", context.get("selected_platform"), "platform")
    add("ATS health", context.get("selected_ats_status"), "ats_status")
    add("Confidence", context.get("selected_confidence"), "confidence")
    add("Verified", context.get("selected_verified"), "verified")
    add("Method", context.get("selected_method"), "method")
    add("Saved view", context.get("selected_smart"), "smart")

    advanced_keys = {
        "status",
        "blacklisted",
        "industry",
        "confidence",
        "verified",
        "method",
    }
    advanced_count = sum(1 for chip in chips if chip["key"] in advanced_keys)
    return {
        "active_filter_chips": chips,
        "advanced_filters_active": advanced_count,
        "has_active_filters": bool(chips),
    }


def labels_query_to_companies_url(query_dict):
    qd = query_dict.copy()
    qd["view"] = "ats"
    legacy_status = qd.get("status", "")
    qd.pop("status", None)
    if legacy_status and not qd.get("ats_status"):
        qd["ats_status"] = legacy_status
    return f"{reverse_lazy('company-list')}?{qd.urlencode()}"


class CompanyListView(AdminOrEmployeeRequiredMixin, ListView):
    model = Company
    template_name = "companies/company_list.html"
    context_object_name = "companies"

    def get_paginate_by(self, queryset):
        return get_page_size(self.request, default=100)

    def get_queryset(self):
        return _get_company_list_queryset(self.request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qd = self.request.GET.copy()
        qd.pop("page", None)
        context["pagination_query"] = qd.urlencode()
        context["page_size"] = get_page_size(self.request, default=100)
        context["page_size_options"] = PAGE_SIZE_OPTIONS
        if context.get("is_paginated"):
            context["pagination_pages"] = build_pagination_window(context["page_obj"])
        context["selected_sort"] = self.request.GET.get("sort", "name")
        context["selected_status"] = self.request.GET.get("status", "")
        context["selected_blacklisted"] = self.request.GET.get("blacklisted", "")
        context["selected_industry"] = self.request.GET.get("industry", "")
        context["selected_website_valid"] = self.request.GET.get("website_valid", "")
        context["selected_platform"] = self.request.GET.get("platform", "")
        context["selected_smart"] = self.request.GET.get("smart", "")
        context.update(_company_ats_context(self.request))
        context["relationship_statuses"] = (
            Company.objects.exclude(relationship_status="")
            .values_list("relationship_status", flat=True)
            .distinct()
            .order_by("relationship_status")
        )
        industries_list = list(
            Company.objects.exclude(industry="")
            .values_list("industry", flat=True)
            .distinct()
            .order_by("industry")
        )
        if context["selected_industry"] and context["selected_industry"] not in industries_list:
            industries_list.append(context["selected_industry"])
            industries_list.sort(key=str.lower)
        context["industries"] = industries_list
        # Results summary: total count and range for current page
        if context.get("page_obj"):
            context["results_total"] = context["page_obj"].paginator.count
            context["results_start"] = context["page_obj"].start_index()
            context["results_end"] = context["page_obj"].end_index()
        else:
            context["results_total"] = context["results_start"] = context["results_end"] = 0
        for company in context.get("companies", []):
            company.engine_row_state = _build_company_engine_row_state(company)
        context.update(_build_company_engine_filter_state(self.request, context))
        context["saved_company_views"] = [
            queue for queue in context.get("engine_queues", [])
            if queue["key"] in {"attention", "ready", "portal_down", "unlabeled", "no_tenant", "no_ats", "scraper_inbox", "high_value"}
        ]
        context["primary_filter_count"] = sum(
            1
            for key in ("q", "platform", "ats_status", "website_valid", "smart")
            if self.request.GET.get(key, "").strip()
        )
        return context


class CompanyIntelligenceView(AdminOrEmployeeRequiredMixin, View):
    """One-click company intelligence panel for ATS, raw jobs, and pipeline signals."""

    template_name = "companies/_company_intelligence_panel.html"

    def get(self, request, pk, *args, **kwargs):
        company = get_object_or_404(
            Company.objects.annotate(
                job_count=Count("jobs", distinct=True),
                raw_job_count=Count("raw_jobs", distinct=True),
                pending_raw_job_count=Count(
                    "raw_jobs",
                    filter=Q(raw_jobs__sync_status="PENDING"),
                    distinct=True,
                ),
            ).select_related("platform_label__platform"),
            pk=pk,
        )
        try:
            label = company.platform_label
        except ObjectDoesNotExist:
            label = None

        raw_jobs = []
        latest_run = None
        raw_job_stats = {
            "total": getattr(company, "raw_job_count", 0),
            "pending": getattr(company, "pending_raw_job_count", 0),
            "synced": 0,
            "failed": 0,
        }
        if label:
            try:
                from harvest.models import CompanyFetchRun, RawJob

                latest_run = (
                    CompanyFetchRun.objects.filter(label=label)
                    .order_by("-started_at", "-id")
                    .first()
                )
                agg = RawJob.objects.filter(company=company).aggregate(
                    synced=Count("id", filter=Q(sync_status="SYNCED")),
                    failed=Count("id", filter=Q(sync_status="FAILED")),
                )
                raw_job_stats.update({k: v or 0 for k, v in agg.items()})
                raw_jobs = (
                    RawJob.objects.filter(company=company)
                    .only("id", "title", "platform_slug", "sync_status", "fetched_at", "original_url")
                    .order_by("-fetched_at")[:5]
                )
            except Exception:
                raw_jobs = []

        attention_items = []
        if company.needs_review:
            attention_items.append(("Duplicate review", "Possible duplicate needs merge or dismiss."))
        if company.is_blacklisted:
            attention_items.append(("Blacklisted", company.blacklist_reason or "Submissions should stay blocked."))
        if company.website and not company.website_is_valid:
            attention_items.append(("Website invalid", "Website check has not passed for this company."))
        if not label:
            attention_items.append(("ATS unlabeled", "Run detection or add a platform label before fetching jobs."))
        elif label.detection_method == "UNDETECTED":
            attention_items.append(("No ATS detected", "Career site did not match a supported platform."))
        elif label.platform and not label.tenant_id:
            attention_items.append(("Tenant missing", "Platform is known but tenant ID is required before fetch."))
        elif label.portal_alive is False:
            attention_items.append(("Portal down", "Last portal health check failed. Re-verify before fetching."))
        elif label.confidence in ("LOW", "UNKNOWN"):
            attention_items.append(("Low confidence", "Detection should be manually reviewed."))

        context = {
            "company": company,
            "label": label,
            "latest_run": latest_run,
            "raw_jobs": raw_jobs,
            "raw_job_stats": raw_job_stats,
            "attention_items": attention_items,
            "raw_jobs_url": f"{reverse('harvest-rawjobs')}?company_id={company.pk}",
            "jobs_url": f"{reverse('job-list')}?company={company.pk}",
        }
        return render(request, self.template_name, context)


class CompanyDetailView(AdminOrEmployeeRequiredMixin, DetailView):
    model = Company
    template_name = "companies/company_detail.html"
    context_object_name = "company"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company: Company = self.object

        job_ids = company.jobs.values_list("id", flat=True)
        AS = ApplicationSubmission
        subs = AS.objects.filter(job_id__in=job_ids)

        total = subs.count()
        interviews = subs.filter(status__in=[AS.Status.INTERVIEW, AS.Status.OFFER]).count()
        offers = subs.filter(status=AS.Status.OFFER).count()
        rejected = subs.filter(status=AS.Status.REJECTED).count()

        def pct(part, whole):
            if not whole:
                return None
            return round((part / whole) * 100)

        funnel = {
            "total_submissions": total,
            "interviews": interviews,
            "offers": offers,
            "rejections": rejected,
            "interview_rate_pct": pct(interviews, total),
            "offer_rate_pct": pct(offers, total),
            "rejection_rate_pct": pct(rejected, total),
        }
        harvest_summary = build_company_harvest_summary(company)
        pipeline_snapshot = build_company_pipeline_snapshot(company)
        data_completeness = build_company_data_completeness(
            company,
            label=harvest_summary.get("label"),
            harvest_summary=harvest_summary,
        )
        company_warnings = build_company_warnings(
            company,
            label=harvest_summary.get("label"),
            harvest_summary=harvest_summary,
            pipeline_snapshot=pipeline_snapshot,
            completeness=data_completeness,
        )
        link_health = build_company_link_health(company, label=harvest_summary.get("label"))
        company_actions = build_company_action_panel(company, label=harvest_summary.get("label"))
        company_role_signals = build_company_role_signals(company)
        company_pipeline_performance = build_company_pipeline_performance(
            company,
            funnel=funnel,
            harvest_summary=harvest_summary,
            pipeline_snapshot=pipeline_snapshot,
        )

        # Top employees and consultants for this company
        employee_rows = (
            subs.values("submitted_by")
            .exclude(submitted_by__isnull=True)
            .annotate(
                submissions=Count("id"),
                interviews=Count("id", filter=Q(status__in=[AS.Status.INTERVIEW, AS.Status.OFFER])),
            )
            .order_by("-interviews", "-submissions")[:5]
        )
        employees_map = {u.id: u for u in User.objects.filter(id__in=[r["submitted_by"] for r in employee_rows])}
        employees = []
        for r in employee_rows:
            u = employees_map.get(r["submitted_by"])
            if not u:
                continue
            subs_count = r["submissions"]
            intr = r["interviews"]
            employees.append(
                {
                    "user": u,
                    "submissions": subs_count,
                    "interviews": intr,
                    "quality_pct": pct(intr, subs_count),
                }
            )

        consultant_rows = (
            subs.values("consultant")
            .annotate(
                submissions=Count("id"),
                interviews=Count("id", filter=Q(status__in=[AS.Status.INTERVIEW, AS.Status.OFFER])),
                offers=Count("id", filter=Q(status=AS.Status.OFFER)),
            )
            .order_by("-offers", "-interviews")[:5]
        )

        # Interaction timeline: submissions, status changes, interviews, email events, offers
        sub_ids = list(subs.values_list("id", flat=True))
        timeline = []

        # Submissions created
        for sub in subs.select_related("consultant__user", "job"):
            timeline.append(
                (
                    sub.created_at,
                    "submission_created",
                    f"Submission created for {sub.consultant.user.get_full_name() or sub.consultant.user.username} on job {sub.job.title}",
                )
            )

        # Status history
        for h in SubmissionStatusHistory.objects.filter(submission_id__in=sub_ids).select_related("submission"):
            timeline.append(
                (
                    h.created_at,
                    "status_change",
                    f"Status changed to {h.to_status} for {h.submission.consultant.user.get_full_name() or h.submission.consultant.user.username}",
                )
            )

        # Interviews
        try:
            from interviews_app.models import Interview

            for iv in Interview.objects.filter(submission_id__in=sub_ids).select_related("submission", "submission__consultant__user"):
                label = f"Interview ({iv.get_round_display()}) scheduled for {iv.submission.consultant.user.get_full_name() or iv.submission.consultant.user.username}"
                timeline.append((iv.scheduled_at, "interview", label))
        except Exception:
            pass

        # Email events
        for ev in EmailEvent.objects.filter(matched_submission_id__in=sub_ids):
            who = ev.from_address
            label = f"Email from {who}: {ev.subject}"
            timeline.append((ev.received_at, "email", label))

        # Offers / placements
        for offer in Offer.objects.filter(submission_id__in=sub_ids).select_related("submission", "submission__consultant__user"):
            ts = offer.accepted_at or offer.created_at
            label = f"Offer for {offer.submission.consultant.user.get_full_name() or offer.submission.consultant.user.username}"
            timeline.append((ts, "offer", label))

        timeline.sort(key=lambda x: x[0] or company.created_at, reverse=True)

        context["company_funnel"] = funnel
        context["company_top_employees"] = employees
        context["company_top_consultants"] = consultant_rows  # resolved lazily in template if needed
        context["company_timeline"] = timeline[:100]
        context["company_jobs"] = company.jobs.all().select_related("posted_by").order_by("-created_at")
        context["harvest_health"] = harvest_summary
        context["company_platform_label"] = harvest_summary.get("label")
        context["recent_fetch_runs"] = build_company_recent_fetch_runs(company)
        context["pipeline_snapshot"] = pipeline_snapshot
        context["company_data_completeness"] = data_completeness
        context["company_warnings"] = company_warnings
        context["link_health_summary"] = link_health
        context["company_actions"] = company_actions
        context["company_role_signals"] = company_role_signals
        context["company_pipeline_performance"] = company_pipeline_performance

        # Raw jobs from harvest engine — check FK first, fall back to name match
        try:
            raw_qs = company_raw_job_queryset(company).order_by("-fetched_at")
            # No .only() here — pipeline_stage_label is a deep property that
            # chains through jd_gate.py and needs many fields. 20 rows is fine.
            context["harvest_raw_jobs"] = list(raw_qs[:20])
            context["harvest_raw_jobs_total"] = raw_qs.count()
            context["harvest_raw_jobs_url"] = (
                f"/jobs/pipeline/?tab=raw&search_by=company&q={company.name}"
            )
        except Exception:
            context["harvest_raw_jobs"] = []
            context["harvest_raw_jobs_total"] = 0
            context["harvest_raw_jobs_url"] = ""
        if company.logo_url:
            context["company_logo_src"] = company.logo_url
        elif company.domain:
            context["company_logo_src"] = f"https://logo.clearbit.com/{company.domain}"
        else:
            context["company_logo_src"] = ""
        desc = (company.description or "").strip()
        context["description_needs_toggle"] = len(desc) > 560
        context["description_preview"] = (desc[:560] + "…") if len(desc) > 560 else desc
        return context


class CompanyCreateView(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Company
    form_class = CompanyForm
    template_name = "companies/company_form.html"

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def form_valid(self, form):
        action = self.request.POST.get("duplicate_action")
        if action:
            # Second step: user already reviewed duplicates, proceed accordingly.
            if action == "use_existing":
                existing_id = self.request.POST.get("existing_company_id")
                if existing_id:
                    try:
                        existing = Company.objects.get(pk=existing_id)
                        messages.info(
                            self.request,
                            f'Using existing company "{existing.name}" (possible duplicate).',
                        )
                        return reverse_lazy("company-detail", kwargs={"pk": existing.pk})
                    except Company.DoesNotExist:
                        pass  # fall through to normal create
            # Either create_anyway or fallback: just create the company
            response = super().form_valid(form)
            messages.success(self.request, "Company created successfully!")
            try:
                config = __import__("core.models", fromlist=["PlatformConfig"]).PlatformConfig.load()
                if getattr(config, "auto_enrich_on_create", True):
                    enrich_company_task.delay(self.object.pk)
            except Exception:
                enrich_company_task.delay(self.object.pk)
            return response

        # First step: run duplicate detection before actually creating
        name = form.cleaned_data.get("name") or ""
        website = form.cleaned_data.get("website") or ""
        duplicates = find_potential_duplicate_companies(name=name, website=website, threshold=0.85, limit=5)
        if duplicates:
            # Render confirmation screen with form + duplicate list
            context = self.get_context_data(form=form, potential_duplicates=duplicates)
            return self.render_to_response(context)

        response = super().form_valid(form)
        messages.success(self.request, "Company created successfully!")
        try:
            config = __import__("core.models", fromlist=["PlatformConfig"]).PlatformConfig.load()
            if getattr(config, "auto_enrich_on_create", True):
                enrich_company_task.delay(self.object.pk)
        except Exception:
            enrich_company_task.delay(self.object.pk)
        return response

    def get_success_url(self):
        next_url = (self.request.GET.get("next") or "").strip()
        if next_url:
            from urllib.parse import quote

            sep = "&" if "?" in next_url else "?"
            return f"{next_url}{sep}company_id={self.object.pk}&company_name={quote(self.object.name)}"
        return reverse_lazy("company-detail", kwargs={"pk": self.object.pk})


class CompanyUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Company
    form_class = CompanyForm
    template_name = "companies/company_form.html"

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role == User.Role.ADMIN

    def get_success_url(self):
        return reverse_lazy("company-detail", kwargs={"pk": self.object.pk})


class CompanyExportCSVView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Export companies as CSV, respecting current filters and sort."""

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get(self, request, *args, **kwargs):
        qs = _get_company_list_queryset(request)
        ids = request.GET.get("ids", "").strip()
        if ids:
            try:
                pk_list = [int(x) for x in ids.split(",") if x.strip()]
                if pk_list:
                    qs = qs.filter(pk__in=pk_list)
            except ValueError:
                pass
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="companies.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Name", "Alias", "Industry", "Website", "Career Site", "Relationship Status",
            "Submissions", "Interviews", "Placements", "Jobs", "Blacklisted",
        ])
        for c in qs:
            writer.writerow([
                c.name,
                c.alias or "",
                c.industry or "",
                c.website or "",
                c.career_site_url or "",
                c.relationship_status or "",
                c.total_submissions,
                c.total_interviews,
                c.total_placements,
                c.job_count,
                "Yes" if c.is_blacklisted else "No",
            ])
        return response


class CompanyDuplicateReviewView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Simple duplicate review list built from find_potential_duplicate_companies.
    Shows potential duplicate pairs and allows merging.
    """

    template_name = "companies/company_duplicate_list.html"

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role == User.Role.ADMIN

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pairs = []
        seen = set()
        # Focus on most recent companies to reduce noise
        for company in Company.objects.order_by("-created_at")[:100]:
            dups = find_potential_duplicate_companies(company.name, company.website, threshold=0.8, limit=5)
            for other, score in dups:
                if other.pk == company.pk:
                    continue
                key = tuple(sorted((company.pk, other.pk)))
                if key in seen:
                    continue
                seen.add(key)
                # Prefer lower id as target to reduce conflicts
                target, source = (company, other) if company.pk < other.pk else (other, company)
                pairs.append(
                    {
                        "target": target,
                        "source": source,
                        "score": round(score, 2),
                    }
                )
        context["duplicate_pairs"] = pairs
        context["jarvis_flagged"] = (
            Company.objects.filter(needs_review=True)
            .select_related("possible_duplicate_of")
            .order_by("-created_at")[:50]
        )
        return context


class CompanyMergeView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role == User.Role.ADMIN

    def post(self, request, *args, **kwargs):
        source_id = request.POST.get("source_id")
        target_id = request.POST.get("target_id")
        if not source_id or not target_id:
            messages.error(request, "Missing source or target company.")
            return redirect("company-duplicate-review")
        try:
            source = Company.objects.get(pk=source_id)
            target = Company.objects.get(pk=target_id)
        except Company.DoesNotExist:
            messages.error(request, "One of the selected companies no longer exists.")
            return redirect("company-duplicate-review")

        merge_companies(source, target)
        messages.success(
            request,
            f"Merged company \"{source.name}\" into \"{target.name}\". All jobs and rules now point to the canonical record.",
        )
        return redirect("company-duplicate-review")


class CompanyDismissReviewView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Clear the needs_review flag without merging — admin decided it's not a duplicate."""

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role == User.Role.ADMIN

    def post(self, request, pk, *args, **kwargs):
        company = get_object_or_404(Company, pk=pk)
        company.needs_review = False
        company.possible_duplicate_of = None
        company.save(update_fields=["needs_review", "possible_duplicate_of"])
        messages.success(request, f"Review dismissed for \"{company.name}\".")
        return redirect("company-duplicate-review")


class CompanyCSVImportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "companies/company_import_csv.html"

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", CompanyCSVImportForm())
        return context

    def post(self, request, *args, **kwargs):
        form = CompanyCSVImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_to_response({"form": form})
        f = form.cleaned_data["csv_file"]
        result = import_companies_from_csv_task(f.read())
        messages.success(
            request,
            f"Company import complete: {result.get('created', 0)} created, {result.get('updated', 0)} updated.",
        )
        return redirect("company-list")


class CompanyDomainImportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "companies/company_import_domains.html"

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", CompanyDomainImportForm())
        return context

    def post(self, request, *args, **kwargs):
        form = CompanyDomainImportForm(request.POST)
        if not form.is_valid():
            return self.render_to_response({"form": form})
        text = form.cleaned_data["domains"]
        result = import_companies_from_domains_task(text)
        messages.success(
            request,
            f"Domain import complete: {result.get('created', 0)} created, {result.get('existing', 0)} already existed.",
        )
        return redirect("company-list")


class CompanyLinkedInImportView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "companies/company_import_linkedin.html"

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", CompanyLinkedInImportForm())
        return context

    def post(self, request, *args, **kwargs):
        form = CompanyLinkedInImportForm(request.POST)
        if not form.is_valid():
            return self.render_to_response({"form": form})
        text = form.cleaned_data["linkedin_urls"]
        result = import_companies_from_linkedin_task(text)
        messages.success(
            request,
            f"LinkedIn import complete: {result.get('created', 0)} created, {result.get('existing', 0)} existing, "
            f"{result.get('invalid', 0)} invalid URLs.",
        )
        return redirect("company-list")


class CompanySearchView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Lightweight JSON endpoint for job-form typeahead.
    """

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE, User.Role.CONSULTANT)

    def get(self, request, *args, **kwargs):
        from difflib import SequenceMatcher

        q = (request.GET.get("q") or "").strip()
        if not q:
            return JsonResponse({"results": []})

        # 1. Substring match first (fast)
        exact_qs = list(
            (
                Company.objects.filter(name__icontains=q)
                | Company.objects.filter(alias__icontains=q)
            ).order_by("name")[:10]
        )
        seen_ids = {c.pk for c in exact_qs}

        # 2. Fuzzy fallback — catch typos like "brighthorizon" → "BrightHorizons"
        fuzzy_matches = []
        if len(exact_qs) < 5:
            q_lower = q.lower().strip()
            for c in Company.objects.only("pk", "name", "alias", "domain", "website", "industry"):
                if c.pk in seen_ids:
                    continue
                ratio = SequenceMatcher(None, q_lower, c.name.lower()).ratio()
                if ratio < 0.75 and c.alias:
                    ratio = max(ratio, SequenceMatcher(None, q_lower, c.alias.lower()).ratio())
                if ratio >= 0.75:
                    fuzzy_matches.append((c, ratio))
            fuzzy_matches.sort(key=lambda x: x[1], reverse=True)

        combined = exact_qs + [c for c, _ in fuzzy_matches[:5]]
        data = [
            {
                "id": c.pk,
                "name": c.name,
                "alias": c.alias,
                "domain": c.domain,
                "website": c.website,
                "industry": c.industry,
            }
            for c in combined[:10]
        ]
        return JsonResponse({"results": data})


class CompanyCreateAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    POST /companies/api/create/: create or return existing company (normalize → dedupe).
    JSON body: { "name": "...", "website": "...", optional: alias, industry, ... }
    Returns: 201 + { "id", "name", "domain", "website", "created": true|false }
    """

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def post(self, request, *args, **kwargs):
        try:
            body = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        name = (body.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "name is required"}, status=400)
        website = (body.get("website") or "").strip()
        name = normalize_company_name(name)
        domain = normalize_domain(website) if website else ""
        existing = None
        if domain:
            existing = Company.objects.filter(domain=domain).first()
        if not existing:
            existing = Company.objects.filter(name__iexact=name).first()
        if existing:
            return JsonResponse(
                {
                    "id": existing.pk,
                    "name": existing.name,
                    "domain": existing.domain or "",
                    "website": existing.website or "",
                    "created": False,
                },
                status=200,
            )
        company = Company.objects.create(
            name=name,
            domain=domain,
            website=website or "",
            alias=(body.get("alias") or "").strip(),
            industry=(body.get("industry") or "").strip(),
        )
        try:
            config = __import__("core.models", fromlist=["PlatformConfig"]).PlatformConfig.load()
            if getattr(config, "auto_enrich_on_create", True):
                enrich_company_task.delay(company.pk)
        except Exception:
            enrich_company_task.delay(company.pk)
        return JsonResponse(
            {
                "id": company.pk,
                "name": company.name,
                "domain": company.domain or "",
                "website": company.website or "",
                "created": True,
            },
            status=201,
        )


class CompanyReEnrichView(LoginRequiredMixin, UserPassesTestMixin, View):
    """POST-only: queue enrich_company_task for one company."""

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def post(self, request, *args, **kwargs):
        from core.http import redirect_with_task_progress

        company = get_object_or_404(Company, pk=kwargs["pk"])
        r = enrich_company_task.delay(company.pk)
        messages.success(request, f'Re-enrichment queued for "{company.name}".')
        return redirect_with_task_progress(
            "company-detail",
            r.id,
            f"Enrich: {company.name}"[:120],
            kwargs={"pk": company.pk},
        )


class CompanyQuickFillView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Synchronous enrichment — no Celery required.
    Runs DDG Instant Answer + OG scrape + keyword classifiers in-request
    and immediately saves + shows what was filled. Zero LLM cost, zero API cost.
    """

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def post(self, request, *args, **kwargs):
        from .enrichment_helpers import apply_free_enrichment
        from .tasks import (
            _compute_data_quality_score,
            _extract_domain_for_enrichment,
            _fetch_apollo,
            _fetch_hunter,
            _apply_link_validation,
        )

        company = get_object_or_404(Company, pk=kwargs["pk"])
        filled, src_tags = apply_free_enrichment(company)

        # Optional APIs (Hunter, Apollo). Knowledge Graph runs inside apply_free_enrichment.
        try:
            config = __import__("core.models", fromlist=["PlatformConfig"]).PlatformConfig.load()
        except Exception:
            config = None
        domain = _extract_domain_for_enrichment(company)
        if config:
            hunter_key = (getattr(config, "hunter_api_key", None) or "").strip()
            if hunter_key and domain:
                h_data = _fetch_hunter(hunter_key, domain)
                if h_data.get("description") and not company.description:
                    company.description = h_data["description"]
                    filled.append("description (Hunter.io)")
                if h_data.get("industry") and not company.industry:
                    company.industry = h_data["industry"]
                    filled.append("industry (Hunter.io)")
                if h_data.get("headcount_range") and not company.headcount_range:
                    company.headcount_range = str(h_data["headcount_range"])
                    filled.append("headcount (Hunter.io)")
                if h_data.get("hq_location") and not company.hq_location:
                    company.hq_location = h_data["hq_location"]
                    filled.append("HQ (Hunter.io)")
            apollo_key = (getattr(config, "apollo_api_key", None) or "").strip()
            if apollo_key and domain:
                a_data = _fetch_apollo(apollo_key, domain)
                if a_data.get("description") and not company.description:
                    company.description = a_data["description"]
                    filled.append("description (Apollo)")
                if a_data.get("industry") and not company.industry:
                    company.industry = a_data["industry"]
                    filled.append("industry (Apollo)")

        _apply_link_validation(company)

        company.enrichment_status = Company.EnrichmentStatus.ENRICHED if filled else Company.EnrichmentStatus.FAILED
        company.enriched_at = timezone.now()
        company.enrichment_source = "quick-fill+" + "+".join(src_tags) if src_tags else "quick-fill"
        company.data_quality_score = _compute_data_quality_score(company)
        company.save()

        if filled:
            messages.success(
                request,
                f"Filled {len(filled)} field(s): {', '.join(filled[:12])}{'…' if len(filled) > 12 else ''}",
            )
        else:
            messages.warning(
                request,
                "Could not find enough public data. Add a website or company name and try again.",
            )

        return redirect("company-detail", pk=company.pk)


class CompanyEnrichmentStatusView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Data pipeline / enrichment status: counts (pending, enriched, failed, stale)
    and "Re-enrich stale" action.
    """

    template_name = "companies/enrichment_status.html"

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        stale_cutoff = now - timezone.timedelta(days=90)

        total = Company.objects.count()
        pending = Company.objects.filter(enrichment_status=Company.EnrichmentStatus.PENDING).count()
        enriched = Company.objects.filter(enrichment_status=Company.EnrichmentStatus.ENRICHED).count()
        failed = Company.objects.filter(enrichment_status=Company.EnrichmentStatus.FAILED).count()
        # Stale = enriched but enriched_at older than 90 days, or explicitly marked stale
        stale = Company.objects.filter(
            Q(enrichment_status=Company.EnrichmentStatus.ENRICHED, enriched_at__lt=stale_cutoff)
            | Q(enrichment_status=Company.EnrichmentStatus.STALE)
        ).count()
        context["total"] = total
        context["pending"] = pending
        context["enriched"] = enriched
        context["failed"] = failed
        context["stale"] = stale
        context["stale_cutoff_days"] = 90
        return context

    def post(self, request, *args, **kwargs):
        """Re-enrich stale: queue enrich_company_task for each stale company."""
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
        messages.success(
            request,
            f"Re-enrichment queued for {len(stale_ids)} stale companies.",
        )
        return redirect("company-enrichment-status")


class EnrichmentLogListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    """Per-company enrichment run history (Phase 3.5 / 5)."""

    model = EnrichmentLog
    template_name = "companies/enrichment_log_list.html"
    context_object_name = "logs"
    paginate_by = 50

    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)

    def get_queryset(self):
        return super().get_queryset().select_related("company").order_by("-timestamp")

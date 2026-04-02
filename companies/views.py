from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.views.generic import ListView, DetailView, UpdateView, CreateView, View, TemplateView
from django.urls import reverse_lazy
from django.shortcuts import redirect, get_object_or_404
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
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
from .services import find_potential_duplicate_companies, merge_companies, normalize_company_name, normalize_domain
from .tasks import (
    import_companies_from_csv_task,
    import_companies_from_domains_task,
    import_companies_from_linkedin_task,
    enrich_company_task,
)
from users.models import User
from submissions.models import ApplicationSubmission, SubmissionStatusHistory, EmailEvent, Offer


class AdminOrEmployeeRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        u: User = self.request.user
        return u.is_superuser or u.role in (User.Role.ADMIN, User.Role.EMPLOYEE)


def _get_company_list_queryset(request):
    """Shared queryset for list and CSV export (search, filters, sort)."""
    qs = Company.objects.all()
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
    sort = request.GET.get("sort", "name")
    if sort == "submissions":
        qs = qs.order_by("-total_submissions", "name")
    elif sort == "interviews":
        qs = qs.order_by("-total_interviews", "name")
    elif sort == "placements":
        qs = qs.order_by("-total_placements", "name")
    elif sort == "name_desc":
        qs = qs.order_by("-name")
    else:
        qs = qs.order_by("name")
    return qs


class CompanyListView(AdminOrEmployeeRequiredMixin, ListView):
    model = Company
    template_name = "companies/company_list.html"
    context_object_name = "companies"
    paginate_by = 25

    def get_queryset(self):
        return _get_company_list_queryset(self.request)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qd = self.request.GET.copy()
        qd.pop("page", None)
        context["pagination_query"] = qd.urlencode()
        context["selected_sort"] = self.request.GET.get("sort", "name")
        context["selected_status"] = self.request.GET.get("status", "")
        context["selected_blacklisted"] = self.request.GET.get("blacklisted", "")
        context["selected_industry"] = self.request.GET.get("industry", "")
        context["selected_website_valid"] = self.request.GET.get("website_valid", "")
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
        return context


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
                            f"Using existing company “{existing.name}” (possible duplicate).",
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
            "Submissions", "Interviews", "Placements", "Blacklisted",
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
            return reverse_lazy("company-duplicate-review")
        try:
            source = Company.objects.get(pk=source_id)
            target = Company.objects.get(pk=target_id)
        except Company.DoesNotExist:
            messages.error(request, "One of the selected companies no longer exists.")
            return reverse_lazy("company-duplicate-review")

        merge_companies(source, target)
        messages.success(
            request,
            f"Merged company “{source.name}” into “{target.name}”. All jobs and rules now point to the canonical record.",
        )
        return reverse_lazy("company-duplicate-review")


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
        q = (request.GET.get("q") or "").strip()
        if not q:
            return JsonResponse({"results": []})
        qs = (
            Company.objects.filter(name__icontains=q)
            | Company.objects.filter(alias__icontains=q)
        ).order_by("name")[:10]
        data = []
        for c in qs:
            data.append(
                {
                    "id": c.pk,
                    "name": c.name,
                    "alias": c.alias,
                    "domain": c.domain,
                    "website": c.website,
                    "industry": c.industry,
                }
            )
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
        company = get_object_or_404(Company, pk=kwargs["pk"])
        enrich_company_task.delay(company.pk)
        messages.success(request, f"Re-enrichment queued for “{company.name}”.")
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

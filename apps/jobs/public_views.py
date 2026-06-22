"""
Public (unauthenticated) job board: OPEN roles for marketing and demo.
Internal job URLs still require login; this uses a separate URL namespace.
"""

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import DetailView, ListView, View

from core.models import PublicSiteContent
from users.models import User

from .models import Job


def _public_job_queryset():
    return Job.objects.filter(status=Job.Status.OPEN, is_archived=False).select_related("posted_by", "company_obj")


def _apply_public_filters(qs, params):
    from django.db.models import Q

    q = (params.get("q") or "").strip()
    country = (params.get("country") or "").strip()
    department = (params.get("department") or "").strip()
    job_type = (params.get("job_type") or "").strip()

    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(company__icontains=q)
            | Q(location__icontains=q)
            | Q(description__icontains=q)
        )
    if country:
        qs = qs.filter(Q(country__iexact=country) | Q(location__icontains=country))
    if department:
        qs = qs.filter(department=department)
    if job_type:
        qs = qs.filter(job_type=job_type)
    return qs


def _public_filters_context():
    qs = _public_job_queryset()
    return {
        "country_options": [
            value for value in qs.exclude(country="").values_list("country", flat=True).distinct().order_by("country")[:50]
        ],
        "department_options": Job.Department.choices,
        "job_type_options": Job.JobType.choices,
    }


class PublicCareersHomeView(ListView):
    model = Job
    template_name = "jobs/public_careers_home.html"
    context_object_name = "jobs"
    paginate_by = 12

    def get_queryset(self):
        return _apply_public_filters(_public_job_queryset().order_by("-created_at"), self.request.GET)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_chrome"] = True
        context["public_site_content"] = PublicSiteContent.load()
        context["featured_jobs"] = list(self.object_list[:3])
        context["open_jobs_count"] = _public_job_queryset().count()
        context.update(_public_filters_context())
        return context


class PublicJobListView(ListView):
    """SEO-friendly list of open roles (no login)."""

    model = Job
    template_name = "jobs/public_job_list.html"
    context_object_name = "jobs"
    paginate_by = 20

    def get_queryset(self):
        return _apply_public_filters(_public_job_queryset().order_by("-created_at"), self.request.GET)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_chrome"] = True
        context["public_site_content"] = PublicSiteContent.load()
        context["open_jobs_count"] = _public_job_queryset().count()
        context.update(_public_filters_context())
        return context


class PublicJobDetailView(DetailView):
    model = Job
    template_name = "jobs/public_job_detail.html"
    context_object_name = "job"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if obj.status != Job.Status.OPEN or obj.is_archived:
            raise Http404()
        return obj

    def get_queryset(self):
        return _public_job_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_chrome"] = True
        context["public_site_content"] = PublicSiteContent.load()
        return context


class PublicJobApplyView(View):
    """
    Entry point for "Apply" from the public board.
    - Anonymous → login with next=/jobs/<pk>/ (internal job page after auth).
    - Consultant → internal job detail (full description + quick submit).
    - Other roles → internal job detail with info message.
    """

    def get(self, request, pk):
        job = get_object_or_404(Job, pk=pk)
        if job.status != Job.Status.OPEN or job.is_archived:
            raise Http404()
        next_url = reverse("job-detail", kwargs={"pk": job.pk})
        if not request.user.is_authenticated:
            return redirect_to_login(next_url, login_url=reverse("sign-in"))
        u = request.user
        if u.role == User.Role.CONSULTANT:
            messages.info(
                request,
                "Review the full job and use Quick submit or your consultant workflow to apply.",
            )
            return redirect(next_url)
        messages.info(request, "Sign in as a consultant to submit applications through the portal.")
        return redirect(next_url)

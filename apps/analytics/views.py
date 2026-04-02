from django.views.generic import TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone
from datetime import timedelta
from django.http import HttpResponse
import csv
from jobs.models import Job
from submissions.models import ApplicationSubmission
from users.models import User, MarketingRole
import json
from django.core.serializers.json import DjangoJSONEncoder

class EmployeeRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.role == User.Role.EMPLOYEE or self.request.user.is_superuser

class AnalyticsDashboardView(LoginRequiredMixin, EmployeeRequiredMixin, TemplateView):
    template_name = 'analytics/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        range_param = self.request.GET.get('range', 'all')
        since = None
        if range_param == '7':
            since = timezone.now() - timedelta(days=7)
            context['date_range_label'] = 'Last 7 days'
            context['date_range'] = '7'
        elif range_param == '30':
            since = timezone.now() - timedelta(days=30)
            context['date_range_label'] = 'Last 30 days'
            context['date_range'] = '30'
        else:
            context['date_range_label'] = 'All time'
            context['date_range'] = 'all'

        job_qs = Job.objects.all()
        app_qs = ApplicationSubmission.objects.all()
        if since:
            job_qs = job_qs.filter(created_at__gte=since)
            app_qs = app_qs.filter(created_at__gte=since)

        # 1. Key Metrics (optionally in period)
        context['total_jobs'] = job_qs.count()
        context['active_jobs'] = job_qs.filter(status='OPEN').count()
        context['total_consultants'] = User.objects.filter(role=User.Role.CONSULTANT).count()
        context['total_applications'] = app_qs.count()

        # 2. Applications by Status (Pie Chart)
        app_status_qs = app_qs.values('status').annotate(count=Count('status'))
        context['app_status_labels'] = json.dumps([item['status'] for item in app_status_qs], cls=DjangoJSONEncoder)
        context['app_status_data'] = json.dumps([item['count'] for item in app_status_qs], cls=DjangoJSONEncoder)

        # 3. Funnel metrics (bar chart)
        status_counts = {row['status']: row['count'] for row in app_status_qs}
        funnel_labels = ['In Progress', 'Applied', 'Interview', 'Offer', 'Rejected']
        funnel_order = [
            ApplicationSubmission.Status.IN_PROGRESS,
            ApplicationSubmission.Status.APPLIED,
            ApplicationSubmission.Status.INTERVIEW,
            ApplicationSubmission.Status.OFFER,
            ApplicationSubmission.Status.REJECTED,
        ]
        funnel_data = [status_counts.get(code, 0) for code in funnel_order]
        context['funnel_labels'] = json.dumps(funnel_labels, cls=DjangoJSONEncoder)
        context['funnel_data'] = json.dumps(funnel_data, cls=DjangoJSONEncoder)

        # 3b. Simple conversion snapshot (percentages)
        total_apps = context['total_applications']
        if total_apps:
            context['interview_rate'] = int(
                (status_counts.get(ApplicationSubmission.Status.INTERVIEW, 0) / total_apps) * 100
            )
            context['offer_rate'] = int(
                (status_counts.get(ApplicationSubmission.Status.OFFER, 0) / total_apps) * 100
            )
            context['rejection_rate'] = int(
                (status_counts.get(ApplicationSubmission.Status.REJECTED, 0) / total_apps) * 100
            )
        else:
            context['interview_rate'] = 0
            context['offer_rate'] = 0
            context['rejection_rate'] = 0

        # 4. Jobs Posted Over Time (Line Chart)
        jobs_over_time_qs = job_qs
        jobs_over_time = (
            jobs_over_time_qs.annotate(month=TruncMonth('created_at'))
            .values('month')
            .annotate(count=Count('id'))
            .order_by('month')
        )
        context['jobs_time_labels'] = json.dumps(
            [item['month'].strftime('%b %Y') for item in jobs_over_time],
            cls=DjangoJSONEncoder,
        )
        context['jobs_time_data'] = json.dumps(
            [item['count'] for item in jobs_over_time],
            cls=DjangoJSONEncoder,
        )

        # 4b. Applications Over Time (Line Chart)
        apps_over_time = (
            app_qs.annotate(month=TruncMonth('created_at'))
            .values('month')
            .annotate(count=Count('id'))
            .order_by('month')
        )
        context['apps_time_labels'] = json.dumps(
            [item['month'].strftime('%b %Y') for item in apps_over_time],
            cls=DjangoJSONEncoder,
        )
        context['apps_time_data'] = json.dumps(
            [item['count'] for item in apps_over_time],
            cls=DjangoJSONEncoder,
        )

        # 5. Top performers (optionally in period)
        if since:
            context['top_employees'] = (
                User.objects.filter(role=User.Role.EMPLOYEE)
                .annotate(job_count=Count('posted_jobs', filter=Q(posted_jobs__created_at__gte=since)))
                .order_by('-job_count')[:5]
            )
            context['top_consultants'] = (
                User.objects.filter(role=User.Role.CONSULTANT, consultant_profile__isnull=False)
                .annotate(submission_count=Count('consultant_profile__submissions', filter=Q(consultant_profile__submissions__created_at__gte=since)))
                .order_by('-submission_count')[:5]
            )
        else:
            context['top_employees'] = (
                User.objects.filter(role=User.Role.EMPLOYEE)
                .annotate(job_count=Count('posted_jobs'))
                .order_by('-job_count')[:5]
            )
            context['top_consultants'] = (
                User.objects.filter(role=User.Role.CONSULTANT, consultant_profile__isnull=False)
                .annotate(submission_count=Count('consultant_profile__submissions'))
                .order_by('-submission_count')[:5]
            )

        # 6. Applications by Marketing Role (simple funnel)
        if since:
            role_qs = MarketingRole.objects.all().annotate(
                total=Count('jobs__submissions', filter=Q(jobs__submissions__created_at__gte=since)),
                applied=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.APPLIED,
                    ),
                ),
                interview=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.INTERVIEW,
                    ),
                ),
                offer=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.OFFER,
                    ),
                ),
                rejected=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.REJECTED,
                    ),
                ),
            )
        else:
            role_qs = MarketingRole.objects.all().annotate(
                total=Count('jobs__submissions'),
                applied=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.APPLIED),
                ),
                interview=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.INTERVIEW),
                ),
                offer=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.OFFER),
                ),
                rejected=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.REJECTED),
                ),
            )

        role_funnel = []
        for role in role_qs:
            if role.total:
                role_funnel.append(
                    {
                        'name': role.name,
                        'total': role.total,
                        'applied': role.applied,
                        'interview': role.interview,
                        'offer': role.offer,
                        'rejected': role.rejected,
                    }
                )
        context['role_funnel'] = role_funnel

        return context


class AnalyticsExportCSVView(LoginRequiredMixin, EmployeeRequiredMixin, View):
    """Export high-level analytics as CSV (same filters as dashboard)."""

    def get(self, request, *args, **kwargs):
        range_param = request.GET.get('range', 'all')
        since = None
        if range_param == '7':
            since = timezone.now() - timedelta(days=7)
            date_range_label = 'Last 7 days'
        elif range_param == '30':
            since = timezone.now() - timedelta(days=30)
            date_range_label = 'Last 30 days'
        else:
            date_range_label = 'All time'

        job_qs = Job.objects.all()
        app_qs = ApplicationSubmission.objects.all()
        if since:
            job_qs = job_qs.filter(created_at__gte=since)
            app_qs = app_qs.filter(created_at__gte=since)

        total_jobs = job_qs.count()
        active_jobs = job_qs.filter(status=Job.Status.OPEN).count()
        total_consultants = User.objects.filter(role=User.Role.CONSULTANT).count()
        total_applications = app_qs.count()

        app_status_qs = app_qs.values('status').annotate(count=Count('status'))
        status_counts = {row['status']: row['count'] for row in app_status_qs}

        # Role funnel (same as dashboard)
        if since:
            role_qs = MarketingRole.objects.all().annotate(
                total=Count('jobs__submissions', filter=Q(jobs__submissions__created_at__gte=since)),
                applied=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.APPLIED,
                    ),
                ),
                interview=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.INTERVIEW,
                    ),
                ),
                offer=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.OFFER,
                    ),
                ),
                rejected=Count(
                    'jobs__submissions',
                    filter=Q(
                        jobs__submissions__created_at__gte=since,
                        jobs__submissions__status=ApplicationSubmission.Status.REJECTED,
                    ),
                ),
            )
        else:
            role_qs = MarketingRole.objects.all().annotate(
                total=Count('jobs__submissions'),
                applied=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.APPLIED),
                ),
                interview=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.INTERVIEW),
                ),
                offer=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.OFFER),
                ),
                rejected=Count(
                    'jobs__submissions',
                    filter=Q(jobs__submissions__status=ApplicationSubmission.Status.REJECTED),
                ),
            )

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="analytics.csv"'
        writer = csv.writer(response)

        writer.writerow(['Section', 'Key', 'Value'])
        writer.writerow(['Summary', 'Date range', date_range_label])
        writer.writerow(['Summary', 'Total jobs', total_jobs])
        writer.writerow(['Summary', 'Active jobs', active_jobs])
        writer.writerow(['Summary', 'Total consultants', total_consultants])
        writer.writerow(['Summary', 'Total applications', total_applications])

        writer.writerow([])
        writer.writerow(['Status breakdown', 'Status code', 'Count'])
        for status_code, count in status_counts.items():
            writer.writerow(['Status breakdown', status_code, count])

        writer.writerow([])
        writer.writerow(['Role funnel', 'Role', 'Total', 'Applied', 'Interview', 'Offer', 'Rejected'])
        for role in role_qs:
            if not role.total:
                continue
            writer.writerow(
                [
                    'Role funnel',
                    role.name,
                    role.total,
                    role.applied,
                    role.interview,
                    role.offer,
                    role.rejected,
                ]
            )

        return response

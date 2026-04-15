from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from django.http import HttpResponse

from core.views import home, AdminDashboardView, EmployeeDashboardView
from config.impersonate_views import start_impersonate, stop_impersonate
from users.views import PublicConsultantProfileView

urlpatterns = [
    # Leapcell health probes (observed typos in platform paths)
    path("kaithhealthcheck", lambda request: HttpResponse("ok", content_type="text/plain")),
    path("kaithheathcheck", lambda request: HttpResponse("ok", content_type="text/plain")),
    path('admin/', admin.site.urls),
    path('c/<slug:slug>/', PublicConsultantProfileView.as_view(), name='consultant-public-profile'),
    path('consultants/consultants/', RedirectView.as_view(url='/consultants/', permanent=True)),
    path('consultants/consultants', RedirectView.as_view(url='/consultants/', permanent=True)),
    path("__reload__/", include("django_browser_reload.urls")),
    path("accounts/", include("django.contrib.auth.urls")),
    path("jobs/", include("jobs.urls")),
    path("careers/", include("jobs.public_urls")),
    path("resumes/", include("resumes.urls")),
    path("submissions/", include("submissions.urls")),
    path("interviews/", include("interviews_app.urls")),
    path("messages/", include("messaging.urls")),
    path("consultants/", include("users.urls")),
    path("employees/", include("users.urls_employees")),
    path("analytics/", include("analytics.urls")),
    path("companies/", include("companies.urls")),
    path("harvest/", include("harvest.urls")),
    path("core/", include("core.urls")),
    path("admin-dashboard/", AdminDashboardView.as_view(), name="admin-dashboard"),
    path("employee-dashboard/", EmployeeDashboardView.as_view(), name="employee-dashboard"),
    path("impersonate/<int:user_id>/", start_impersonate, name="start-impersonate"),
    path("impersonate/stop/", stop_impersonate, name="stop-impersonate"),
    path("", home, name="home"),
]

handler404 = 'django.views.defaults.page_not_found'
handler403 = 'django.views.defaults.permission_denied'
handler500 = 'django.views.defaults.server_error'

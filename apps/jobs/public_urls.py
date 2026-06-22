from django.urls import path

from .public_views import PublicCareersHomeView, PublicJobApplyView, PublicJobDetailView, PublicJobListView

urlpatterns = [
    path("", PublicCareersHomeView.as_view(), name="public-careers-home"),
    path("jobs/", PublicJobListView.as_view(), name="public-job-list"),
    path("jobs/<int:pk>/", PublicJobDetailView.as_view(), name="public-job-detail"),
    path("jobs/<int:pk>/apply/", PublicJobApplyView.as_view(), name="public-job-apply"),
]

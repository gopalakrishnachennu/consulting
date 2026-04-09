from django.urls import path
from .views import (
    JobListView, JobDetailView, JobCreateView, JobUpdateView, JobDeleteView,
    JobBulkUploadView, JobParseJDView, JobExportCSVView, JobDuplicateCheckView,
    JobUrlCheckView,
    # Phase 5
    JobArchiveView, JobRestoreView, ArchivedJobsView,
    # Job Pool / Validation Pipeline
    JobPoolView, JobPoolRevalidateView, JobApproveView, JobRejectView, JobBulkApproveView,
)

urlpatterns = [
    path('', JobListView.as_view(), name='job-list'),
    path('export/', JobExportCSVView.as_view(), name='job-export-csv'),
    path('duplicate-check/', JobDuplicateCheckView.as_view(), name='job-duplicate-check'),
    path('url-check/', JobUrlCheckView.as_view(), name='job-url-check'),
    path('archived/', ArchivedJobsView.as_view(), name='job-archived'),
    path('new/', JobCreateView.as_view(), name='job-create'),
    path('bulk-upload/', JobBulkUploadView.as_view(), name='job-bulk-upload'),
    # Job Pool
    path('pool/', JobPoolView.as_view(), name='job-pool'),
    path('pool/bulk-approve/', JobBulkApproveView.as_view(), name='job-bulk-approve'),
    path('<int:pk>/', JobDetailView.as_view(), name='job-detail'),
    path('<int:pk>/parse-jd/', JobParseJDView.as_view(), name='job-parse-jd'),
    path('<int:pk>/edit/', JobUpdateView.as_view(), name='job-update'),
    path('<int:pk>/delete/', JobDeleteView.as_view(), name='job-delete'),
    path('<int:pk>/archive/', JobArchiveView.as_view(), name='job-archive'),
    path('<int:pk>/restore/', JobRestoreView.as_view(), name='job-restore'),
    path('<int:pk>/approve/', JobApproveView.as_view(), name='job-approve'),
    path('<int:pk>/reject/', JobRejectView.as_view(), name='job-reject'),
    path('<int:pk>/revalidate/', JobPoolRevalidateView.as_view(), name='job-revalidate'),
]

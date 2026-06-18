from django.urls import path
from .classification_workspace import (
    ClassificationDetailV2View,
    ClassificationMetricsV2View,
    ClassificationQueueV2View,
    ClassificationSettingsV2View,
)
from .views import (
    JobListView, JobDetailView, JobCreateView, JobUpdateView, JobDeleteView,
    JobBulkUploadView, JobParseJDView, JobExportCSVView, JobDuplicateCheckView,
    JobUrlCheckView,
    # Phase 5
    JobArchiveView, JobRestoreView, ArchivedJobsView,
    # Job Pool / Validation Pipeline
    JobPoolView, JobPoolRevalidateView, JobApproveView, JobRejectView, JobBulkApproveView, JobPoolRefreshLinksView,
    # Unified Command Center
    JobsPipelineView,
    # Phase 4: lineage + health
    JobTimelineView, PipelineHealthView,
    # Classification engine
    ClassifyJobsTriggerView,
    # Bulk actions
    JobBulkActionView,
)
from harvest.views import (
    RunBackfillDescriptionsView,
    RunCleanupNowView,
    RunDetectNowView,
    RunRetryFailedFetchesView,
    RunSyncNowView,
    RunSyncSelectedRawJobsView,
    RunValidateRawUrlsView,
    TriggerBatchFetchView,
)

urlpatterns = [
    path('', JobListView.as_view(), name='job-list'),
    path('pipeline/', JobsPipelineView.as_view(), name='jobs-pipeline'),
    path('classification/queue/', ClassificationQueueV2View.as_view(), name='jobs-classification-queue'),
    path('classification/settings/', ClassificationSettingsV2View.as_view(), name='jobs-classification-settings'),
    path('classification/metrics/', ClassificationMetricsV2View.as_view(), name='jobs-classification-metrics'),
    path('classification/<int:pk>/', ClassificationDetailV2View.as_view(), name='jobs-classification-detail'),
    # Jobs Pipeline owns the user-facing raw job workflow. Legacy /harvest/run/*
    # endpoints remain available for existing links and Harvest Engine settings.
    path('pipeline/run/fetch-batch/', TriggerBatchFetchView.as_view(), name='jobs-pipeline-run-fetch-batch'),
    path('pipeline/run/sync/', RunSyncNowView.as_view(), name='jobs-pipeline-run-sync'),
    path('pipeline/run/sync-selected/', RunSyncSelectedRawJobsView.as_view(), name='jobs-pipeline-run-sync-selected'),
    path('pipeline/run/detect/', RunDetectNowView.as_view(), name='jobs-pipeline-run-detect'),
    path('pipeline/run/backfill-descriptions/', RunBackfillDescriptionsView.as_view(), name='jobs-pipeline-run-backfill-descriptions'),
    path('pipeline/run/validate-urls/', RunValidateRawUrlsView.as_view(), name='jobs-pipeline-run-validate-urls'),
    path('pipeline/run/retry-failed-fetches/', RunRetryFailedFetchesView.as_view(), name='jobs-pipeline-run-retry-failed-fetches'),
    path('pipeline/run/cleanup/', RunCleanupNowView.as_view(), name='jobs-pipeline-run-cleanup'),
    path('export/', JobExportCSVView.as_view(), name='job-export-csv'),
    path('duplicate-check/', JobDuplicateCheckView.as_view(), name='job-duplicate-check'),
    path('url-check/', JobUrlCheckView.as_view(), name='job-url-check'),
    path('archived/', ArchivedJobsView.as_view(), name='job-archived'),
    path('bulk-action/', JobBulkActionView.as_view(), name='job-bulk-action'),
    path('new/', JobCreateView.as_view(), name='job-create'),
    path('bulk-upload/', JobBulkUploadView.as_view(), name='job-bulk-upload'),
    # Job Pool
    path('pool/', JobPoolView.as_view(), name='job-pool'),
    path('pool/bulk-approve/', JobBulkApproveView.as_view(), name='job-bulk-approve'),
    path('pool/refresh-links/', JobPoolRefreshLinksView.as_view(), name='job-pool-refresh-links'),
    path('<int:pk>/', JobDetailView.as_view(), name='job-detail'),
    path('<int:pk>/parse-jd/', JobParseJDView.as_view(), name='job-parse-jd'),
    path('<int:pk>/edit/', JobUpdateView.as_view(), name='job-update'),
    path('<int:pk>/delete/', JobDeleteView.as_view(), name='job-delete'),
    path('<int:pk>/archive/', JobArchiveView.as_view(), name='job-archive'),
    path('<int:pk>/restore/', JobRestoreView.as_view(), name='job-restore'),
    path('<int:pk>/approve/', JobApproveView.as_view(), name='job-approve'),
    path('<int:pk>/reject/', JobRejectView.as_view(), name='job-reject'),
    path('<int:pk>/revalidate/', JobPoolRevalidateView.as_view(), name='job-revalidate'),
    # Phase 4: per-job timeline + ops health dashboard
    path('<int:pk>/timeline/', JobTimelineView.as_view(), name='job-timeline'),
    path('pipeline/health/', PipelineHealthView.as_view(), name='pipeline-health'),
    # Classification engine
    path('classify/', ClassifyJobsTriggerView.as_view(), name='job-classify'),
]

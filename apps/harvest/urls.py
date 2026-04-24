from django.urls import path

from .push_api import ExportLabelsView, PushJobsView, PushStatusView
from .views import (
    CompanyFetchStatusView,
    CompanyLabelListView,
    EngineConfigView,
    FetchBatchListView,
    FetchCooldownStatusView,
    JarvisReScrapeView,
    JarvisStatusView,
    JarvisView,
    LabelManualSetView,
    LabelUpdateTenantView,
    LabelVerifyView,
    PlatformCreateView,
    PlatformDeleteView,
    PlatformListView,
    PlatformToggleView,
    PlatformUpdateView,
    RawJobDetailView,
    RawJobListView,
    RawJobStatsView,
    RunBackfillDescriptionsView,
    RunBackfillNowView,
    RunBulkSyncView,
    RunCleanupNowView,
    RunDetectNowView,
    RunEnrichExistingView,
    RunHarvestNowView,
    RunMonitorView,
    RunSyncNowView,
    RunVerifyPortalsView,
    ScheduleConfigView,
    SetupScheduleView,
    StopBatchView,
    TriggerBatchFetchView,
    TriggerCompanyFetchView,
)

urlpatterns = [
    # Platform Registry
    path("platforms/", PlatformListView.as_view(), name="harvest-platforms"),
    path("platforms/new/", PlatformCreateView.as_view(), name="harvest-platform-create"),
    path("platforms/<int:pk>/edit/", PlatformUpdateView.as_view(), name="harvest-platform-edit"),
    path("platforms/<int:pk>/delete/", PlatformDeleteView.as_view(), name="harvest-platform-delete"),
    path("platforms/<int:pk>/toggle/", PlatformToggleView.as_view(), name="harvest-platform-toggle"),
    # Schedule
    path("schedule/", ScheduleConfigView.as_view(), name="harvest-schedule"),
    # Monitor
    path("monitor/", RunMonitorView.as_view(), name="harvest-monitor"),
    # Labels
    path("labels/", CompanyLabelListView.as_view(), name="harvest-labels"),
    path("labels/<int:pk>/verify/", LabelVerifyView.as_view(), name="harvest-label-verify"),
    path("labels/<int:pk>/set-platform/", LabelManualSetView.as_view(), name="harvest-label-set-platform"),
    path("labels/<int:pk>/update-tenant/", LabelUpdateTenantView.as_view(), name="harvest-label-update-tenant"),
    # Raw Jobs — note: static paths before <int:pk>
    path("raw-jobs/", RawJobListView.as_view(), name="harvest-rawjobs"),
    path("raw-jobs/batches/", FetchBatchListView.as_view(), name="harvest-rawjobs-batches"),
    path("raw-jobs/company-status/", CompanyFetchStatusView.as_view(), name="harvest-rawjobs-company-status"),
    path("raw-jobs/stats/", RawJobStatsView.as_view(), name="harvest-rawjobs-stats"),
    path("raw-jobs/<int:pk>/", RawJobDetailView.as_view(), name="harvest-rawjob-detail"),
    # Trigger actions
    path("run/detect/", RunDetectNowView.as_view(), name="harvest-run-detect"),
    path("run/harvest/", RunHarvestNowView.as_view(), name="harvest-run-harvest"),
    path("run/sync/", RunSyncNowView.as_view(), name="harvest-run-sync"),
    path("run/bulk-sync/", RunBulkSyncView.as_view(), name="harvest-run-bulk-sync"),
    path("run/cleanup/", RunCleanupNowView.as_view(), name="harvest-run-cleanup"),
    path("run/backfill/", RunBackfillNowView.as_view(), name="harvest-run-backfill"),
    path("run/verify-portals/", RunVerifyPortalsView.as_view(), name="harvest-run-verify-portals"),
    path("run/fetch-company/", TriggerCompanyFetchView.as_view(), name="harvest-run-fetch-company"),
    path("run/fetch-batch/", TriggerBatchFetchView.as_view(), name="harvest-run-fetch-batch"),
    path("run/stop-batch/", StopBatchView.as_view(), name="harvest-run-stop-batch"),
    path("run/backfill-descriptions/", RunBackfillDescriptionsView.as_view(), name="harvest-run-backfill-descriptions"),
    path("run/enrich-existing/", RunEnrichExistingView.as_view(), name="harvest-run-enrich-existing"),
    path("run/setup-schedule/", SetupScheduleView.as_view(), name="harvest-run-setup-schedule"),
    # API helpers
    path("api/cooldown/", FetchCooldownStatusView.as_view(), name="harvest-api-cooldown"),
    # Local Harvesting Agent — push API (token-authenticated, csrf_exempt)
    path("api/push/labels/", ExportLabelsView.as_view(), name="harvest-push-labels"),
    path("api/push/jobs/", PushJobsView.as_view(), name="harvest-push-jobs"),
    path("api/push/status/", PushStatusView.as_view(), name="harvest-push-status"),
    # Engine Config — runtime tuning knobs (concurrency, rate limit, stagger)
    path("engine/", EngineConfigView.as_view(), name="harvest-engine-config"),
    # Job Jarvis — paste-any-URL ingestion
    path("jarvis/", JarvisView.as_view(), name="harvest-jarvis"),
    path("jarvis/status/", JarvisStatusView.as_view(), name="harvest-jarvis-status"),
    path("jarvis/rescrape/", JarvisReScrapeView.as_view(), name="harvest-jarvis-rescrape"),
]

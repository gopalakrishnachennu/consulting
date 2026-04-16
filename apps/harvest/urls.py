from django.urls import path

from .views import (
    CompanyLabelListView,
    LabelManualSetView,
    LabelUpdateTenantView,
    LabelVerifyView,
    PlatformCreateView,
    PlatformDeleteView,
    PlatformListView,
    PlatformToggleView,
    PlatformUpdateView,
    RunBackfillNowView,
    RunCleanupNowView,
    RunDetectNowView,
    RunHarvestNowView,
    RunMonitorView,
    RunSyncNowView,
    RunVerifyPortalsView,
    ScheduleConfigView,
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
    # Trigger actions
    path("run/detect/", RunDetectNowView.as_view(), name="harvest-run-detect"),
    path("run/harvest/", RunHarvestNowView.as_view(), name="harvest-run-harvest"),
    path("run/sync/", RunSyncNowView.as_view(), name="harvest-run-sync"),
    path("run/cleanup/", RunCleanupNowView.as_view(), name="harvest-run-cleanup"),
    path("run/backfill/", RunBackfillNowView.as_view(), name="harvest-run-backfill"),
    path("run/verify-portals/", RunVerifyPortalsView.as_view(), name="harvest-run-verify-portals"),
]

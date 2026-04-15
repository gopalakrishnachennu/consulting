from django.contrib import admin
from django.db.models import Count

from .models import CompanyPlatformLabel, HarvestRun, HarvestedJob, JobBoardPlatform


@admin.register(JobBoardPlatform)
class JobBoardPlatformAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "api_type", "company_count", "is_enabled", "last_harvested_at"]
    list_filter = ["api_type", "is_enabled"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_company_count=Count("labels"))

    @admin.display(description="Companies", ordering="_company_count")
    def company_count(self, obj):
        return obj._company_count


@admin.register(CompanyPlatformLabel)
class CompanyPlatformLabelAdmin(admin.ModelAdmin):
    list_display = ["company", "platform", "confidence", "detection_method", "is_verified", "last_checked_at"]
    list_filter = ["platform", "confidence", "detection_method", "is_verified"]
    search_fields = ["company__name"]
    raw_id_fields = ["company"]
    readonly_fields = ["detected_at", "last_checked_at", "verified_at"]


@admin.register(HarvestRun)
class HarvestRunAdmin(admin.ModelAdmin):
    list_display = [
        "pk",
        "run_type",
        "platform",
        "status",
        "triggered_by",
        "started_at",
        "jobs_new",
        "detection_detected",
        "jobs_fetched",
        "jobs_failed",
    ]
    list_filter = ["run_type", "status", "triggered_by"]
    readonly_fields = ["started_at", "finished_at"]


@admin.register(HarvestedJob)
class HarvestedJobAdmin(admin.ModelAdmin):
    list_display = ["title", "company_name", "platform", "location", "sync_status", "fetched_at", "is_active"]
    list_filter = ["platform", "sync_status", "is_active", "job_type"]
    search_fields = ["title", "company_name"]
    raw_id_fields = ["company", "synced_to_job", "harvest_run"]
    readonly_fields = ["url_hash", "fetched_at", "expires_at"]

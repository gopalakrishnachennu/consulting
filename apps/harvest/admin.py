from django.contrib import admin
from django.db.models import Count

from .models import (
    CompanyPlatformLabel,
    HarvestFilterSnapshot,
    HarvestEngineConfig,
    HarvestOpsRun,
    HarvestRoleCategory,
    HarvestSkippedTitle,
    JobBoardPlatform,
    LocationCache,
    PlatformEngineConfig,
    RawJob,
    RawJobPayloadSnapshot,
    VetGateConfig,
)


@admin.register(JobBoardPlatform)
class JobBoardPlatformAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "api_type", "company_count", "title_in_list", "unknown_jd_budget_per_run", "is_enabled", "last_harvested_at"]
    list_filter = ["api_type", "title_in_list", "is_enabled"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_company_count=Count("labels"))

    @admin.display(description="Companies", ordering="_company_count")
    def company_count(self, obj):
        return obj._company_count


@admin.register(CompanyPlatformLabel)
class CompanyPlatformLabelAdmin(admin.ModelAdmin):
    list_display = ["company", "platform", "confidence", "detection_method", "skip_in_selective_harvest", "consecutive_zero_tech_fetches", "is_verified", "last_checked_at"]
    list_filter = ["platform", "confidence", "detection_method", "skip_in_selective_harvest", "is_verified"]
    search_fields = ["company__name"]
    raw_id_fields = ["company"]
    readonly_fields = ["detected_at", "last_checked_at", "verified_at"]


@admin.register(RawJob)
class RawJobAdmin(admin.ModelAdmin):
    list_display = [
        "title", "company_name", "platform_slug",
        "country_code", "scope_status", "is_priority",
        "filter_decision", "role_category", "is_cold",
        "job_domain", "job_category",
        "sync_status", "employment_type", "fetched_at", "is_active",
    ]
    list_filter = [
        "platform_slug", "sync_status", "employment_type",
        "filter_decision", "role_category", "is_cold", "jd_fetch_skipped",
        "job_domain", "country_code", "scope_status", "is_priority", "is_active",
    ]
    search_fields = ["title", "company_name", "url_hash", "job_domain", "location_raw", "filter_reason"]
    raw_id_fields = ["company"]
    readonly_fields = ["url_hash", "fetched_at", "updated_at", "job_domain", "domain_version", "last_scope_evaluated_at"]


@admin.register(HarvestRoleCategory)
class HarvestRoleCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_active", "priority", "updated_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug", "include_phrases", "exclude_phrases"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(HarvestFilterSnapshot)
class HarvestFilterSnapshotAdmin(admin.ModelAdmin):
    list_display = ["snapshot_id", "batch", "phrase_hash", "taken_at"]
    search_fields = ["snapshot_id", "phrase_hash", "notes"]
    raw_id_fields = ["batch"]
    readonly_fields = ["snapshot_id", "taken_at", "category_data", "phrase_hash"]


@admin.register(HarvestSkippedTitle)
class HarvestSkippedTitleAdmin(admin.ModelAdmin):
    list_display = [
        "job_title", "company_name", "platform_slug", "filter_decision",
        "is_sampled", "skipped_at",
    ]
    list_filter = ["filter_decision", "platform_slug", "is_sampled", "skipped_at"]
    search_fields = ["job_title", "company_name", "department", "filter_reason", "matched_negative"]
    raw_id_fields = ["raw_job"]
    readonly_fields = ["skipped_at"]


@admin.register(RawJobPayloadSnapshot)
class RawJobPayloadSnapshotAdmin(admin.ModelAdmin):
    list_display = [
        "raw_job", "payload_kind", "platform_slug", "size_label",
        "is_failure", "http_status", "captured_at",
    ]
    list_filter = ["payload_kind", "platform_slug", "is_failure", "captured_at"]
    search_fields = ["raw_job__title", "raw_job__company_name", "source_url", "content_hash"]
    raw_id_fields = ["raw_job", "fetch_batch"]
    readonly_fields = [
        "raw_job", "fetch_batch", "platform_slug", "source_url", "payload_kind",
        "schema_version", "payload", "raw_html_gzip", "content_hash",
        "payload_size_bytes", "raw_html_size_bytes", "redaction_version",
        "source_metadata", "is_failure", "http_status", "captured_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(LocationCache)
class LocationCacheAdmin(admin.ModelAdmin):
    list_display = [
        "normalized_text", "country_code", "region_code", "city",
        "confidence", "source", "provider", "status", "looked_up_at",
    ]
    list_filter = ["country_code", "source", "provider", "status"]
    search_fields = ["raw_text", "normalized_text", "city", "region_name", "country_name"]
    readonly_fields = ["created_at", "looked_up_at"]


@admin.register(PlatformEngineConfig)
class PlatformEngineConfigAdmin(admin.ModelAdmin):
    list_display = ["platform", "auto_backfill", "backfill_priority", "fetch_cadence_hours", "inter_request_delay_ms", "is_active"]
    list_filter = ["auto_backfill", "is_active"]


@admin.register(HarvestEngineConfig)
class HarvestEngineConfigAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "selective_filter_enabled",
        "filter_audit_mode",
        "pre_storage_filter_enabled",
        "pre_storage_strict_strong_only",
        "jd_gate_enabled",
        "jd_gate_audit_mode",
        "jd_gate_scope",
        "auto_backfill_jd",
        "auto_sync_to_pool",
        "updated_at",
    ]
    readonly_fields = ["updated_at", "updated_by"]

    def has_add_permission(self, request):
        return False


@admin.register(VetGateConfig)
class VetGateConfigAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "allow_unknown_country",
        "allow_possible_filter",
        "require_description",
        "auto_sync_after_harvest",
        "default_chunk_size",
    ]

    def has_add_permission(self, request):
        return False


@admin.register(HarvestOpsRun)
class HarvestOpsRunAdmin(admin.ModelAdmin):
    list_display = [
        "operation",
        "status",
        "progress_current",
        "progress_total",
        "last_heartbeat_at",
        "created_at",
        "finished_at",
    ]
    list_filter = ["operation", "status", "created_at"]
    search_fields = ["celery_task_id", "progress_message"]
    readonly_fields = [
        "operation",
        "celery_task_id",
        "status",
        "audit_payload",
        "progress_current",
        "progress_total",
        "progress_message",
        "last_heartbeat_at",
        "triggered_by_user",
        "created_at",
        "finished_at",
    ]

    def has_add_permission(self, request):
        return False

from django.contrib import admin
from .models import (
    Job,
    JobEmbedding,
    MatchScore,
    RawJobClassificationConflict,
    RawJobClassificationSnapshot,
    RawJobClassifierRun,
)


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'company',
        'primary_marketing_role',
        'location',
        'posted_by',
        'status',
        'possibly_filled',
        'original_link_health',
        'original_link_is_live',
        'created_at',
    )
    list_filter = ('status', 'job_type', 'possibly_filled', 'original_link_health', 'created_at')
    search_fields = ('title', 'company', 'description', 'original_link')
    date_hierarchy = 'created_at'


@admin.register(JobEmbedding)
class JobEmbeddingAdmin(admin.ModelAdmin):
    list_display = ('job', 'model', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(MatchScore)
class MatchScoreAdmin(admin.ModelAdmin):
    list_display = ('job', 'consultant', 'score_pct', 'rank', 'computed_at')
    list_filter = ('job',)
    ordering = ('job', 'rank')


@admin.register(RawJobClassifierRun)
class RawJobClassifierRunAdmin(admin.ModelAdmin):
    list_display = (
        'raw_job',
        'provider',
        'provider_role',
        'status',
        'confidence',
        'completed_at',
    )
    list_filter = ('provider', 'provider_role', 'status')
    search_fields = ('raw_job__title', 'raw_job__company_name', 'raw_job__original_url', 'input_hash')
    readonly_fields = ('started_at', 'completed_at', 'created_at', 'updated_at')


@admin.register(RawJobClassificationSnapshot)
class RawJobClassificationSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        'raw_job',
        'status',
        'approval_state',
        'approved_source',
        'approved_primary_role_slug',
        'final_confidence',
        'needs_review',
        'ready_for_vetting',
        'pushed_to_vetting_with_warnings',
        'last_merged_at',
    )
    list_filter = ('status', 'approval_state', 'needs_review', 'ready_for_vetting', 'pushed_to_vetting_with_warnings')
    search_fields = ('raw_job__title', 'raw_job__company_name', 'current_input_hash')
    readonly_fields = (
        'created_at', 'updated_at', 'last_merged_at', 'approved_at', 'pushed_to_vetting_at',
        'approved_primary_role_slug', 'primary_role_source', 'primary_role_locked',
        'primary_role_override_reason', 'primary_role_overridden_at', 'primary_role_overridden_by',
    )


@admin.register(RawJobClassificationConflict)
class RawJobClassificationConflictAdmin(admin.ModelAdmin):
    list_display = ('raw_job', 'field_path', 'resolution', 'severity', 'note', 'created_at')
    list_filter = ('resolution', 'severity')
    search_fields = ('raw_job__title', 'raw_job__company_name', 'field_path', 'note')
    readonly_fields = ('created_at',)

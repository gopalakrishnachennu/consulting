from django.contrib import admin
from .models import (
    PlatformConfig,
    LLMConfig,
    LLMConfigVersion,
    LLMUsageLog,
    AuditLog,
    Notification,
    BroadcastMessage,
    BroadcastDelivery,
)


class BroadcastDeliveryInline(admin.TabularInline):
    model = BroadcastDelivery
    extra = 0
    readonly_fields = ('user', 'notification', 'status', 'created_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(BroadcastMessage)
class BroadcastMessageAdmin(admin.ModelAdmin):
    list_display = ('title', 'audience', 'kind', 'created_by', 'created_at')
    list_filter = ('audience', 'kind', 'created_at')
    inlines = [BroadcastDeliveryInline]


@admin.register(PlatformConfig)
class PlatformConfigAdmin(admin.ModelAdmin):
    fieldsets = (
        ('Branding', {
            'fields': ('site_name', 'site_tagline', 'logo_url'),
        }),
        ('SEO', {
            'fields': ('meta_description', 'meta_keywords'),
        }),
        ('Contact', {
            'fields': ('contact_email', 'support_phone', 'address'),
        }),
        ('Feature flags', {
            'fields': (
                'enable_consultant_registration',
                'enable_job_applications',
                'enable_public_consultant_view',
                'match_jd_title_default',
                'enable_consultant_global_interview_calendar',
            ),
        }),
        ('System & maintenance', {
            'fields': (
                'maintenance_mode',
                'maintenance_message',
                'session_timeout_minutes',
                'max_upload_size_mb',
            ),
        }),
        ('Email ingestion (IMAP)', {
            'classes': ('collapse',),
            'fields': (
                'email_ingest_enabled',
                'email_imap_host',
                'email_imap_port',
                'email_imap_use_ssl',
                'email_imap_username',
                'email_imap_encrypted_password',
                'email_poll_interval_seconds',
                'email_auto_poll_enabled',
                'email_ai_fallback_enabled',
                'email_ai_confidence_threshold',
                'email_notify_employee_on_auto_update',
                'email_notify_consultant_on_auto_update',
            ),
        }),
        ('Company enrichment & API keys', {
            'classes': ('collapse',),
            'fields': (
                'google_kg_api_key',
                'apollo_api_key',
                'hunter_api_key',
                'auto_enrich_on_create',
            ),
        }),
        ('Jobs pipeline (auto-close)', {
            'description': (
                'Closes stale OPEN jobs on a schedule. Requires Celery worker and beat '
                '(see auto_close_jobs in apps/jobs/tasks.py).'
            ),
            'fields': ('job_auto_close_after_days', 'job_auto_close_when_link_dead'),
        }),
        ('Social & legal', {
            'fields': (
                'twitter_url',
                'linkedin_url',
                'github_url',
                'tos_url',
                'privacy_policy_url',
            ),
        }),
    )

    def has_add_permission(self, request):
        return not PlatformConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LLMConfig)
class LLMConfigAdmin(admin.ModelAdmin):
    list_display = ('active_model', 'generation_enabled', 'monthly_token_cap', 'updated_at')


@admin.register(LLMConfigVersion)
class LLMConfigVersionAdmin(admin.ModelAdmin):
    list_display = ('active_model', 'created_at')


@admin.register(LLMUsageLog)
class LLMUsageLogAdmin(admin.ModelAdmin):
    list_display = ('model_name', 'total_tokens', 'cost_total', 'latency_ms', 'success', 'created_at')
    list_filter = ('model_name', 'success', 'created_at')


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('actor', 'action', 'target_model', 'target_id', 'timestamp')


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'kind', 'title', 'read_at', 'dedupe_key', 'created_at')
    list_filter = ('kind', 'read_at')
    search_fields = ('title', 'body', 'user__username', 'dedupe_key')


@admin.register(BroadcastDelivery)
class BroadcastDeliveryAdmin(admin.ModelAdmin):
    list_display = ('broadcast', 'user', 'status', 'notification', 'created_at')
    list_filter = ('status',)
    search_fields = ('broadcast__title', 'user__username')

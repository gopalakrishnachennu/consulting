from django.contrib import admin
from .models import (
    PlatformConfig,
    PublicSiteContent,
    LLMConfig,
    LLMConfigVersion,
    LLMUsageLog,
    AuditLog,
    Notification,
    BroadcastMessage,
    BroadcastDelivery,
    FeatureFlag,
    EmployeeDesignation,
    ErrorLog,
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


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ('key', 'label', 'category', 'applies_to', 'is_enabled', 'enabled_for_consultants', 'enabled_for_employees', 'sort_order')
    list_filter = ('category', 'applies_to', 'is_enabled')
    search_fields = ('key', 'label')
    ordering = ('sort_order', 'key')


@admin.register(EmployeeDesignation)
class EmployeeDesignationAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'level', 'is_active')
    filter_horizontal = ('allowed_features',)
    search_fields = ('name', 'slug')


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


@admin.register(PublicSiteContent)
class PublicSiteContentAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Hero", {
            "fields": (
                "hero_badge",
                "hero_title",
                "hero_body",
                "hero_primary_label",
                "hero_primary_url",
                "hero_secondary_label",
                "hero_secondary_url",
            ),
        }),
        ("Proof strip", {
            "fields": (
                "proof_stat_1_value",
                "proof_stat_1_label",
                "proof_stat_2_value",
                "proof_stat_2_label",
                "proof_stat_3_value",
                "proof_stat_3_label",
            ),
        }),
        ("Workflow", {
            "fields": (
                "workflow_title",
                "workflow_body",
                "workflow_step_1_title",
                "workflow_step_1_body",
                "workflow_step_2_title",
                "workflow_step_2_body",
                "workflow_step_3_title",
                "workflow_step_3_body",
                "workflow_step_4_title",
                "workflow_step_4_body",
            ),
        }),
        ("Consultant and employer sections", {
            "fields": (
                "consultant_title",
                "consultant_body",
                "consultant_cta_label",
                "employer_title",
                "employer_body",
                "employer_cta_label",
            ),
        }),
        ("Careers and auth", {
            "fields": (
                "careers_title",
                "careers_body",
                "signin_title",
                "signin_body",
                "consultant_onboarding_title",
                "consultant_onboarding_body",
                "employee_onboarding_title",
                "employee_onboarding_body",
            ),
        }),
    )

    def has_add_permission(self, request):
        return not PublicSiteContent.objects.exists()

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
    list_display = (
        'timestamp',
        'actor',
        'event_code',
        'outcome',
        'action',
        'target_model',
        'target_id',
    )
    list_filter = ('outcome', 'timestamp')
    search_fields = ('action', 'event_code', 'human_summary', 'correlation_id', 'url_name')


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


@admin.register(ErrorLog)
class ErrorLogAdmin(admin.ModelAdmin):
    list_display = ('severity', 'method', 'path', 'status_code', 'user', 'error_type', 'response_time_ms', 'resolved', 'created_at')
    list_filter = ('severity', 'resolved', 'status_code', 'created_at')
    search_fields = ('path', 'error_message', 'error_type', 'user__username')
    readonly_fields = ('severity', 'path', 'method', 'status_code', 'user', 'error_type', 'error_message', 'traceback', 'request_data', 'response_time_ms', 'user_agent', 'ip_address', 'created_at')
    list_per_page = 50
    date_hierarchy = 'created_at'

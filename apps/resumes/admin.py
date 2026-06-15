from django.contrib import admin
from .models import ResumeDraft, MasterPrompt, SectionPrompt, PipelineRun, JDExtractorPrompt


@admin.register(JDExtractorPrompt)
class JDExtractorPromptAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_by', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'prompt_text')


@admin.register(ResumeDraft)
class ResumeDraftAdmin(admin.ModelAdmin):
    list_display = ('consultant', 'job', 'version', 'status', 'ats_score', 'tokens_used', 'created_by', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('consultant__user__username', 'job__title')
    readonly_fields = ('version', 'tokens_used', 'created_at')


class SectionPromptInline(admin.TabularInline):
    model = SectionPrompt
    extra = 0
    fields = ('section_type', 'system_prompt', 'generation_rules', 'temperature_override', 'max_tokens_override')


@admin.register(MasterPrompt)
class MasterPromptAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active', 'created_by', 'updated_at')
    list_filter = ('is_active',)
    inlines = [SectionPromptInline]


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display = ('draft', 'consultant', 'job', 'total_tokens', 'total_cost', 'total_llm_calls', 'started_at')
    list_filter = ('started_at',)
    readonly_fields = (
        'draft', 'consultant', 'job', 'started_at', 'completed_at',
        'jd_intelligence', 'matching_matrix', 'section_results',
        'quality_gate', 'total_tokens', 'total_cost', 'total_llm_calls', 'total_latency_ms',
    )
    search_fields = ('consultant__user__username', 'job__title')

from django.contrib import admin
from .models import Job, JobEmbedding, MatchScore


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'company',
        'location',
        'posted_by',
        'status',
        'possibly_filled',
        'original_link_is_live',
        'created_at',
    )
    list_filter = ('status', 'job_type', 'possibly_filled', 'created_at')
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

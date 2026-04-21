from django.contrib import admin
from .models import DailySnapshot, FunnelEvent, RevenueRecord


@admin.register(DailySnapshot)
class DailySnapshotAdmin(admin.ModelAdmin):
    list_display = ('date', 'jobs_live', 'jobs_in_pool', 'submissions_total', 'active_placements', 'revenue_mtd', 'avg_margin_pct')
    ordering = ('-date',)
    date_hierarchy = 'date'
    readonly_fields = ('created_at',)


@admin.register(FunnelEvent)
class FunnelEventAdmin(admin.ModelAdmin):
    list_display = ('stage', 'consultant', 'job', 'source', 'occurred_at')
    list_filter = ('stage', 'source')
    search_fields = ('consultant__user__username', 'job__title')
    ordering = ('-occurred_at',)
    date_hierarchy = 'occurred_at'


@admin.register(RevenueRecord)
class RevenueRecordAdmin(admin.ModelAdmin):
    list_display = ('placement', 'period_start', 'period_end', 'hours_billed', 'bill_rate', 'gross_revenue', 'margin_pct')
    ordering = ('-period_start',)
    date_hierarchy = 'period_start'

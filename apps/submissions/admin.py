from django.contrib import admin
from .models import (
    ApplicationSubmission,
    SubmissionResponse,
    SubmissionStatusHistory,
    Offer,
    OfferRound,
    EmailEvent,
)


@admin.register(ApplicationSubmission)
class ApplicationSubmissionAdmin(admin.ModelAdmin):
    list_display = ('job', 'consultant', 'status', 'submitted_by', 'created_at')
    list_filter = ('status', 'created_at')


@admin.register(SubmissionResponse)
class SubmissionResponseAdmin(admin.ModelAdmin):
    list_display = ('submission', 'response_type', 'status', 'responded_at', 'created_by')
    list_filter = ('status', 'response_type', 'responded_at')


@admin.register(SubmissionStatusHistory)
class SubmissionStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ('submission', 'from_status', 'to_status', 'created_at')
    list_filter = ('to_status',)


class OfferRoundInline(admin.TabularInline):
    model = OfferRound
    extra = 0


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ('submission', 'initial_salary', 'final_salary', 'accepted_at')
    inlines = [OfferRoundInline]


@admin.register(EmailEvent)
class EmailEventAdmin(admin.ModelAdmin):
    list_display = (
        'received_at',
        'from_address',
        'subject',
        'detected_status',
        'confidence',
        'matched_submission',
        'applied_action',
    )
    list_filter = (
        'detected_status',
        'applied_action',
        'received_at',
    )
    search_fields = (
        'from_address',
        'to_address',
        'subject',
        'body_snippet',
        'raw_message_id',
    )

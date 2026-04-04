from django.urls import path
from .views import (
    SubmissionCreateView, SubmissionListView, SubmissionUpdateView,
    SubmissionClaimView, SubmissionDetailView, SubmissionExportCSVView,
    SubmissionBulkStatusView, SubmissionInlineStatusView,
    SubmissionMarkRejectedView, RejectionAnalysisView,
    EmailEventListView, EmailEventPollNowView, EmailEventDetailView,
    # Phase 1: Placements, Timesheets, Commissions
    PlacementListView, PlacementDetailView, PlacementCreateView, PlacementUpdateView,
    TimesheetCreateView, TimesheetApproveView, TimesheetListView,
    CommissionCreateView, CommissionListView, CommissionUpdateView,
)

urlpatterns = [
    path('', SubmissionListView.as_view(), name='submission-list'),
    path('export/', SubmissionExportCSVView.as_view(), name='submission-export-csv'),
    path('bulk-status/', SubmissionBulkStatusView.as_view(), name='submission-bulk-status'),
    path('log/', SubmissionCreateView.as_view(), name='submission-create'),
    path('email-events/', EmailEventListView.as_view(), name='email-event-list'),
    path('email-events/poll-now/', EmailEventPollNowView.as_view(), name='email-event-poll-now'),
    path('email-events/<int:pk>/', EmailEventDetailView.as_view(), name='email-event-detail'),
    path('<int:pk>/status/', SubmissionInlineStatusView.as_view(), name='submission-inline-status'),
    path('<int:pk>/update/', SubmissionUpdateView.as_view(), name='submission-update'),
    path('<int:pk>/', SubmissionDetailView.as_view(), name='submission-detail'),
    path('<int:pk>/mark-rejected/', SubmissionMarkRejectedView.as_view(), name='submission-mark-rejected'),
    path('<int:pk>/rejection-analysis/', RejectionAnalysisView.as_view(), name='submission-rejection-analysis'),
    path('claim/<int:draft_id>/', SubmissionClaimView.as_view(), name='submission-claim'),

    # Phase 1: Placements
    path('placements/', PlacementListView.as_view(), name='placement-list'),
    path('placements/<int:pk>/', PlacementDetailView.as_view(), name='placement-detail'),
    path('placements/<int:pk>/edit/', PlacementUpdateView.as_view(), name='placement-update'),
    path('<int:submission_pk>/place/', PlacementCreateView.as_view(), name='placement-create'),

    # Phase 1: Timesheets
    path('timesheets/', TimesheetListView.as_view(), name='timesheet-list'),
    path('placements/<int:placement_pk>/timesheets/add/', TimesheetCreateView.as_view(), name='timesheet-create'),
    path('timesheets/<int:pk>/approve/', TimesheetApproveView.as_view(), name='timesheet-approve'),

    # Phase 1: Commissions
    path('commissions/', CommissionListView.as_view(), name='commission-list'),
    path('commissions/<int:pk>/edit/', CommissionUpdateView.as_view(), name='commission-update'),
    path('placements/<int:placement_pk>/commissions/add/', CommissionCreateView.as_view(), name='commission-create'),
]

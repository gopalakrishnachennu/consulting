from django.urls import path
from .views import (
    SubmissionCreateView, SubmissionQuickSubmitView, SubmissionListView, SubmissionUpdateView,
    SubmissionKanbanView, SubmissionKanbanMoveView,
    SubmissionClaimView, SubmissionDetailView, SubmissionExportCSVView,
    SubmissionBulkStatusView, SubmissionInlineStatusView,
    SubmissionMarkRejectedView, RejectionAnalysisView,
    EmailEventListView, EmailEventPollNowView, EmailEventDetailView,
    # Phase 1: Placements, Timesheets, Commissions
    PlacementListView, PlacementDetailView, PlacementCreateView, PlacementUpdateView,
    TimesheetCreateView, TimesheetApproveView, TimesheetListView,
    CommissionCreateView, CommissionListView, CommissionUpdateView,
    # Phase 4+5
    FollowUpReminderCreateView, FollowUpReminderDismissView, StaleSubmissionsView,
    SubmissionArchiveView, SubmissionRestoreView, ArchivedSubmissionsView,
    GDPRExportView, WinLossAnalysisView,
)
from .views import (
    WorkflowDashboardView, WorkflowPanelView, WorkflowStarToggleView,
    ConsultantClaimView, ConsultantReleaseView,
    LockHeartbeatView, LockOverrideView,
    MarkExternalApplicationView,
)

urlpatterns = [
    path('', SubmissionListView.as_view(), name='submission-list'),
    path('kanban/', SubmissionKanbanView.as_view(), name='submission-kanban'),
    path('kanban/move/', SubmissionKanbanMoveView.as_view(), name='submission-kanban-move'),
    path('quick-submit/', SubmissionQuickSubmitView.as_view(), name='submission-quick-submit'),
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

    # Phase 4: Follow-up reminders
    path('<int:pk>/reminder/', FollowUpReminderCreateView.as_view(), name='followup-reminder-create'),
    path('reminders/<int:pk>/dismiss/', FollowUpReminderDismissView.as_view(), name='followup-reminder-dismiss'),
    path('stale/', StaleSubmissionsView.as_view(), name='stale-submissions'),

    # Phase 5: Archive / Restore
    path('<int:pk>/archive/', SubmissionArchiveView.as_view(), name='submission-archive'),
    path('<int:pk>/restore/', SubmissionRestoreView.as_view(), name='submission-restore'),
    path('archived/', ArchivedSubmissionsView.as_view(), name='submission-archived'),

    # Phase 5: GDPR export
    path('gdpr-export/<int:pk>/', GDPRExportView.as_view(), name='gdpr-export'),

    # Phase 5: Win/Loss analysis
    path('win-loss/', WinLossAnalysisView.as_view(), name='win-loss-analysis'),

    # Consultant Workflow Pipeline
    path('workflow/', WorkflowDashboardView.as_view(), name='workflow-dashboard'),
    path('workflow/star/<int:pk>/', WorkflowStarToggleView.as_view(), name='workflow-star-toggle'),
    path('workflow/consultant/<int:pk>/', WorkflowPanelView.as_view(), name='workflow-panel'),
    path('workflow/claim/<int:pk>/', ConsultantClaimView.as_view(), name='workflow-claim'),
    path('workflow/release/<int:pk>/', ConsultantReleaseView.as_view(), name='workflow-release'),
    path('workflow/heartbeat/<int:pk>/', LockHeartbeatView.as_view(), name='workflow-heartbeat'),
    path('workflow/override/<int:pk>/', LockOverrideView.as_view(), name='workflow-override'),
    path('workflow/mark-external/<int:consultant_pk>/job/<int:job_pk>/', MarkExternalApplicationView.as_view(), name='workflow-mark-external'),
]

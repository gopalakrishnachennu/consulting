from django.urls import path
from .views import (
    SubmissionCreateView, SubmissionListView, SubmissionUpdateView,
    SubmissionClaimView, SubmissionDetailView, SubmissionExportCSVView,
    SubmissionBulkStatusView, SubmissionInlineStatusView,
    SubmissionMarkRejectedView, RejectionAnalysisView,
    EmailEventListView, EmailEventPollNowView, EmailEventDetailView,
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
]

from django.urls import path

from .views import (
    InboxView,
    ThreadDetailView,
    StartThreadView,
    StartOrgThreadView,
    MessagingRecipientSearchView,
    MessageEditView,
    MessageDeleteView,
    ThreadTypingPingView,
    ThreadTypingStatusView,
)

urlpatterns = [
    path("", InboxView.as_view(), name="inbox"),
    path("search/", MessagingRecipientSearchView.as_view(), name="messaging-search"),
    path("start/<int:user_id>/", StartThreadView.as_view(), name="start-thread"),
    path("start-org/", StartOrgThreadView.as_view(), name="start-org-thread"),
    path("thread/<int:pk>/", ThreadDetailView.as_view(), name="thread-detail"),
    path("thread/<int:pk>/typing/", ThreadTypingPingView.as_view(), name="thread-typing-ping"),
    path("thread/<int:pk>/typing/status/", ThreadTypingStatusView.as_view(), name="thread-typing-status"),
    path("message/<int:pk>/edit/", MessageEditView.as_view(), name="message-edit"),
    path("message/<int:pk>/delete/", MessageDeleteView.as_view(), name="message-delete"),
]

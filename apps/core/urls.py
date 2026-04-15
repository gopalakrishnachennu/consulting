from django.urls import path
from .views import (
    home,
    GlobalSearchView,
    GlobalSearchPartialView,
    PlatformConfigView,
    DataPipelineDashboardView,
    SystemStatusView,
    HealthcheckJSONView,
    LLMConfigView,
    LLMLogListView,
    LLMLogDetailView,
    AuditLogListView,
    WarRoomDashboardView,
    HelpCenterView,
    NotificationListView,
    NotificationMarkReadView,
    NotificationMarkAllReadView,
    # Phase 6: Master Prompt
    MasterPromptListView,
    MasterPromptCreateView,
    MasterPromptEditView,
    MasterPromptActivateView,
    BroadcastListView,
    BroadcastCreateView,
    BroadcastDetailView,
    FeatureControlCenterView,
    MyFeaturesJsonView,
    # Task Scheduler
    TaskSchedulerView,
    TaskToggleView,
    TaskEditScheduleView,
    TaskRunNowView,
    TaskProgressAPIView,
)

urlpatterns = [
    path('', home, name='home'),
    path('setup/', PlatformConfigView.as_view(), name='platform-config'),
    path('data-pipeline/', DataPipelineDashboardView.as_view(), name='data-pipeline'),
    path('audit/', AuditLogListView.as_view(), name='audit-log'),
    path('status/', SystemStatusView.as_view(), name='system-status'),
    path('health/', HealthcheckJSONView.as_view(), name='health-json'),
    path('llm/', LLMConfigView.as_view(), name='llm-config'),
    path('llm/logs/', LLMLogListView.as_view(), name='llm-logs'),
    path('llm/logs/<int:pk>/', LLMLogDetailView.as_view(), name='llm-log-detail'),
    path('war-room/', WarRoomDashboardView.as_view(), name='war-room'),
    path('help/', HelpCenterView.as_view(), name='settings-help'),
    path('search/', GlobalSearchView.as_view(), name='global-search'),
    path('search/partial/', GlobalSearchPartialView.as_view(), name='global-search-partial'),
    path('notifications/', NotificationListView.as_view(), name='notification-list'),
    path('notifications/read-all/', NotificationMarkAllReadView.as_view(), name='notification-mark-all-read'),
    path('notifications/<int:pk>/read/', NotificationMarkReadView.as_view(), name='notification-mark-read'),

    # Phase 6: Master Prompt Editor
    path('master-prompt/', MasterPromptListView.as_view(), name='master-prompt-list'),
    path('master-prompt/new/', MasterPromptCreateView.as_view(), name='master-prompt-create'),
    path('master-prompt/<int:pk>/edit/', MasterPromptEditView.as_view(), name='master-prompt-edit'),
    path('master-prompt/<int:pk>/activate/', MasterPromptActivateView.as_view(), name='master-prompt-activate'),

    path('broadcasts/', BroadcastListView.as_view(), name='broadcast-list'),
    path('broadcasts/new/', BroadcastCreateView.as_view(), name='broadcast-create'),
    path('broadcasts/<int:pk>/', BroadcastDetailView.as_view(), name='broadcast-detail'),

    path('feature-control/', FeatureControlCenterView.as_view(), name='feature-control-center'),
    path('api/my-features/', MyFeaturesJsonView.as_view(), name='api-my-features'),
    path('api/task-progress/<str:task_id>/', TaskProgressAPIView.as_view(), name='api-task-progress'),

    # Task Scheduler
    path('task-scheduler/', TaskSchedulerView.as_view(), name='task-scheduler'),
    path('task-scheduler/<int:pk>/toggle/', TaskToggleView.as_view(), name='task-toggle'),
    path('task-scheduler/<int:pk>/edit/', TaskEditScheduleView.as_view(), name='task-edit-schedule'),
    path('task-scheduler/<int:pk>/run/', TaskRunNowView.as_view(), name='task-run-now'),
]

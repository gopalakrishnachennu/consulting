from django.urls import path
from .views import (
    home,
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
]

from django.urls import path
from .views import AnalyticsDashboardView, AnalyticsExportCSVView

urlpatterns = [
    path('', AnalyticsDashboardView.as_view(), name='analytics-dashboard'),
    path('export/', AnalyticsExportCSVView.as_view(), name='analytics-export-csv'),
]

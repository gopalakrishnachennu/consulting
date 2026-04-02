from django.urls import path

from .views import (
    CompanyListView,
    CompanyDetailView,
    CompanyCreateView,
    CompanyUpdateView,
    CompanyExportCSVView,
    CompanyDuplicateReviewView,
    CompanyMergeView,
    CompanyCSVImportView,
    CompanyDomainImportView,
    CompanyLinkedInImportView,
    CompanySearchView,
    CompanyReEnrichView,
    CompanyEnrichmentStatusView,
    CompanyCreateAPIView,
    EnrichmentLogListView,
)


urlpatterns = [
    path("", CompanyListView.as_view(), name="company-list"),
    path("new/", CompanyCreateView.as_view(), name="company-create"),
    path("export/", CompanyExportCSVView.as_view(), name="company-export-csv"),
    path("import/csv/", CompanyCSVImportView.as_view(), name="company-import-csv"),
    path("import/domains/", CompanyDomainImportView.as_view(), name="company-import-domains"),
    path("import/linkedin/", CompanyLinkedInImportView.as_view(), name="company-import-linkedin"),
    path("search/", CompanySearchView.as_view(), name="company-search"),
    path("api/create/", CompanyCreateAPIView.as_view(), name="company-api-create"),
    path("duplicates/", CompanyDuplicateReviewView.as_view(), name="company-duplicate-review"),
    path("merge/", CompanyMergeView.as_view(), name="company-merge"),
    path("enrichment/", CompanyEnrichmentStatusView.as_view(), name="company-enrichment-status"),
    path("enrichment/logs/", EnrichmentLogListView.as_view(), name="company-enrichment-logs"),
    path("<int:pk>/", CompanyDetailView.as_view(), name="company-detail"),
    path("<int:pk>/re-enrich/", CompanyReEnrichView.as_view(), name="company-re-enrich"),
    path("<int:pk>/edit/", CompanyUpdateView.as_view(), name="company-edit"),
]


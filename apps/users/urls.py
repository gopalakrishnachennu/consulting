from django.urls import path
from .views import (
    ConsultantListView, ConsultantExportCSVView,
    EmployeeListView, EmployeeExportCSVView, EmployeeDetailView, EmployeeCreateView,
    EmployeeEditView, ConsultantEditView, ConsultantDetailView, ConsultantDashboardView,
    SaveJobView, SavedJobListView, ConsultantCreateView,
    MarketingRoleListView, MarketingRoleCreateView, MarketingRoleUpdateView, MarketingRoleDeleteView,
    SettingsView,
    ExperienceCreateView, ExperienceUpdateView, ExperienceDeleteView,
    EducationCreateView, EducationUpdateView, EducationDeleteView,
    CertificationCreateView, CertificationUpdateView, CertificationDeleteView,
)
from resumes.views import DraftGenerateView
from resumes.preview_views import draft_preview_llm


urlpatterns = [
    path('employees/export/', EmployeeExportCSVView.as_view(), name='employee-export-csv'),
    path('employees/', EmployeeListView.as_view(), name='employee-list'),
    path('employees/create/', EmployeeCreateView.as_view(), name='employee-create'),
    
    path('settings/', SettingsView.as_view(), name='settings-dashboard'),
    
    path('', ConsultantListView.as_view(), name='consultant-list'),
    path('export/', ConsultantExportCSVView.as_view(), name='consultant-export-csv'),
    path('add/', ConsultantCreateView.as_view(), name='consultant-add'),
    path('<int:pk>/', ConsultantDetailView.as_view(), name='consultant-detail'),
    path('<int:pk>/edit/', ConsultantEditView.as_view(), name='consultant-edit'),
    path('dashboard/', ConsultantDashboardView.as_view(), name='consultant-dashboard'),

    # Experience CRUD (self)
    path('experience/add/', ExperienceCreateView.as_view(), name='experience-add'),
    path('experience/<int:pk>/edit/', ExperienceUpdateView.as_view(), name='experience-edit'),
    path('experience/<int:pk>/delete/', ExperienceDeleteView.as_view(), name='experience-delete'),

    # Education CRUD (self)
    path('education/add/', EducationCreateView.as_view(), name='education-add'),
    path('education/<int:pk>/edit/', EducationUpdateView.as_view(), name='education-edit'),
    path('education/<int:pk>/delete/', EducationDeleteView.as_view(), name='education-delete'),

    # Certification CRUD (self)
    path('certification/add/', CertificationCreateView.as_view(), name='certification-add'),
    path('certification/<int:pk>/edit/', CertificationUpdateView.as_view(), name='certification-edit'),
    path('certification/<int:pk>/delete/', CertificationDeleteView.as_view(), name='certification-delete'),

    # Admin: manage any consultant's profile items (pass consultant_pk)
    path('<int:consultant_pk>/experience/add/', ExperienceCreateView.as_view(), name='admin-experience-add'),
    path('<int:consultant_pk>/experience/<int:pk>/edit/', ExperienceUpdateView.as_view(), name='admin-experience-edit'),
    path('<int:consultant_pk>/experience/<int:pk>/delete/', ExperienceDeleteView.as_view(), name='admin-experience-delete'),
    path('<int:consultant_pk>/education/add/', EducationCreateView.as_view(), name='admin-education-add'),
    path('<int:consultant_pk>/education/<int:pk>/edit/', EducationUpdateView.as_view(), name='admin-education-edit'),
    path('<int:consultant_pk>/education/<int:pk>/delete/', EducationDeleteView.as_view(), name='admin-education-delete'),
    path('<int:consultant_pk>/certification/add/', CertificationCreateView.as_view(), name='admin-certification-add'),
    path('<int:consultant_pk>/certification/<int:pk>/edit/', CertificationUpdateView.as_view(), name='admin-certification-edit'),
    path('<int:consultant_pk>/certification/<int:pk>/delete/', CertificationDeleteView.as_view(), name='admin-certification-delete'),

    # Saved Jobs
    path('saved-jobs/', SavedJobListView.as_view(), name='saved-jobs'),
    path('save-job/<int:pk>/', SaveJobView.as_view(), name='save-job'),

    # Marketing Roles CRUD (Admin)
    path('marketing-roles/', MarketingRoleListView.as_view(), name='marketing-role-list'),
    path('marketing-roles/add/', MarketingRoleCreateView.as_view(), name='marketing-role-add'),
    path('marketing-roles/<int:pk>/edit/', MarketingRoleUpdateView.as_view(), name='marketing-role-edit'),
    path('marketing-roles/<int:pk>/delete/', MarketingRoleDeleteView.as_view(), name='marketing-role-delete'),

    # Resume Draft Generation (Admin/Employee only)
    path('<int:pk>/drafts/generate/', DraftGenerateView.as_view(), name='draft-generate'),
    path('<int:pk>/drafts/preview/', draft_preview_llm, name='draft-preview-llm'),
]

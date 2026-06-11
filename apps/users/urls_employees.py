from django.urls import path
from django.views.generic import RedirectView

from .views import (
    EmployeeCreateView,
    EmployeeDeleteView,
    EmployeeDetailView,
    EmployeeEditView,
    EmployeeExportCSVView,
    EmployeeListView,
)

urlpatterns = [
    path('', EmployeeListView.as_view(), name='employee-list'),
    path('export/', EmployeeExportCSVView.as_view(), name='employee-export-csv'),
    path('add/', EmployeeCreateView.as_view(), name='employee-add'),
    path(
        'create/',
        RedirectView.as_view(pattern_name='employee-add', permanent=True),
        name='employee-create',
    ),
    path('<int:pk>/', EmployeeDetailView.as_view(), name='employee-detail'),
    path('<int:pk>/edit/', EmployeeEditView.as_view(), name='employee-edit'),
    path('<int:pk>/delete/', EmployeeDeleteView.as_view(), name='employee-delete'),
]

from django.urls import path
from .views import (
    DraftDetailView, DraftDownloadView, DraftPromoteView, DraftDeleteView,
    DraftRegenerateView, LLMInputPreferenceSaveView,
    DraftRegenerateSectionView,
    ResumeGeneratePageView, ResumeGenerateActionView,
    PreflightCheckView, DraftReviewView, PipelineResultsView,
    # Template editor
    ResumeEditorView, ResumeEditorSaveView, ResumeEditorPreviewView,
    ResumeExportDOCXView, ResumeExportPDFView,
    ResumeTemplateSaveView, ResumeTemplateDeleteView, ResumeTemplateListView,
    # Consultant self-resume & cover letter
    ConsultantResumeGeneratePageView,
    CoverLetterGenerateView, CoverLetterDetailView, CoverLetterDownloadView,
    # Interview prep
    InterviewPrepView,
)

urlpatterns = [
    path('generate/', ResumeGeneratePageView.as_view(), name='resume-generate'),
    path('generate/run/', ResumeGenerateActionView.as_view(), name='resume-generate-run'),
    path('generate/preflight/', PreflightCheckView.as_view(), name='resume-preflight'),
    path('drafts/<int:pk>/review/', DraftReviewView.as_view(), name='draft-review'),
    path('drafts/<int:pk>/pipeline/', PipelineResultsView.as_view(), name='pipeline-results'),

    path('drafts/<int:pk>/', DraftDetailView.as_view(), name='draft-detail'),
    path('drafts/<int:pk>/regenerate/', DraftRegenerateView.as_view(), name='draft-regenerate'),
    path('drafts/<int:pk>/regenerate-section/', DraftRegenerateSectionView.as_view(), name='draft-regenerate-section'),
    path('drafts/<int:pk>/save-input-defaults/', LLMInputPreferenceSaveView.as_view(), name='llm-input-defaults'),
    path('drafts/<int:pk>/download/', DraftDownloadView.as_view(), name='draft-download'),
    path('drafts/<int:pk>/promote/', DraftPromoteView.as_view(), name='draft-promote'),
    path('drafts/<int:pk>/delete/', DraftDeleteView.as_view(), name='draft-delete'),

    # ── Template Editor ──────────────────────────────────────────────
    path('drafts/<int:pk>/editor/', ResumeEditorView.as_view(), name='resume-editor'),
    path('drafts/<int:pk>/editor/save/', ResumeEditorSaveView.as_view(), name='resume-editor-save'),
    path('drafts/<int:pk>/editor/preview/', ResumeEditorPreviewView.as_view(), name='resume-editor-preview'),
    path('drafts/<int:pk>/export/docx/', ResumeExportDOCXView.as_view(), name='resume-export-docx'),
    path('drafts/<int:pk>/export/pdf/', ResumeExportPDFView.as_view(), name='resume-export-pdf'),

    # ── Template CRUD ────────────────────────────────────────────────
    path('templates/', ResumeTemplateListView.as_view(), name='resume-template-list'),
    path('templates/save/', ResumeTemplateSaveView.as_view(), name='resume-template-save'),
    path('templates/<int:pk>/delete/', ResumeTemplateDeleteView.as_view(), name='resume-template-delete'),

    # ── Consultant self-service resume generation ─────────────────────
    path('my-resume/', ConsultantResumeGeneratePageView.as_view(), name='consultant-resume-generate'),

    # ── Cover Letter ──────────────────────────────────────────────────
    path('cover-letter/', CoverLetterGenerateView.as_view(), name='cover-letter-generate'),
    path('cover-letter/<int:pk>/', CoverLetterDetailView.as_view(), name='cover-letter-detail'),
    path('cover-letter/<int:pk>/download/', CoverLetterDownloadView.as_view(), name='cover-letter-download'),

    # ── Interview Prep ────────────────────────────────────────────────
    path('interview-prep/<int:pk>/', InterviewPrepView.as_view(), name='interview-prep'),
]

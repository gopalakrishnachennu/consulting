from django.urls import reverse
import json
from django.views.generic import DetailView, View
from django.views import View as BaseView
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from .models import ResumeDraft, LLMInputPreference
from .forms import DraftGenerateForm
from .services import (
    LLMService, DocxService, build_input_summary, get_system_prompt_text,
    build_user_prompt_from_sections, score_ats, validate_resume,
    extract_section, replace_section, normalize_generated_resume
)
from users.models import ConsultantProfile
from core.models import LLMConfig
from prompts_app.models import Prompt
from jobs.services import JDParserService

class AdminOrEmployeeMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Only Admins and Employees can access draft features."""
    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in ('ADMIN', 'EMPLOYEE')


class DraftAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Admins/Employees or the owning consultant can view/download drafts."""
    def test_func(self):
        u = self.request.user
        if u.is_superuser or u.role in ('ADMIN', 'EMPLOYEE'):
            return True
        if u.role == 'CONSULTANT' and hasattr(u, 'consultant_profile'):
            draft_id = self.kwargs.get('pk')
            return ResumeDraft.objects.filter(pk=draft_id, consultant=u.consultant_profile).exists()
        return False


class DraftGenerateView(AdminOrEmployeeMixin, BaseView):
    """POST: Generate a new resume draft for a consultant + job."""

    def post(self, request, pk):
        consultant_profile = get_object_or_404(ConsultantProfile, user__pk=pk)
        form = DraftGenerateForm(request.POST)

        if not form.is_valid():
            messages.error(request, "Please select a valid job.")
            return redirect('consultant-detail', pk=pk)

        job = form.cleaned_data['job']
        if not job.parsed_jd:
            JDParserService.parse_job(job, actor=request.user)

        # Create draft record in PROCESSING state
        draft = ResumeDraft(
            consultant=consultant_profile,
            job=job,
            status=ResumeDraft.Status.PROCESSING,
            created_by=request.user,
        )
        llm = LLMService()
        user_prompt = llm._build_prompt(job, consultant_profile)
        draft.llm_system_prompt = get_system_prompt_text(job, consultant_profile)
        draft.llm_user_prompt = user_prompt
        draft.llm_input_summary = build_input_summary(job, consultant_profile)
        config = LLMConfig.load()
        draft.llm_request_payload = {
            "model": config.active_model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": draft.llm_system_prompt},
                {"role": "user", "content": draft.llm_user_prompt},
            ],
            "temperature": float(config.temperature),
            "max_tokens": config.max_output_tokens,
        }
        draft.save()

        # Generate content
        content, tokens, error = llm.generate_resume_content(job, consultant_profile, actor=request.user)

        if error:
            draft.status = ResumeDraft.Status.ERROR
            draft.error_message = error
            draft.save(skip_version=True)
            messages.error(request, f"Draft generation failed: {error}")
        else:
            normalized = normalize_generated_resume(content, job, consultant_profile)
            draft.content = normalized
            draft.tokens_used = tokens
            errors, warnings = validate_resume(draft.content)
            draft.validation_errors = errors
            draft.validation_warnings = warnings
            draft.ats_score = score_ats(job.description, draft.content)
            draft.status = ResumeDraft.Status.REVIEW if errors else ResumeDraft.Status.DRAFT
            draft.save(skip_version=True)
            messages.success(
                request,
                f"Resume draft v{draft.version} generated for {consultant_profile.user.get_full_name() or consultant_profile.user.username}!"
            )

        return redirect('consultant-detail', pk=pk)


class DraftDetailView(DraftAccessMixin, DetailView):
    """View a single draft's generated content."""
    model = ResumeDraft
    template_name = 'resumes/draft_detail.html'
    context_object_name = 'draft'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        draft = context['draft']
        llm = LLMService()
        user_prompt = draft.llm_user_prompt or llm._build_prompt(draft.job, draft.consultant)
        system_prompt = draft.llm_system_prompt or get_system_prompt_text(draft.job, draft.consultant)
        context['llm_system_prompt'] = system_prompt
        context['llm_user_prompt'] = user_prompt
        config = LLMConfig.load()
        context['prompt_options'] = Prompt.objects.filter(is_active=True).order_by('name')
        context['selected_prompt_id'] = config.active_prompt_id
        context['selected_prompt_name'] = config.active_prompt.name if config.active_prompt else None
        context['llm_input_summary'] = draft.llm_input_summary or build_input_summary(draft.job, draft.consultant)
        context['llm_request_payload'] = draft.llm_request_payload or {
            "model": config.active_model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(config.temperature),
            "max_tokens": config.max_output_tokens,
        }
        default_sections = [
            "name", "email", "phone", "jd_location",
            "professional_summary", "skills", "base_resume", "experience", "education", "jd_description",
        ]
        if self.request.user.is_authenticated:
            pref = LLMInputPreference.objects.filter(user=self.request.user).first()
            if pref and pref.sections:
                default_sections = pref.sections
        # Always enforce required sections in defaults
        for required in ("experience", "education", "base_resume"):
            if required not in default_sections:
                default_sections.append(required)
        context['llm_builder_defaults'] = default_sections
        context['llm_builder_data'] = {
            "name": draft.consultant.user.get_full_name() or draft.consultant.user.username,
            "email": draft.consultant.user.email or "Not provided.",
            "phone": draft.consultant.phone or "Not provided.",
            "jd_location": draft.job.location or "Not provided.",
            "jd_description": draft.job.description or "Not provided.",
            "base_resume": draft.consultant.base_resume_text or "",
            "skills": draft.consultant.skills or [],
            "has_base_resume": bool(draft.consultant.base_resume_text and draft.consultant.base_resume_text.strip()),
            "has_jd": bool(draft.job.description and draft.job.description.strip()),
            "experience_count": draft.consultant.experience.count(),
            "education_count": draft.consultant.education.count(),
            "experience": [
                {
                    "title": e.title,
                    "company": e.company,
                    "start_year": e.start_date.strftime('%Y') if e.start_date else "",
                    "end_year": "Present" if e.is_current else (e.end_date.strftime('%Y') if e.end_date else ""),
                    "description": e.description or "",
                }
                for e in draft.consultant.experience.all()
            ],
            "education": [
                {
                    "degree": e.degree,
                    "field_of_study": e.field_of_study,
                    "institution": e.institution,
                    "start_year": e.start_date.strftime('%Y') if e.start_date else "",
                    "end_year": e.end_date.strftime('%Y') if e.end_date else "Present",
                }
                for e in draft.consultant.education.all()
            ],
            "system_prompt": system_prompt,
            "model": config.active_model or "gpt-4o-mini",
            "temperature": float(config.temperature),
            "max_tokens": config.max_output_tokens,
        }
        # JD parse proof + summary line count for UI
        job = draft.job
        context['jd_parse_status'] = getattr(job, "parsed_jd_status", "")
        context['jd_parse_updated_at'] = getattr(job, "parsed_jd_updated_at", None)
        context['jd_parse_error'] = getattr(job, "parsed_jd_error", "")
        context['jd_parse_present'] = bool(getattr(job, "parsed_jd", None))
        summary_section = extract_section(draft.content or "", "PROFESSIONAL SUMMARY", [
            "PROFESSIONAL SUMMARY", "SKILLS", "PROFESSIONAL EXPERIENCE", "EDUCATION", "CERTIFICATIONS"
        ])
        if summary_section:
            lines = [l for l in summary_section.splitlines() if l.strip()][1:] if summary_section else []
            context['summary_line_count'] = len(lines)
        else:
            context['summary_line_count'] = 0
        return context


class LLMInputPreferenceSaveView(AdminOrEmployeeMixin, BaseView):
    """Save LLM input builder defaults for the current user."""

    def post(self, request, pk):
        sections = request.POST.getlist('sections')
        if not sections:
            raw = request.POST.get('sections_json')
            if raw:
                try:
                    sections = json.loads(raw)
                except Exception:
                    sections = []
        if not sections:
            raw = request.POST.get('sections')
            if raw:
                try:
                    sections = json.loads(raw)
                except Exception:
                    sections = []
        if not sections:
            messages.error(request, "Please select at least one section.")
            return redirect(f"{reverse('draft-detail', kwargs={'pk': pk})}#llm-builder")

        # Enforce required sections
        required = {"experience", "education", "base_resume"}
        if not required.issubset(set(sections)):
            messages.error(request, "Experience, Education, and Base Resume are required defaults.")
            return redirect(f"{reverse('draft-detail', kwargs={'pk': pk})}#llm-builder")

        pref, _ = LLMInputPreference.objects.get_or_create(user=request.user)
        pref.sections = sections
        pref.save()
        messages.success(request, "Default selections saved.")
        return redirect(f"{reverse('draft-detail', kwargs={'pk': pk})}#llm-builder")


class DraftRegenerateView(AdminOrEmployeeMixin, BaseView):
    """Regenerate a draft using explicit selected sections."""

    def post(self, request, pk):
        existing = get_object_or_404(ResumeDraft, pk=pk)
        consultant_profile = existing.consultant
        job = existing.job
        if not job.parsed_jd:
            JDParserService.parse_job(job, actor=request.user)

        sections = request.POST.getlist('sections')
        if not sections:
            sections = [
                "name", "email", "phone", "jd_location",
                "professional_summary", "skills", "base_resume", "experience", "education", "jd_description",
            ]

        missing_required = [s for s in ("experience", "education", "base_resume") if s not in sections]
        if missing_required:
            messages.error(request, "Experience, Education, and Base Resume are required to generate a resume.")
            return redirect(f"{reverse('draft-detail', kwargs={'pk': existing.pk})}#llm-builder")
        if not consultant_profile.base_resume_text or not consultant_profile.base_resume_text.strip():
            messages.error(request, "Base Resume is empty. Add it in the consultant profile before generating.")
            return redirect(f"{reverse('draft-detail', kwargs={'pk': existing.pk})}#llm-builder")
        if not job.description or not job.description.strip():
            messages.error(request, "Job Description is empty. Add the JD before generating.")
            return redirect(f"{reverse('draft-detail', kwargs={'pk': existing.pk})}#llm-builder")
        if consultant_profile.experience.count() < 1:
            messages.error(request, "Experience is required. Add at least one experience entry.")
            return redirect(f"{reverse('draft-detail', kwargs={'pk': existing.pk})}#llm-builder")
        if consultant_profile.education.count() < 1:
            messages.error(request, "Education is required. Add at least one education entry.")
            return redirect(f"{reverse('draft-detail', kwargs={'pk': existing.pk})}#llm-builder")

        # Enforce always-on sections
        if "jd_description" not in sections:
            sections.append("jd_description")
        if "professional_summary" not in sections:
            sections.append("professional_summary")

        user_prompt = build_user_prompt_from_sections(job, consultant_profile, sections)
        system_prompt = get_system_prompt_text(job, consultant_profile)

        config = LLMConfig.load()
        llm = LLMService()

        draft = ResumeDraft(
            consultant=consultant_profile,
            job=job,
            status=ResumeDraft.Status.PROCESSING,
            created_by=request.user,
            llm_system_prompt=system_prompt,
            llm_user_prompt=user_prompt,
            llm_input_summary=build_input_summary(job, consultant_profile),
            llm_request_payload={
                "model": config.active_model or "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": float(config.temperature),
                "max_tokens": config.max_output_tokens,
            },
        )
        draft.save()

        force_new = request.POST.get('force_new') == 'on'

        content, tokens, error = llm.generate_with_prompts(
            job, consultant_profile, system_prompt, user_prompt, actor=request.user, force_new=force_new
        )

        if error:
            draft.status = ResumeDraft.Status.ERROR
            draft.error_message = error
            draft.save(skip_version=True)
            messages.error(request, f"Draft generation failed: {error}")
        else:
            normalized = normalize_generated_resume(content, job, consultant_profile)
            draft.content = normalized
            draft.tokens_used = tokens
            errors, warnings = validate_resume(draft.content)
            draft.validation_errors = errors
            draft.validation_warnings = warnings
            draft.ats_score = score_ats(job.description, draft.content)
            draft.status = ResumeDraft.Status.REVIEW if errors else ResumeDraft.Status.DRAFT
            draft.save(skip_version=True)
            messages.success(request, f"Resume draft v{draft.version} generated.")

        return redirect('draft-detail', pk=draft.pk)


class DraftRegenerateSectionView(AdminOrEmployeeMixin, BaseView):
    """Regenerate a specific section for a draft."""

    SECTION_HEADINGS = [
        "Header",
        "Professional Summary",
        "Core Skills",
        "Professional Experience",
        "Education",
    ]

    def post(self, request, pk):
        existing = get_object_or_404(ResumeDraft, pk=pk)
        section = request.POST.get('section')
        if section not in {"summary", "skills", "experience", "education", "header"}:
            messages.error(request, "Invalid section.")
            return redirect('draft-detail', pk=pk)

        heading_map = {
            "summary": "Professional Summary",
            "skills": "Core Skills",
            "experience": "Professional Experience",
            "education": "Education",
            "header": "Header",
        }
        heading = heading_map[section]

        base_content = existing.content or ""
        current_section = extract_section(base_content, heading, self.SECTION_HEADINGS)
        if not current_section:
            messages.error(request, f"Section not found: {heading}.")
            return redirect('draft-detail', pk=pk)

        # Build focused user prompt
        job = existing.job
        consultant = existing.consultant
        system_prompt = existing.llm_system_prompt or get_system_prompt_text(job, consultant)
        header_note = "Do NOT change names, company names, or dates."
        if section == "header":
            header_note = "Do NOT change any personal details; keep name, email, phone, location unchanged."

        user_prompt = (
            f"Update ONLY the '{heading}' section based on the JD and consultant data.\n"
            f"{header_note}\n"
            f"Return the full '{heading}' section only (including the heading line).\n\n"
            f"--- JOB DESCRIPTION ---\n{job.description or 'Not provided.'}\n\n"
            f"--- CURRENT SECTION ---\n{current_section}\n\n"
        )

        llm = LLMService()
        content, tokens, error = llm.generate_with_prompts(
            job, consultant, system_prompt, user_prompt, actor=request.user, force_new=True
        )
        if error:
            messages.error(request, f"Section update failed: {error}")
            return redirect('draft-detail', pk=pk)

        new_section = content.strip()
        if heading not in new_section:
            new_section = f"{heading}\n{new_section}"

        updated_content = replace_section(base_content, heading, self.SECTION_HEADINGS, new_section)
        updated_content = normalize_generated_resume(updated_content, job, consultant)

        draft = ResumeDraft(
            consultant=consultant,
            job=job,
            status=ResumeDraft.Status.PROCESSING,
            created_by=request.user,
            llm_system_prompt=system_prompt,
            llm_user_prompt=user_prompt,
            llm_input_summary=build_input_summary(job, consultant),
            llm_request_payload={
                "model": llm.config.active_model or "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": float(llm.config.temperature),
                "max_tokens": llm.config.max_output_tokens,
            },
        )
        draft.save()

        draft.content = updated_content
        draft.tokens_used = tokens
        errors, warnings = validate_resume(updated_content)
        draft.validation_errors = errors
        draft.validation_warnings = warnings
        draft.ats_score = score_ats(job.description, updated_content)
        draft.status = ResumeDraft.Status.REVIEW if errors else ResumeDraft.Status.DRAFT
        draft.save(skip_version=True)

        messages.success(request, f"{heading} updated in draft v{draft.version}.")
        return redirect('draft-detail', pk=draft.pk)


class DraftSetPromptView(AdminOrEmployeeMixin, BaseView):
    """Set the active prompt used for LLM generation (global)."""

    def post(self, request, pk):
        prompt_id = request.POST.get('prompt_id')
        config = LLMConfig.load()

        if not prompt_id:
            config.active_prompt = None
            config.save()
            messages.success(request, "Prompt selection cleared.")
            return redirect('draft-detail', pk=pk)

        prompt = get_object_or_404(Prompt, pk=prompt_id, is_active=True)
        config.active_prompt = prompt
        config.save()
        messages.success(request, f"Prompt set to: {prompt.name}")
        return redirect('draft-detail', pk=pk)


class DraftDownloadView(DraftAccessMixin, BaseView):
    """Download a draft as .docx."""

    def get(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)

        if not draft.content:
            messages.error(request, "This draft has no content to download.")
            return redirect('draft-detail', pk=pk)

        docx_service = DocxService()
        buffer = docx_service.create_docx(draft.content)

        filename = f"resume_{draft.consultant.user.username}_{draft.job.title.replace(' ', '_')}_v{draft.version}.docx"

        response = HttpResponse(
            buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class DraftPromoteView(AdminOrEmployeeMixin, BaseView):
    """Promote a draft to FINAL status. Only one FINAL per consultant+job."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role == 'ADMIN'

    def post(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)

        # Demote any existing FINAL for this consultant+job
        ResumeDraft.objects.filter(
            consultant=draft.consultant, job=draft.job, status=ResumeDraft.Status.FINAL
        ).update(status=ResumeDraft.Status.DRAFT)

        draft.status = ResumeDraft.Status.FINAL
        draft.save(skip_version=True)
        messages.success(request, f"Draft v{draft.version} promoted to FINAL.")
        return redirect('consultant-detail', pk=draft.consultant.user.pk)


class DraftDeleteView(AdminOrEmployeeMixin, BaseView):
    """Delete a draft. Admin only."""

    def test_func(self):
        u = self.request.user
        return u.is_superuser or u.role in ('ADMIN', 'EMPLOYEE')

    def post(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)
        consultant_pk = draft.consultant.user.pk
        draft.delete()
        messages.success(request, "Draft deleted.")
        return redirect('consultant-detail', pk=consultant_pk)


# ─── Legacy views (kept for backward compat) ─────────────────────────
class ResumeCreateView(AdminOrEmployeeMixin, BaseView):
    """Legacy resume creation — redirects to consultant list."""
    def get(self, request):
        return redirect('consultant-list')


class ResumeDetailView(AdminOrEmployeeMixin, DetailView):
    """Legacy — redirects to new draft detail."""
    model = ResumeDraft
    template_name = 'resumes/draft_detail.html'
    context_object_name = 'draft'


class ResumeDownloadView(DraftDownloadView):
    """Legacy alias."""
    pass

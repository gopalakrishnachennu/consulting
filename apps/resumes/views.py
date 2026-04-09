from django.urls import reverse
import json
from django.views.generic import DetailView, View
from django.views import View as BaseView
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from .models import ResumeDraft, LLMInputPreference, MasterPrompt
from .services import (
    DocxService, score_ats, validate_resume,
    extract_section, replace_section, normalize_generated_resume
)
from .engine import (
    generate_resume,
    generate_section,
    merge_input_sections,
    parse_input_sections_from_request,
    preflight_check,
    validate_input_sections,
)
from users.models import ConsultantProfile
from core.models import LLMConfig
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


class DraftDetailView(DraftAccessMixin, DetailView):
    """View a single draft's generated content."""
    model = ResumeDraft
    template_name = 'resumes/draft_detail.html'
    context_object_name = 'draft'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        draft = context['draft']
        master = MasterPrompt.get_active()
        system_prompt = draft.llm_system_prompt or (master.system_prompt if master else "")
        user_prompt = draft.llm_user_prompt or ""
        context['llm_system_prompt'] = system_prompt
        context['llm_user_prompt'] = user_prompt
        config = LLMConfig.load()
        context['active_master_prompt'] = master
        context['llm_input_summary'] = draft.llm_input_summary or {}
        context['llm_request_payload'] = draft.llm_request_payload or {
            "model": config.active_model or "gpt-4o-mini",
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
    """Regenerate a draft using the Master Prompt engine (single LLM call)."""

    def post(self, request, pk):
        existing = get_object_or_404(ResumeDraft, pk=pk)
        consultant_profile = existing.consultant
        job = existing.job

        if not job.parsed_jd:
            JDParserService.parse_job(job, actor=request.user)

        post_sections = parse_input_sections_from_request(request)
        effective = merge_input_sections(MasterPrompt.get_active(), post_sections)
        v_err = validate_input_sections(effective)
        if v_err:
            messages.error(request, v_err)
            return redirect("draft-detail", pk=pk)

        content, tokens, error, metadata = generate_resume(
            job, consultant_profile, actor=request.user, input_sections=post_sections
        )

        draft = ResumeDraft(
            consultant=consultant_profile,
            job=job,
            status=ResumeDraft.Status.PROCESSING,
            created_by=request.user,
            llm_system_prompt=metadata.get("system_prompt", ""),
            llm_user_prompt=metadata.get("user_prompt", ""),
            llm_request_payload={
                "model": metadata.get("model", ""),
                "temperature": metadata.get("temperature"),
                "max_tokens": metadata.get("max_tokens"),
                "master_prompt": metadata.get("master_prompt_name"),
                "input_sections": metadata.get("input_sections"),
            },
        )
        draft.save()

        if error:
            draft.status = ResumeDraft.Status.ERROR
            draft.error_message = error
            draft.save(skip_version=True)
            messages.error(request, f"Regeneration failed: {error}")
        else:
            normalized = normalize_generated_resume(content, job, consultant_profile)
            draft.content = normalized
            draft.tokens_used = tokens
            errors, warnings = validate_resume(draft.content)
            draft.validation_errors = errors
            draft.validation_warnings = warnings
            draft.ats_score = score_ats(job.description, draft.content)
            draft.status = ResumeDraft.Status.REVIEW if errors else ResumeDraft.Status.DRAFT
            summ = dict(metadata.get("preflight", {}))
            summ["input_sections"] = metadata.get("input_sections", {})
            draft.llm_input_summary = summ
            draft.save(skip_version=True)
            messages.success(request, f"Resume draft v{draft.version} regenerated via Master Prompt.")

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
        master = MasterPrompt.get_active()
        system_prompt = existing.llm_system_prompt or (master.system_prompt if master else "")
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

        content, tokens, error = generate_section(system_prompt, user_prompt, actor=request.user)
        if error:
            messages.error(request, f"Section update failed: {error}")
            return redirect('draft-detail', pk=pk)

        new_section = content.strip()
        if heading not in new_section:
            new_section = f"{heading}\n{new_section}"

        updated_content = replace_section(base_content, heading, self.SECTION_HEADINGS, new_section)
        updated_content = normalize_generated_resume(updated_content, job, consultant)

        config = LLMConfig.load()
        draft = ResumeDraft(
            consultant=consultant,
            job=job,
            status=ResumeDraft.Status.PROCESSING,
            created_by=request.user,
            llm_system_prompt=system_prompt,
            llm_user_prompt=user_prompt,
            llm_request_payload={
                "model": config.active_model or "gpt-4o-mini",
                "temperature": float(config.temperature),
                "max_tokens": config.max_output_tokens,
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


# ─── Phase 6: Clean Resume Generation Flow ───────────────────────────

class ResumeGeneratePageView(AdminOrEmployeeMixin, BaseView):
    """
    GET: Show the clean generation page — select consultant + job,
    see pre-flight compatibility, then generate.
    """

    def get(self, request):
        from jobs.models import Job as JobModel
        consultant_id = request.GET.get('consultant')
        job_id = request.GET.get('job')

        consultants = ConsultantProfile.objects.select_related('user').filter(
            status__in=['ACTIVE', 'BENCH']
        ).order_by('user__first_name')
        jobs = JobModel.objects.filter(status='OPEN').order_by('-created_at')

        master = MasterPrompt.get_active()
        input_sections_defaults = merge_input_sections(master, None)

        context = {
            'consultants': consultants,
            'jobs': jobs,
            'selected_consultant_id': int(consultant_id) if consultant_id else None,
            'selected_job_id': int(job_id) if job_id else None,
            'input_sections_defaults': input_sections_defaults,
        }

        # If both selected, show pre-flight
        if consultant_id and job_id:
            try:
                cp = ConsultantProfile.objects.get(pk=consultant_id)
                job = JobModel.objects.get(pk=job_id)
                context['preflight'] = preflight_check(job, cp)
                context['consultant_obj'] = cp
                context['job_obj'] = job
            except (ConsultantProfile.DoesNotExist, JobModel.DoesNotExist):
                pass

        return render(request, 'resumes/generate_resume.html', context)


class ResumeGenerateActionView(AdminOrEmployeeMixin, BaseView):
    """
    POST: Generate a resume using the clean engine (single LLM call).
    Creates a ResumeDraft and redirects to the review page.
    """

    def post(self, request):
        from .engine import generate_resume, score_resume
        from jobs.models import Job as JobModel

        consultant_id = request.POST.get('consultant')
        job_id = request.POST.get('job')

        if not consultant_id or not job_id:
            messages.error(request, "Select both a consultant and a job.")
            return redirect('resume-generate')

        cp = get_object_or_404(ConsultantProfile, pk=consultant_id)
        job = get_object_or_404(JobModel, pk=job_id)

        post_sections = parse_input_sections_from_request(request)
        effective = merge_input_sections(MasterPrompt.get_active(), post_sections)
        v_err = validate_input_sections(effective)
        if v_err:
            messages.error(request, v_err)
            return redirect('resume-generate')

        # Create draft in PROCESSING
        draft = ResumeDraft(
            consultant=cp,
            job=job,
            status=ResumeDraft.Status.PROCESSING,
            created_by=request.user,
        )
        draft.save()

        content, tokens, error, metadata = generate_resume(
            job, cp, actor=request.user, input_sections=post_sections
        )

        if error:
            draft.status = ResumeDraft.Status.ERROR
            draft.error_message = error
            draft.llm_system_prompt = metadata.get('system_prompt', '')
            draft.llm_user_prompt = metadata.get('user_prompt', '')
            draft.save(skip_version=True)
            messages.error(request, f"Generation failed: {error}")
            return redirect('resume-generate')

        draft.content = content
        draft.tokens_used = tokens
        draft.ats_score = score_resume(job.description, content)
        draft.llm_system_prompt = metadata.get('system_prompt', '')
        draft.llm_user_prompt = metadata.get('user_prompt', '')
        draft.llm_request_payload = {
            'model': metadata.get('model'),
            'temperature': metadata.get('temperature'),
            'max_tokens': metadata.get('max_tokens'),
            'master_prompt': metadata.get('master_prompt_name'),
            'input_sections': metadata.get('input_sections'),
        }
        summ = dict(metadata.get('preflight', {}))
        summ['input_sections'] = metadata.get('input_sections', {})
        draft.llm_input_summary = summ
        draft.status = ResumeDraft.Status.DRAFT
        draft.save(skip_version=True)

        messages.success(
            request,
            f"Resume v{draft.version} generated for {cp.user.get_full_name()} — ATS score: {draft.ats_score}%"
        )
        return redirect('draft-review', pk=draft.pk)


class PreflightCheckView(AdminOrEmployeeMixin, BaseView):
    """HTMX endpoint: return pre-flight compatibility check HTML fragment."""

    def get(self, request):
        from jobs.models import Job as JobModel

        consultant_id = request.GET.get('consultant')
        job_id = request.GET.get('job')

        if not consultant_id or not job_id:
            return render(request, 'resumes/partials/preflight.html', {'preflight': None})

        try:
            from .engine import get_resume_location
            cp = ConsultantProfile.objects.get(pk=consultant_id)
            job = JobModel.objects.get(pk=job_id)
            pf = preflight_check(job, cp)
            resolved_location, location_source = get_resume_location(cp, job)
            return render(request, 'resumes/partials/preflight.html', {
                'preflight': pf,
                'consultant_obj': cp,
                'job_obj': job,
                'resolved_location': resolved_location,
                'location_source': location_source,
            })
        except (ConsultantProfile.DoesNotExist, JobModel.DoesNotExist):
            return render(request, 'resumes/partials/preflight.html', {'preflight': None})


class DraftReviewView(DraftAccessMixin, DetailView):
    """
    Clean review page for a generated draft.
    Shows content, ATS score, pre-flight info, download + promote actions.
    """
    model = ResumeDraft
    template_name = 'resumes/draft_review.html'
    context_object_name = 'draft'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        draft = context['draft']
        # All versions for this consultant+job (version history)
        context['all_versions'] = ResumeDraft.objects.filter(
            consultant=draft.consultant, job=draft.job
        ).order_by('-version')[:10]
        return context


# ─── Resume Template Editor Views ────────────────────────────────────────────

import json as _json
from django.db.models import Q as _Q
from django.http import JsonResponse
from django.utils.text import slugify
from .models import ResumeTemplate, ResumeEditorState
from .parser import parse_resume
from .export_utils import export_docx, export_pdf, export_pdf_html, render_resume_html


class ResumeEditorView(DraftAccessMixin, BaseView):
    """Open the split-pane template editor for a draft."""

    def get(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)
        self._draft = draft  # for DraftAccessMixin.get_object

        # Get or create editor state (parse draft on first open)
        state, created = ResumeEditorState.objects.get_or_create(
            draft=draft,
            defaults={'sections_json': parse_resume(draft.content or '')}
        )

        # If state exists but sections are empty (e.g. draft was regenerated), re-parse
        if not state.sections_json:
            state.sections_json = parse_resume(draft.content or '')
            state.save(update_fields=['sections_json'])

        # Template: use saved one, or default to first builtin
        template = state.template
        if template is None:
            template = ResumeTemplate.objects.filter(is_builtin=True).first()

        all_templates = list(ResumeTemplate.objects.filter(
            _Q(is_builtin=True) | _Q(created_by=request.user)
        ).order_by('-is_builtin', 'name'))

        return render(request, 'resumes/editor.html', {
            'draft': draft,
            'state': state,
            'sections_json': _json.dumps(state.sections_json),
            'template_config': _json.dumps(template.to_dict() if template else {}),
            'all_templates': all_templates,
            'all_templates_json': _json.dumps([t.to_dict() for t in all_templates]),
            'active_template': template,
            'font_choices': ResumeTemplate.FONT_CHOICES,
            'header_style_choices': ResumeTemplate.HEADER_STYLE_CHOICES,
            # Settings panel form helpers
            'font_size_fields': [
                ('name_size',    'Name Size',    'name_size'),
                ('header_size',  'Header Size',  'header_size'),
                ('body_size',    'Body Size',    'body_size'),
                ('contact_size', 'Contact Size', 'contact_size'),
            ],
            'margin_fields': [
                ('Top',    'margin_top'),
                ('Right',  'margin_right'),
                ('Bottom', 'margin_bottom'),
                ('Left',   'margin_left'),
            ],
        })

    def get_object(self):
        # DraftAccessMixin needs this
        return getattr(self, '_draft', None) or get_object_or_404(ResumeDraft, pk=self.kwargs['pk'])


class ResumeEditorSaveView(AdminOrEmployeeMixin, BaseView):
    """AJAX autosave — saves sections_json + optionally switches template."""

    def post(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)
        try:
            body = _json.loads(request.body)
        except Exception:
            return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

        state, _ = ResumeEditorState.objects.get_or_create(draft=draft)

        if 'sections' in body:
            state.sections_json = body['sections']

        if 'template_id' in body:
            tpl_id = body['template_id']
            if tpl_id:
                try:
                    tpl = ResumeTemplate.objects.get(pk=tpl_id)
                    state.template = tpl
                except ResumeTemplate.DoesNotExist:
                    pass
            else:
                state.template = None

        state.save()
        return JsonResponse({'ok': True, 'saved_at': state.updated_at.isoformat()})


class ResumeEditorPreviewView(AdminOrEmployeeMixin, BaseView):
    """Return rendered resume HTML fragment for the live preview (HTMX fallback)."""

    def post(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)
        try:
            body = _json.loads(request.body)
        except Exception:
            return HttpResponse('')

        sections = body.get('sections', {})
        tpl_cfg  = body.get('template', {})
        html = render_resume_html(sections, tpl_cfg, for_print=False)
        return HttpResponse(html)


class ResumeExportDOCXView(DraftAccessMixin, BaseView):
    """Download DOCX with current editor state + template."""

    def get(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)
        self._draft = draft

        state = getattr(draft, 'editor_state', None)
        sections = state.sections_json if state else parse_resume(draft.content or '')
        tpl = (state.template if state and state.template else
               ResumeTemplate.objects.filter(is_builtin=True).first())
        tpl_cfg = tpl.to_dict() if tpl else {}

        docx_bytes = export_docx(sections, tpl_cfg)
        consultant = draft.consultant.user.get_full_name() or draft.consultant.user.username
        job_title  = draft.job.title.replace(' ', '_')[:40]
        filename   = f"{consultant.replace(' ','_')}_{job_title}_v{draft.version}.docx"

        resp = HttpResponse(
            docx_bytes,
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )
        resp['Content-Disposition'] = f'attachment; filename="{filename}"'
        return resp

    def get_object(self):
        return getattr(self, '_draft', None) or get_object_or_404(ResumeDraft, pk=self.kwargs['pk'])


class ResumeExportPDFView(DraftAccessMixin, BaseView):
    """Download PDF with current editor state + template."""

    def get(self, request, pk):
        draft = get_object_or_404(ResumeDraft, pk=pk)
        self._draft = draft

        state    = getattr(draft, 'editor_state', None)
        sections = state.sections_json if state else parse_resume(draft.content or '')
        tpl      = (state.template if state and state.template else
                    ResumeTemplate.objects.filter(is_builtin=True).first())
        tpl_cfg  = tpl.to_dict() if tpl else {}

        consultant = draft.consultant.user.get_full_name() or draft.consultant.user.username
        job_title  = draft.job.title.replace(' ', '_')[:40]
        safe_name  = consultant.replace(' ', '_')

        try:
            pdf_bytes = export_pdf(sections, tpl_cfg)
            filename  = f"{safe_name}_{job_title}_v{draft.version}.pdf"
            resp = HttpResponse(pdf_bytes, content_type='application/pdf')
            resp['Content-Disposition'] = f'attachment; filename="{filename}"'
            return resp
        except ImportError:
            # No PDF library installed — serve print-ready HTML (browser prints to PDF)
            html = export_pdf_html(sections, tpl_cfg)
            filename = f"{safe_name}_{job_title}_v{draft.version}_print.html"
            resp = HttpResponse(html, content_type='text/html; charset=utf-8')
            resp['Content-Disposition'] = f'inline; filename="{filename}"'
            return resp
        except Exception as e:
            messages.error(request, f'PDF generation failed: {e}')
            return redirect('draft-review', pk=pk)

    def get_object(self):
        return getattr(self, '_draft', None) or get_object_or_404(ResumeDraft, pk=self.kwargs['pk'])


# ─── Template CRUD ────────────────────────────────────────────────────────────

class ResumeTemplateSaveView(AdminOrEmployeeMixin, BaseView):
    """Create or update a custom template (JSON API)."""

    def post(self, request):
        try:
            body = _json.loads(request.body)
        except Exception:
            return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

        tpl_id = body.get('id')
        if tpl_id:
            tpl = get_object_or_404(ResumeTemplate, pk=tpl_id, created_by=request.user)
        else:
            tpl = ResumeTemplate(created_by=request.user, is_builtin=False)

        name = (body.get('name') or '').strip()
        if not name:
            return JsonResponse({'ok': False, 'error': 'Template name is required'}, status=400)

        tpl.name = name
        # Generate unique slug
        base_slug = slugify(name)
        slug = base_slug
        qs = ResumeTemplate.objects.exclude(pk=tpl.pk if tpl.pk else None)
        i = 1
        while qs.filter(slug=slug).exists():
            slug = f'{base_slug}-{i}'
            i += 1
        tpl.slug = slug

        # Apply all editable fields
        fields = [
            'font_family', 'name_size', 'header_size', 'body_size', 'contact_size',
            'accent_color', 'name_color', 'body_color',
            'margin_top', 'margin_bottom', 'margin_left', 'margin_right',
            'line_height', 'para_spacing', 'section_spacing',
            'header_style', 'show_dividers', 'bullet_char',
        ]
        for f in fields:
            if f in body:
                setattr(tpl, f, body[f])

        tpl.save()
        return JsonResponse({'ok': True, 'template': tpl.to_dict()})


class ResumeTemplateDeleteView(AdminOrEmployeeMixin, BaseView):
    """Delete a user-created template."""

    def post(self, request, pk):
        tpl = get_object_or_404(ResumeTemplate, pk=pk, created_by=request.user, is_builtin=False)
        tpl.delete()
        return JsonResponse({'ok': True})


class ResumeTemplateListView(AdminOrEmployeeMixin, BaseView):
    """Return JSON list of templates visible to this user."""

    def get(self, request):
        qs = ResumeTemplate.objects.filter(
            _Q(is_builtin=True) | _Q(created_by=request.user)
        ).order_by('-is_builtin', 'name')
        return JsonResponse({'templates': [t.to_dict() for t in qs]})

"""
Resume Generation Pipeline V3 — Multi-Phase Architecture.

Public API:
    generate_resume_pipeline(job, consultant, actor=None, input_sections=None)

Returns: (content, total_tokens, error_msg, metadata)
    Same signature as engine.generate_resume() for drop-in replacement.
"""
import time
import logging

from django.utils import timezone

from .jd_intelligence import get_jd_intelligence
from .matching import build_compatibility_matrix
from .llm_client import PipelineLLMClient
from .generators.header import generate_header
from .generators.summary import generate_summary
from .generators.skills import generate_skills
from .generators.experience import generate_experience
from .generators.education import generate_education, generate_certifications
from .assembly import assemble_resume
from .quality_gate import run_quality_gate

logger = logging.getLogger("apps.resumes.pipeline")


def generate_resume_pipeline(job, consultant, actor=None, input_sections=None):
    """
    Multi-phase resume generation pipeline.

    Phases:
        1. JD Intelligence (deterministic, 0 tokens)
        2. Candidate-JD Matching (deterministic, 0 tokens)
        3. Section Generation (3 LLM calls: summary, skills, experience)
        4. Assembly + Validation (deterministic, 0 tokens)
        5. Quality Gate (0-2 retry LLM calls if needed)

    Returns: (content, total_tokens, error_msg, metadata)
        Same signature as engine.generate_resume() for drop-in compatibility.
    """
    pipeline_start = time.time()

    # Initialize LLM client
    llm = PipelineLLMClient()
    available, err_msg = llm.is_available()
    if not available:
        return None, 0, err_msg, {}

    cap_ok, cap_msg = llm.check_token_cap()
    if not cap_ok:
        return None, 0, cap_msg, {}

    # Load section prompts (if configured)
    section_prompts = _load_section_prompts()

    metadata = {
        "pipeline_version": "v3",
        "phases": {},
    }

    try:
        # ── Phase 1: JD Intelligence ────────────────────────────────────
        phase_start = time.time()
        jd_intel = get_jd_intelligence(job)
        jd_intel["_job_pk"] = job.pk  # for cache key in matching
        metadata["phases"]["jd_intelligence"] = {
            "source": jd_intel.get("source", "unknown"),
            "latency_ms": int((time.time() - phase_start) * 1000),
            "tokens": 0,
        }

        # ── Phase 2: Candidate-JD Matching ──────────────────────────────
        phase_start = time.time()
        matching = build_compatibility_matrix(consultant, jd_intel)
        metadata["phases"]["matching"] = {
            "match_pct": matching.get("match_pct", 0),
            "matched_required": len(matching.get("matched_required", [])),
            "missing_required": len(matching.get("missing_required", [])),
            "coaching_keywords": matching.get("coaching_keywords", [])[:8],
            "warnings": matching.get("warnings", []),
            "latency_ms": int((time.time() - phase_start) * 1000),
            "tokens": 0,
        }

        # ── Phase 3: Section Generation ─────────────────────────────────
        # 3e: Header (deterministic)
        header = generate_header(consultant, job)

        # 3d: Education + Certifications (deterministic)
        education_text = generate_education(consultant)
        certs_text = generate_certifications(consultant)

        # 3a: Summary (LLM)
        phase_start = time.time()
        summary_text, summary_tokens, summary_err = generate_summary(
            llm, jd_intel, matching, consultant,
            section_prompt=section_prompts.get("summary"),
            job=job, actor=actor,
        )
        metadata["phases"]["summary"] = {
            "tokens": summary_tokens,
            "latency_ms": int((time.time() - phase_start) * 1000),
            "error": summary_err,
        }
        if summary_err:
            logger.warning("Summary generation failed: %s", summary_err)
            summary_text = "Summary generation failed."

        # 3b: Skills (LLM or skills_extractor)
        phase_start = time.time()
        skills_text, skills_tokens, skills_err = generate_skills(
            llm, jd_intel, matching, consultant,
            section_prompt=section_prompts.get("skills"),
            job=job, actor=actor,
        )
        metadata["phases"]["skills"] = {
            "tokens": skills_tokens,
            "latency_ms": int((time.time() - phase_start) * 1000),
            "error": skills_err,
        }
        if skills_err:
            logger.warning("Skills generation failed: %s", skills_err)
            skills_text = "Skills generation failed."

        # 3c: Experience (LLM)
        phase_start = time.time()
        exp_text, exp_tokens, exp_err = generate_experience(
            llm, jd_intel, matching, consultant,
            section_prompt=section_prompts.get("experience"),
            job=job, actor=actor,
        )
        metadata["phases"]["experience"] = {
            "tokens": exp_tokens,
            "latency_ms": int((time.time() - phase_start) * 1000),
            "error": exp_err,
        }
        if exp_err:
            logger.warning("Experience generation failed: %s", exp_err)
            exp_text = "Experience generation failed."

        # ── Phase 4: Assembly ───────────────────────────────────────────
        phase_start = time.time()
        content, val_errors, val_warnings = assemble_resume(
            header=header,
            summary=summary_text,
            skills=skills_text,
            experience=exp_text,
            education=education_text,
            certifications=certs_text,
        )
        metadata["phases"]["assembly"] = {
            "validation_errors": val_errors,
            "validation_warnings": val_warnings,
            "latency_ms": int((time.time() - phase_start) * 1000),
        }

        # ── Phase 5: Quality Gate ───────────────────────────────────────
        phase_start = time.time()
        generators = {
            "summary": generate_summary,
            "skills": generate_skills,
            "experience": generate_experience,
        }
        qg_result = run_quality_gate(
            content=content,
            job=job,
            jd_intel=jd_intel,
            validation_errors=val_errors,
            validation_warnings=val_warnings,
            generators=generators,
            llm_client=llm,
            matching=matching,
            consultant=consultant,
            section_prompts=section_prompts,
            actor=actor,
        )
        metadata["phases"]["quality_gate"] = {
            "ats_score": qg_result["ats_score"],
            "passed": qg_result["passed"],
            "retried_sections": qg_result["retried_sections"],
            "final_errors": qg_result["validation_errors"],
            "retry_tokens": qg_result["retry_tokens"],
            "latency_ms": int((time.time() - phase_start) * 1000),
        }

        final_content = qg_result["final_content"]
        final_ats = qg_result["ats_score"]

        # ── Totals ──────────────────────────────────────────────────────
        total_latency = int((time.time() - pipeline_start) * 1000)
        metadata["totals"] = {
            "tokens": llm.total_tokens,
            "cost": float(llm.total_cost),
            "llm_calls": llm.total_calls,
            "latency_ms": total_latency,
            "ats_score": final_ats,
        }

        # Store for PipelineRun creation by caller
        metadata["jd_intelligence"] = {
            k: v for k, v in jd_intel.items()
            if k not in ("_job_pk", "raw_description")
        }
        metadata["matching_matrix"] = {
            k: v for k, v in matching.items()
            if k != "_job_pk"
        }
        metadata["input_sections"] = input_sections or {}

        logger.info(
            "Pipeline complete for consultant %s x job %s: "
            "ATS=%d, tokens=%d, calls=%d, latency=%dms, retries=%s",
            consultant.pk, job.pk, final_ats, llm.total_tokens,
            llm.total_calls, total_latency, qg_result["retried_sections"],
        )

        return final_content, llm.total_tokens, None, metadata

    except Exception as e:
        logger.exception("Pipeline failed for consultant %s x job %s: %s",
                        consultant.pk, job.pk, e)
        return None, llm.total_tokens, str(e), metadata


def _load_section_prompts():
    """Load section-specific prompts from the active MasterPrompt."""
    from resumes.models import MasterPrompt, SectionPrompt

    master = MasterPrompt.get_active()
    if not master:
        return {}

    prompts = {}
    for sp in SectionPrompt.objects.filter(master_prompt=master):
        prompts[sp.section_type] = sp

    return prompts

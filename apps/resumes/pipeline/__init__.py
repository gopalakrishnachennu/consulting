"""
Resume Generation Pipeline V3 — Single-Call with Structured Intelligence.

Architecture:
    Phase 1: JD Intelligence (deterministic, 0 tokens) — extracts structured data from parsed_jd
    Phase 2: Candidate-JD Matching (deterministic, 0 tokens) — builds compatibility matrix
    Phase 3: ONE focused LLM call with structured input → Full resume
    Phase 4: Validation + ATS scoring (deterministic, 0 tokens)

Public API:
    generate_resume_pipeline(job, consultant, actor=None, input_sections=None)

Returns: (content, total_tokens, error_msg, metadata)
    Same signature as engine.generate_resume() for drop-in replacement.
"""
import time
import logging

from .jd_intelligence import get_jd_intelligence
from .matching import build_compatibility_matrix
from .llm_client import PipelineLLMClient
from .generators.header import generate_header
from .generators.education import generate_education, generate_certifications
from .prompts import build_prompt, SYSTEM_MESSAGE
from .utils import validate_resume, score_ats

logger = logging.getLogger("apps.resumes.pipeline")


def generate_resume_pipeline(job, consultant, actor=None, input_sections=None):
    """
    Single-call resume generation with structured intelligence.

    Phase 1: JD Intelligence (0 tokens) — use parsed_jd, not raw text
    Phase 2: Matching (0 tokens) — know what skills match before prompting
    Phase 3: ONE LLM call with all structured data → full resume
    Phase 4: Validation + ATS (0 tokens)

    Returns: (content, total_tokens, error_msg, metadata)
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

    # Load MasterPrompt for system prompt + generation rules
    from resumes.models import MasterPrompt
    master_prompt = MasterPrompt.get_active()

    metadata = {
        "pipeline_version": "v3.1",
        "phases": {},
    }

    try:
        # ── Phase 1: JD Intelligence (0 tokens) ────────────────────────
        phase_start = time.time()
        jd_intel = get_jd_intelligence(job)
        jd_intel["_job_pk"] = job.pk
        metadata["phases"]["jd_intelligence"] = {
            "source": jd_intel.get("source", "unknown"),
            "required_skills_count": len(jd_intel.get("required_skills", [])),
            "latency_ms": int((time.time() - phase_start) * 1000),
        }

        # ── Phase 2: Candidate-JD Matching (0 tokens) ──────────────────
        phase_start = time.time()
        matching = build_compatibility_matrix(consultant, jd_intel)
        metadata["phases"]["matching"] = {
            "match_pct": matching.get("match_pct", 0),
            "matched_required": len(matching.get("matched_required", [])),
            "missing_required": len(matching.get("missing_required", [])),
            "coaching_keywords": matching.get("coaching_keywords", [])[:8],
            "warnings": matching.get("warnings", []),
            "latency_ms": int((time.time() - phase_start) * 1000),
        }

        # ── Deterministic sections (0 tokens) ──────────────────────────
        header = generate_header(consultant, job)
        education_text = generate_education(consultant)
        certs_text = generate_certifications(consultant)

        # ── Phase 3: ONE LLM call ──────────────────────────────────────
        phase_start = time.time()

        # Admin rules from MasterPrompt.generation_rules (the ONLY part admin writes)
        admin_rules = None
        if master_prompt and master_prompt.generation_rules:
            admin_rules = master_prompt.generation_rules

        # Code builds ALL data sections, admin rules go at the end
        full_prompt = build_prompt(
            jd_intel=jd_intel,
            matching=matching,
            consultant=consultant,
            header=header,
            education=education_text,
            certifications=certs_text,
            admin_rules=admin_rules,
        )

        content, tokens, error = llm.call(
            SYSTEM_MESSAGE,
            full_prompt,
            request_type="pipeline_v3_generate",
            temperature=0.6,
            max_tokens=4000,
            job=job,
            consultant=consultant,
            actor=actor,
        )

        metadata["phases"]["generation"] = {
            "tokens": tokens,
            "latency_ms": int((time.time() - phase_start) * 1000),
            "error": error,
        }

        if error:
            return None, tokens, error, metadata

        # Clean LLM output artifacts
        content = _clean_llm_output(content or "")

        # ── Phase 4: Validation + ATS (0 tokens) ──────────────────────
        phase_start = time.time()
        val_errors, val_warnings = validate_resume(content)

        # ── Phase 4b: Deterministic truth guardrails (model-agnostic) ──
        # Checks the output against the candidate's REAL data so fabrication is
        # caught regardless of which model wrote it. Enabled unless flag is off.
        review_status = "pass"
        try:
            from core.models import FeatureFlag
            _ff = FeatureFlag.objects.filter(key="resume_guardrails").first()
            guardrails_on = _ff.is_enabled if _ff else True
        except Exception:
            guardrails_on = True
        if guardrails_on:
            try:
                from .guardrails import run_guardrails
                guard = run_guardrails(content, consultant, jd_intel)
                val_errors = list(val_errors) + guard["errors"]
                val_warnings = list(val_warnings) + guard["warnings"]
                review_status = guard["status"]
                metadata["guardrails"] = guard
            except Exception as ge:
                logger.exception("Guardrails failed (non-fatal): %s", ge)
        metadata["review_status"] = review_status
        metadata["validation_errors"] = val_errors
        metadata["validation_warnings"] = val_warnings

        # ATS score using structured keywords
        ats_keywords = jd_intel.get("keywords_for_ats", [])
        if ats_keywords:
            ats = score_ats(" ".join(ats_keywords), content or "")
        else:
            ats = score_ats(jd_intel.get("raw_description", ""), content or "")

        metadata["phases"]["validation"] = {
            "ats_score": ats,
            "validation_errors": val_errors,
            "validation_warnings": val_warnings,
            "latency_ms": int((time.time() - phase_start) * 1000),
        }

        # ── Totals ─────────────────────────────────────────────────────
        total_latency = int((time.time() - pipeline_start) * 1000)
        metadata["totals"] = {
            "tokens": llm.total_tokens,
            "cost": float(llm.total_cost),
            "llm_calls": llm.total_calls,
            "latency_ms": total_latency,
            "ats_score": ats,
        }
        metadata["jd_intelligence"] = {
            k: v for k, v in jd_intel.items()
            if k not in ("_job_pk", "raw_description")
        }
        metadata["matching_matrix"] = {
            k: v for k, v in matching.items()
            if k != "_job_pk"
        }
        metadata["system_prompt"] = SYSTEM_MESSAGE
        metadata["user_prompt"] = full_prompt

        logger.info(
            "Pipeline V3.1 complete: consultant %s x job %s — "
            "ATS=%d, tokens=%d, cost=$%.4f, latency=%dms",
            consultant.pk, job.pk, ats, llm.total_tokens,
            llm.total_cost, total_latency,
        )

        return content, llm.total_tokens, None, metadata

    except Exception as e:
        logger.exception("Pipeline failed: consultant %s x job %s: %s",
                        consultant.pk, job.pk, e)
        return None, llm.total_tokens, str(e), metadata



# _load_section_prompts removed — pipeline now reads directly from MasterPrompt


def _clean_llm_output(text):
    """Strip markdown fences, bold markers, separator lines, and other LLM artifacts."""
    import re

    # Remove markdown code fences
    text = re.sub(r'^```\w*\s*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)

    # Strip leading LLM meta-commentary (e.g. "NOTES: this JD has limited overlap...")
    # before the resume actually starts. The model sometimes prepends a strategy note
    # when the skill match is low. Drop a leading NOTE/NOTES block up to the first
    # blank line, plus common "Here is ..." preambles.
    text = text.lstrip()
    text = re.sub(r'^(NOTES?|DISCLAIMER)\s*:.*?(\n\s*\n|\Z)', '', text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'^(Here(\'s| is)|Sure|Certainly|Below is)\b.*?(\n\s*\n|\Z)', '', text,
                  flags=re.IGNORECASE | re.DOTALL)

    # KEEP **bold** — the resume renderer (preview, PDF, DOCX) now styles it for
    # keyword emphasis. Only strip lone *italic* markers (the renderer ignores those),
    # without touching the double asterisks of **bold**.
    text = re.sub(r'(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)', r'\1', text)

    # Remove separator lines (====, ----) — but not bold; require 4+ of = or - only
    text = re.sub(r'^[=\-]{4,}\s*$', '', text, flags=re.MULTILINE)

    # Remove trailing whitespace per line
    text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)

    # Collapse 3+ blank lines into 2
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()

"""
LLM JD Extraction Engine (Resume Engine V4 — P1a).

extract_jd(job) is the public entry point. It:
  1. normalizes the JD text and computes a sha256 content hash
  2. reuses the cached parsed_jd when hash + schema/prompt/model all match
  3. otherwise calls the LLM (central LLMConfig → DeepSeek/OpenRouter capable),
     parses strict JSON, validates (VAL_001..010), and repairs once on failure
  4. falls back to the legacy rule parser only if the LLM path fails
  5. stores the rich parsed_jd + status + version metadata on the Job

Everything is gated by the `resume_engine_v4` feature flag; when off, callers keep
using the legacy rule parser unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from django.utils import timezone

from . import jd_extractor_schemas as S

logger = logging.getLogger("apps.resumes.pipeline.jd_extractor")


# ── feature flags ────────────────────────────────────────────────────────────
def _flag(key: str, default: bool) -> bool:
    try:
        from core.models import FeatureFlag
        ff = FeatureFlag.objects.filter(key=key).first()
        return ff.is_enabled if ff is not None else default
    except Exception:
        return default


def v4_enabled() -> bool:
    return _flag("resume_engine_v4", False)


# ── normalization + hashing ──────────────────────────────────────────────────
def normalize_jd_text(text: str) -> str:
    """Stable normalization for hashing — preserves meaning, only tidies whitespace."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def jd_hash(text: str) -> str:
    norm = normalize_jd_text(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ── cache ────────────────────────────────────────────────────────────────────
def _cache_valid(job, current_hash: str, model: str) -> bool:
    if (job.parsed_jd_status or "") not in (
        S.STATUS_OK_LLM, S.STATUS_OK_LLM_WARN, S.STATUS_NEEDS_REVIEW,
    ):
        return False
    if (getattr(job, "parsed_jd_hash", "") or "") != current_hash:
        return False
    if (getattr(job, "parsed_jd_schema_version", "") or "") != S.SCHEMA_VERSION:
        return False
    if (getattr(job, "parsed_jd_prompt_version", "") or "") != S.PROMPT_VERSION:
        return False
    # model is allowed to differ (a re-run on a cheaper model is fine), so not checked
    return bool(job.parsed_jd)


# ── LLM JSON helpers ─────────────────────────────────────────────────────────
def _strip_json(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_json(text: str):
    try:
        return json.loads(_strip_json(text))
    except Exception:
        # Best-effort: grab the outermost {...}
        m = re.search(r"\{.*\}", _strip_json(text), re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None


def _call_llm(llm, jd_text: str, repair_feedback: str | None = None):
    """One LLM extraction call. Returns (data_or_None, model, error)."""
    user = f"JOB DESCRIPTION:\n{jd_text[:12000]}"
    if repair_feedback:
        user += (
            "\n\nYOUR PREVIOUS OUTPUT FAILED VALIDATION:\n"
            f"{repair_feedback}\n"
            "Return corrected STRICT JSON only — fix those issues, keep everything else."
        )
    # Prefer the cheap validation model (DeepSeek/etc.) for extraction.
    model = (getattr(llm.config, "validation_model", "") or "").strip() or llm.default_model
    content, _tokens, error = llm.call(
        S.EXTRACTOR_SYSTEM_PROMPT, user,
        request_type="jd_extract_v4", model=model, temperature=0.1, max_tokens=4000,
    )
    if error:
        return None, model, error
    data = _parse_json(content or "")
    if data is None:
        return None, model, "json_parse_failed"
    return data, model, None


# ── public entry point ───────────────────────────────────────────────────────
def extract_jd(job, *, force: bool = False, save: bool = True) -> dict:
    """Extract a rich parsed_jd for `job`. Returns the parsed_jd dict (with
    parser_metadata attached). Caches by content hash; falls back to the legacy
    rule parser on failure. Never raises."""
    jd_text = (job.description or "").strip()
    current_hash = jd_hash(jd_text)

    # 1. cache
    if not force and _cache_valid(job, current_hash, ""):
        return job.parsed_jd

    if not jd_text:
        return _fallback(job, current_hash, reason="empty_jd", save=save)

    # 2. LLM available?
    try:
        from .llm_client import PipelineLLMClient
        llm = PipelineLLMClient()
        available, _msg = llm.is_available()
    except Exception as exc:
        logger.warning("jd_extractor: LLM client init failed: %s", exc)
        available = False
    if not available:
        return _fallback(job, current_hash, reason="llm_unavailable", save=save)

    # 3. extract + validate (+ one repair retry)
    data, model, error = _call_llm(llm, jd_text)
    status = S.STATUS_OK_LLM
    warnings: list[str] = []

    if data is not None:
        v = S.validate_parsed_jd(data)
        if not v["ok"] and v["needs_retry"]:
            feedback = "; ".join(v["errors"])[:1000]
            data2, model, error = _call_llm(llm, jd_text, repair_feedback=feedback)
            if data2 is not None:
                data = data2
                v = S.validate_parsed_jd(data)
        warnings = list(v.get("warnings", []))
        if not v["ok"]:
            # validation still failing after retry → fall back
            logger.info("jd_extractor: validation failed for job %s: %s", job.pk, v["errors"])
            return _fallback(job, current_hash, reason="validation_failed", save=save)
        if warnings:
            status = S.STATUS_OK_LLM_WARN
    else:
        logger.info("jd_extractor: LLM error for job %s: %s", job.pk, error)
        return _fallback(job, current_hash, reason=(error or "llm_error"), save=save)

    # needs_human_review escalation
    eq = data.get("extraction_quality") or {}
    if eq.get("needs_human_review"):
        status = S.STATUS_NEEDS_REVIEW

    # 4. attach metadata (VAL_010) + persist
    data["parser_metadata"] = {
        "extractor_model": model,
        "extractor_prompt_version": S.PROMPT_VERSION,
        "extractor_schema_version": S.SCHEMA_VERSION,
        "jd_text_hash": current_hash,
        "created_at": timezone.now().isoformat(),
        "status": status,
        "warnings": warnings,
    }
    if save:
        _store(job, data, status, current_hash, model)
    return data


def _fallback(job, current_hash: str, *, reason: str, save: bool) -> dict:
    """Legacy rule parser fallback (when allowed) — keeps the system working."""
    allow = _flag("allow_legacy_parser_fallback", True)
    data: dict = {}
    if allow:
        try:
            from jobs.services import rule_parse_jd
            data = rule_parse_jd(job.description or "") or {}
        except Exception as exc:
            logger.warning("jd_extractor: legacy fallback failed for job %s: %s", job.pk, exc)
            data = {}
    status = S.STATUS_RULES_FALLBACK if (allow and data) else S.STATUS_FAILED
    data = dict(data)
    data["parser_metadata"] = {
        "extractor_model": "rule_parse_jd" if data else "",
        "extractor_prompt_version": "legacy",
        "extractor_schema_version": "legacy",
        "jd_text_hash": current_hash,
        "created_at": timezone.now().isoformat(),
        "status": status,
        "fallback_reason": reason,
    }
    if save:
        _store(job, data, status, current_hash, "rule_parse_jd")
    return data


def _store(job, data: dict, status: str, current_hash: str, model: str) -> None:
    job.parsed_jd = data
    job.parsed_jd_status = status[:20]
    job.parsed_jd_error = "" if status not in (S.STATUS_FAILED,) else "extraction_failed"
    job.parsed_jd_updated_at = timezone.now()
    fields = ["parsed_jd", "parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"]
    # version metadata (added in migration)
    for attr, val in (
        ("parsed_jd_hash", current_hash),
        ("parsed_jd_model", (model or "")[:100]),
        ("parsed_jd_prompt_version", S.PROMPT_VERSION),
        ("parsed_jd_schema_version", S.SCHEMA_VERSION),
    ):
        if hasattr(job, attr):
            setattr(job, attr, val)
            fields.append(attr)
    try:
        job.save(update_fields=fields)
    except Exception as exc:
        logger.warning("jd_extractor: store failed for job %s: %s", job.pk, exc)

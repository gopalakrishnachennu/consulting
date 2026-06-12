"""
Tier-2 JD Content Gate — LLM-based relevance filter for AMBIGUOUS jobs.

Pipeline position:
  Tier-1 title gate (classify_title_v2) → AMBIGUOUS jobs → THIS MODULE
  → CONFIRMED jobs → full JD backfill queue
  → REJECTED jobs  → stored cheaply, done
  → UNCERTAIN jobs → human review queue (tiny fraction)

How it works:
  1. Load a batch of AMBIGUOUS RawJobs (jd_gate_decision=PENDING or NULL + title_gate=AMBIGUOUS).
  2. For each job:
     a. If the platform has description in the LIST response → use list_payload_json snippet (free).
     b. Otherwise → call harvester.fetch_job_snippet(url) to get first N chars.
  3. Batch into groups of `batch_size` and call gate_jobs_batch() (LLM binary YES/NO).
  4. Update RawJob: jd_gate_decision, jd_gate_confidence, jd_gate_reason, jd_gate_snippet.
  5. Route: CONFIRMED (YES) → queue for full JD backfill.
            REJECTED  (NO)  → mark done (no further processing).
            UNCERTAIN       → mark for human review.

Audit mode (HarvestEngineConfig.jd_gate_audit_mode=True):
  Gate runs and records decisions but does NOT suppress anything.
  All jobs proceed as if CONFIRMED. Use this for 2-4 weeks before enforcement.

Usage:
  from harvest.content_gate import run_content_gate
  results = run_content_gate(batch_size=100, dry_run=False)
"""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Decision constants ─────────────────────────────────────────────────────────
GATE_CONFIRMED  = "CONFIRMED"   # LLM said YES → proceed to full JD backfill
GATE_REJECTED   = "REJECTED"    # LLM said NO  → store minimal row, done
GATE_UNCERTAIN  = "UNCERTAIN"   # LLM confidence too low → human review
GATE_SKIPPED    = "SKIPPED"     # gate disabled / audit mode / no API key
GATE_PENDING    = "PENDING"     # queued but not yet processed


@dataclass
class GateRunResult:
    """Summary of a content gate run."""
    total_processed: int = 0
    confirmed: int = 0
    rejected: int = 0
    uncertain: int = 0
    skipped: int = 0
    errors: int = 0
    snippet_from_list: int = 0    # no extra HTTP call needed (free)
    snippet_from_detail: int = 0  # required a detail fetch
    audit_mode: bool = False
    dry_run: bool = False
    duration_seconds: float = 0.0
    model: str = ""
    errors_detail: list[str] = field(default_factory=list)


def _strip_html_to_text(raw: str) -> str:
    """Strip HTML tags and decode entities to plain text."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_snippet_from_list_payload(list_payload: dict | None, max_chars: int = 800) -> str:
    """
    Extract a description snippet from the LIST endpoint payload.

    Tries common field names used by Lever, Ashby, Greenhouse, Workable.
    Returns empty string if no usable text found.
    """
    if not list_payload:
        return ""

    candidates = [
        list_payload.get("description") or "",
        list_payload.get("content") or "",
        list_payload.get("descriptionPlain") or "",
        list_payload.get("descriptionBody") or "",
        list_payload.get("body") or "",
        list_payload.get("jobDescription") or "",
        # Greenhouse: description is nested
        (list_payload.get("metadata") or {}).get("description") or "",
    ]

    for raw in candidates:
        if not raw:
            continue
        text = _strip_html_to_text(str(raw))
        if len(text) >= 50:  # need at least 50 chars to be useful
            return text[:max_chars]

    return ""


def _get_snippet_for_job(
    raw_job,
    harvester,
    max_chars: int = 800,
) -> tuple[str, str]:
    """
    Return (snippet_text, source) for a RawJob.

    source is "list" (free) or "detail" (required HTTP call).

    Priority:
      1. If platform has list_has_description=True and list_payload_json exists → extract free.
      2. If job already has a description (e.g. previously backfilled) → use it.
      3. Otherwise → call harvester.fetch_job_snippet(url).
    """
    # ── Option 1: free from list payload ────────────────────────────────────
    platform = None
    try:
        label = getattr(raw_job.company, "platform_label", None)
        platform = label.platform if label else None
    except Exception:
        pass

    if platform and getattr(platform, "list_has_description", False):
        snippet = _extract_snippet_from_list_payload(
            raw_job.list_payload_json, max_chars=max_chars
        )
        if snippet:
            return snippet, "list"

    # ── Option 2: already has description ───────────────────────────────────
    existing_desc = (getattr(raw_job, "description_clean", "") or
                     getattr(raw_job, "description", "") or "")
    if existing_desc and len(existing_desc.strip()) >= 50:
        text = _strip_html_to_text(existing_desc)[:max_chars]
        return text, "existing"

    # ── Option 3: fetch from detail endpoint ─────────────────────────────────
    if harvester and raw_job.original_url:
        try:
            snippet = harvester.fetch_job_snippet(
                raw_job.original_url, max_chars=max_chars
            )
            if snippet:
                return snippet, "detail"
        except Exception as exc:
            # Full traceback — a broken harvester here silently degrades gate
            # decisions (empty snippet → wrong REJECTs), so the cause must be visible.
            logger.warning(
                "content_gate: fetch_job_snippet failed for RawJob %s: %s",
                raw_job.pk, exc, exc_info=True,
            )

    return "", "none"


def _get_harvester_for_job(raw_job):
    """Return the platform harvester for a RawJob, or None if unavailable."""
    try:
        from .harvesters import get_harvester
        platform_slug = raw_job.platform_slug or ""
        return get_harvester(platform_slug)
    except Exception:
        return None


def _load_pending_jobs(batch_size: int, scope: str = "ambiguous_only"):
    """
    Load RawJobs that need JD gate evaluation.

    scope:
      "ambiguous_only"  → only jobs with title_gate_decision=AMBIGUOUS
      "all_possible"    → AMBIGUOUS + COLD-with-tech-signal (filter_decision=POSSIBLE)
      "all_non_hard_no" → any job without filter_decision=NO_MATCH and not is_cold
    """
    from .models import RawJob

    qs = RawJob.objects.filter(
        is_active=True,
        is_test_run=False,
    ).exclude(
        jd_gate_decision__in=[GATE_CONFIRMED, GATE_REJECTED],
    ).select_related("company__platform_label__platform")

    if scope == "ambiguous_only":
        qs = qs.filter(title_gate_decision=GATE_PENDING)
        # Also pick up older jobs that have POSSIBLE filter_decision but no title_gate set
        from django.db.models import Q
        qs = RawJob.objects.filter(
            is_active=True,
            is_test_run=False,
        ).filter(
            Q(title_gate_decision=GATE_PENDING) |
            Q(title_gate_decision__isnull=True, filter_decision="POSSIBLE")
        ).exclude(
            jd_gate_decision__in=[GATE_CONFIRMED, GATE_REJECTED, GATE_UNCERTAIN],
        ).select_related("company__platform_label__platform")

    elif scope == "all_possible":
        from django.db.models import Q
        qs = qs.filter(
            Q(title_gate_decision=GATE_PENDING) |
            Q(title_gate_decision__isnull=True, filter_decision__in=["POSSIBLE", "STRONG"])
        )
    else:  # all_non_hard_no
        qs = qs.exclude(
            filter_decision="NO_MATCH"
        ).exclude(is_cold=True)

    return qs.order_by("-fetched_at")[:batch_size]


def run_content_gate(
    *,
    batch_size: int = 100,
    dry_run: bool = False,
    scope: str | None = None,
    model: str | None = None,
    confidence_threshold: float | None = None,
    snippet_chars: int | None = None,
    audit_mode: bool | None = None,
    trigger_backfill_on_confirm: bool = True,
) -> GateRunResult:
    """
    Main entry point for the Tier-2 JD content gate.

    Args:
        batch_size: max jobs to process per call.
        dry_run: if True, evaluate but do NOT write any DB changes or queue backfill.
        scope: override HarvestEngineConfig.jd_gate_scope.
        model: override HarvestEngineConfig.jd_gate_model.
        confidence_threshold: override HarvestEngineConfig.jd_gate_confidence_threshold.
        snippet_chars: override HarvestEngineConfig.jd_gate_snippet_chars.
        audit_mode: override HarvestEngineConfig.jd_gate_audit_mode.
        trigger_backfill_on_confirm: queue CONFIRMED jobs for JD backfill automatically.

    Returns:
        GateRunResult summary.
    """
    from .models import HarvestEngineConfig
    from .llm_classifier import gate_jobs_batch

    t_start = time.monotonic()
    result = GateRunResult(dry_run=dry_run)

    # ── Load config ───────────────────────────────────────────────────────────
    try:
        cfg = HarvestEngineConfig.get()
    except Exception:
        cfg = None

    _model              = model              or (cfg.jd_gate_model              if cfg else "gpt-4o-mini")
    _threshold          = confidence_threshold or (cfg.jd_gate_confidence_threshold if cfg else 0.65)
    _snippet_chars      = snippet_chars      or (cfg.jd_gate_snippet_chars      if cfg else 800)
    _scope              = scope              or (cfg.jd_gate_scope              if cfg else "ambiguous_only")
    _audit_mode         = audit_mode if audit_mode is not None else (cfg.jd_gate_audit_mode if cfg else True)
    _gate_batch_size    = cfg.jd_gate_batch_size if cfg else 20

    result.audit_mode = _audit_mode
    result.model = _model

    logger.info(
        "content_gate: starting — batch=%d scope=%s model=%s threshold=%.2f audit=%s dry_run=%s",
        batch_size, _scope, _model, _threshold, _audit_mode, dry_run,
    )

    # ── Load jobs ─────────────────────────────────────────────────────────────
    jobs = list(_load_pending_jobs(batch_size, scope=_scope))
    if not jobs:
        logger.info("content_gate: no AMBIGUOUS jobs to process")
        return result

    logger.info("content_gate: loaded %d jobs to evaluate", len(jobs))

    # ── Build snippets for each job ───────────────────────────────────────────
    job_inputs: list[dict] = []
    snippet_map: dict[int, str] = {}

    for raw_job in jobs:
        harvester = _get_harvester_for_job(raw_job)
        snippet, source = _get_snippet_for_job(raw_job, harvester, max_chars=_snippet_chars)

        if source == "list":
            result.snippet_from_list += 1
        elif source in ("detail", "existing"):
            result.snippet_from_detail += 1

        snippet_map[raw_job.pk] = snippet

        job_inputs.append({
            "id":         raw_job.pk,
            "title":      raw_job.title or "",
            "company":    str(raw_job.company) if raw_job.company else "",
            "department": raw_job.department or "",
            "snippet":    snippet,
        })

    # ── Call LLM gate in batches ──────────────────────────────────────────────
    gate_results: dict[int, dict[str, Any]] = {}
    for i in range(0, len(job_inputs), _gate_batch_size):
        chunk = job_inputs[i : i + _gate_batch_size]
        logger.info(
            "content_gate: calling LLM gate batch %d–%d of %d",
            i + 1, min(i + _gate_batch_size, len(job_inputs)), len(job_inputs),
        )
        try:
            batch_result = gate_jobs_batch(
                chunk,
                model=_model,
                confidence_threshold=_threshold,
            )
            gate_results.update(batch_result)
        except Exception as exc:
            logger.error("content_gate: gate_jobs_batch failed for chunk %d: %s", i, exc)
            result.errors += len(chunk)
            result.errors_detail.append(str(exc)[:200])

    # ── Apply decisions to RawJobs ────────────────────────────────────────────
    confirmed_ids: list[int] = []
    now = timezone.now()

    for raw_job in jobs:
        pk = raw_job.pk
        gate = gate_results.get(pk)
        result.total_processed += 1

        if gate is None:
            # LLM returned no result for this job — treat as UNCERTAIN
            gate = {
                "decision":   GATE_UNCERTAIN,
                "confidence": 0.0,
                "category":   "",
                "reason":     "LLM returned no result for this job",
            }

        llm_decision  = gate["decision"]   # YES | NO | UNCERTAIN
        llm_conf      = gate["confidence"]
        llm_category  = gate.get("category", "")
        llm_reason    = gate.get("reason", "")
        snippet_text  = snippet_map.get(pk, "")

        # Map LLM decision to gate decision constant
        if llm_decision == "YES":
            gate_decision = GATE_CONFIRMED
            result.confirmed += 1
            if not _audit_mode:
                confirmed_ids.append(pk)
        elif llm_decision == "NO":
            gate_decision = GATE_REJECTED
            result.rejected += 1
        else:
            gate_decision = GATE_UNCERTAIN
            result.uncertain += 1

        # In audit mode: record the gate decision but don't change routing
        if _audit_mode:
            gate_decision_to_store = gate_decision  # store real decision for analysis
        else:
            gate_decision_to_store = gate_decision

        if not dry_run:
            try:
                update_fields = {
                    "jd_gate_decision":   gate_decision_to_store,
                    "jd_gate_confidence": llm_conf,
                    "jd_gate_reason":     llm_reason,
                    "jd_gate_snippet":    snippet_text[:800],
                    "jd_gate_model":      _model,
                    "jd_gate_category":   llm_category,
                    "jd_gate_ran_at":     now,
                }
                # In audit mode, don't change filter_decision or is_cold —
                # let existing pipeline behavior proceed unchanged.
                if not _audit_mode and gate_decision == GATE_REJECTED:
                    update_fields["is_cold"] = True
                    update_fields["jd_fetch_skipped"] = True
                    update_fields["filter_decision"] = "NO_MATCH"

                type(raw_job).objects.filter(pk=pk).update(**update_fields)
            except Exception as exc:
                logger.error("content_gate: failed to update RawJob %s: %s", pk, exc)
                result.errors += 1
                result.errors_detail.append(f"RawJob {pk}: {exc!s:.100}")

    # ── Queue confirmed jobs for JD backfill ──────────────────────────────────
    if trigger_backfill_on_confirm and confirmed_ids and not dry_run and not _audit_mode:
        try:
            from .tasks import backfill_single_job_task
            for job_pk in confirmed_ids:
                backfill_single_job_task.apply_async(
                    args=[job_pk],
                    queue="harvest",
                    countdown=1,
                )
            logger.info("content_gate: queued %d CONFIRMED jobs for JD backfill", len(confirmed_ids))
        except Exception as exc:
            logger.warning(
                "content_gate: failed to queue backfill for confirmed jobs: %s", exc
            )

    result.duration_seconds = time.monotonic() - t_start

    logger.info(
        "content_gate: done in %.1fs — "
        "total=%d confirmed=%d rejected=%d uncertain=%d skipped=%d errors=%d "
        "(list_snippets=%d detail_snippets=%d) audit=%s",
        result.duration_seconds,
        result.total_processed,
        result.confirmed,
        result.rejected,
        result.uncertain,
        result.skipped,
        result.errors,
        result.snippet_from_list,
        result.snippet_from_detail,
        result.audit_mode,
    )
    return result

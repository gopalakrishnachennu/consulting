"""Shared selective-intake enforcement — STRONG phrase matches only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from django.db.models import Q

from .role_filter import STRONG, ClassifyResult

if TYPE_CHECKING:
    from .models import HarvestEngineConfig

# Phrase-matched intake rows are treated as classified for pipeline gating/UI.
STRONG_INTAKE_CONFIDENCE = 0.92


def is_strong_intake(raw_job) -> bool:
    return (getattr(raw_job, "filter_decision", "") or "").strip().upper() == STRONG


def jd_backfill_eligibility_q() -> Q:
    """
    RawJobs that should receive JD backfill.

    Priority targets always qualify. STRONG intake rows qualify even when country
    scope left is_priority=False (unknown / no location) — JD text is needed to
    resolve country and resume gates before vetting.
    """
    return Q(is_priority=True) | Q(filter_decision=STRONG)


def job_qualifies_for_jd_backfill(job) -> bool:
    return bool(getattr(job, "is_priority", False) or is_strong_intake(job))


def effective_pipeline_confidence(raw_job) -> float | None:
    """
    Confidence used for READY/sync/resume-gate decisions.

    STRONG intake (Role Category phrase match) bypasses legacy domain-regex
    category_confidence — the intake rule is the source of truth.
    """
    if is_strong_intake(raw_job):
        return STRONG_INTAKE_CONFIDENCE

    snapshot = getattr(raw_job, "classification_snapshot", None)
    if snapshot and getattr(snapshot, "final_confidence", None):
        return float(snapshot.final_confidence)

    for attr in ("category_confidence", "classification_confidence"):
        val = getattr(raw_job, attr, None)
        if val is not None:
            return float(val)
    return None


def apply_strong_intake_confidence_fields(defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Persist intake confidence on upsert when a STRONG phrase match is stored."""
    merged = dict(defaults)
    decision = (merged.get("filter_decision") or "").strip().upper()
    if decision != STRONG:
        return merged
    current = merged.get("category_confidence")
    if current is None or float(current) < STRONG_INTAKE_CONFIDENCE:
        merged["category_confidence"] = STRONG_INTAKE_CONFIDENCE
    return merged


def ensure_strong_intake_confidence_on_job(job) -> bool:
    """Backfill category_confidence on in-memory RawJob rows after enrichment."""
    if not is_strong_intake(job):
        return False
    current = getattr(job, "category_confidence", None)
    if current is not None and float(current) >= STRONG_INTAKE_CONFIDENCE:
        return False
    job.category_confidence = STRONG_INTAKE_CONFIDENCE
    return True


def selective_enforcement_active(cfg: HarvestEngineConfig, *, fetch_all: bool = False) -> bool:
    """True when intake rules should drop rows before they reach RawJob."""
    if not getattr(cfg, "selective_filter_enabled", False):
        return False
    if fetch_all and not getattr(cfg, "filter_full_crawl", False):
        return False
    return True


def should_pre_storage_drop(
    cfg: HarvestEngineConfig,
    filter_result: ClassifyResult | None,
    *,
    title: str,
    enforcement_active: bool,
) -> bool:
    """
    Return True when a harvested job must NOT be written to RawJob.

    Selective intake is STRONG-only: the title must match an include phrase from
    Role Categories. COLD / POSSIBLE / NO_MATCH / UNKNOWN are never stored.
    """
    if not enforcement_active or filter_result is None:
        return False

    if not (title or "").strip():
        return True

    return filter_result.decision != STRONG


def ensure_selective_intake_enabled() -> bool:
    """
    Turn on selective intake enforcement when role categories exist.
    Returns True when config was changed.
    """
    from .models import HarvestEngineConfig, HarvestRoleCategory

    has_active_rules = any(
        bool(cat.include_phrases)
        for cat in HarvestRoleCategory.objects.filter(is_active=True).only("include_phrases")
    )
    if not has_active_rules:
        return False

    cfg = HarvestEngineConfig.get()
    if cfg.selective_filter_enabled:
        return False

    cfg.selective_filter_enabled = True
    cfg.save()
    return True

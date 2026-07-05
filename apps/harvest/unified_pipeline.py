"""
Single operational pipeline for Raw Jobs (post selective intake).

Matches the live harvest flow:
  List harvest → STRONG saved → JD backfill → scope/domain enrich → vet queue
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Q

from harvest.models import RawJob
from harvest.role_filter import STRONG
from harvest.selective_intake import is_strong_intake
from harvest.services.rawjob_query import DUPLICATE_SKIP_REASONS, duplicate_rawjob_q, filtered_out_q, ready_stage_q

UNIFIED_STEPS = (
    "INTAKE",
    "JD_BACKFILL",
    "ENRICH",
    "READY",
    "VET_QUEUE",
)
UNIFIED_TERMINAL = (
    "FILTERED_OUT",
    "DUPLICATE",
    "FAILED",
    "INACTIVE",
    "TEST",
)

_STEP_LABELS = {
    "INTAKE": "Intake",
    "JD_BACKFILL": "JD backfill",
    "ENRICH": "Enrich",
    "READY": "Ready",
    "VET_QUEUE": "Vet queue",
    "FILTERED_OUT": "Filtered out",
    "DUPLICATE": "Duplicate",
    "FAILED": "Failed",
    "INACTIVE": "Inactive",
    "TEST": "Test",
}


def _min_jd_words() -> int:
    return max(1, int(getattr(settings, "RESUME_JD_MIN_WORDS", 80)))


def _needs_enrichment(raw_job) -> bool:
    scope = (getattr(raw_job, "scope_status", "") or "").strip().upper()
    if scope in {"", RawJob.ScopeStatus.UNSCOPED}:
        return True
    domain = (getattr(raw_job, "job_domain", "") or "").strip()
    if not domain:
        return True
    if getattr(raw_job, "quality_score", None) is None and getattr(raw_job, "jd_quality_score", None) is None:
        return True
    return False


def resolve_unified_pipeline(raw_job, *, gate=None) -> dict:
    """
    Return a single pipeline position for UI/analytics.

    Keys: step, label, detail, level (success|progress|warn|error|muted)
    """
    from harvest.jd_gate import evaluate_raw_job_resume_gate

    if gate is None:
        gate = evaluate_raw_job_resume_gate(raw_job)

    sync = (getattr(raw_job, "sync_status", "") or "").strip().upper()
    if sync == RawJob.SyncStatus.SYNCED:
        return _pack("VET_QUEUE", "Synced to vetting queue", "success")
    if sync == RawJob.SyncStatus.FAILED:
        return _pack("FAILED", raw_job.last_error_text() or "Sync failed", "error")
    if sync == RawJob.SyncStatus.DUPLICATE:
        return _pack("DUPLICATE", "Duplicate / skipped", "muted")
    if sync == RawJob.SyncStatus.SKIPPED and (getattr(raw_job, "sync_skip_reason", "") or "") in DUPLICATE_SKIP_REASONS:
        return _pack("DUPLICATE", "Duplicate / skipped", "muted")
    if getattr(raw_job, "is_test_run", False):
        return _pack("TEST", "Smoke / test row", "muted")
    if (
        getattr(raw_job, "is_cold", False)
        or getattr(raw_job, "jd_fetch_skipped", False)
        or (raw_job.filter_decision or "") in {"COLD", "NO_MATCH"}
    ):
        return _pack("FILTERED_OUT", raw_job.filter_reason or "Outside intake rules", "muted")
    if not getattr(raw_job, "is_active", True):
        return _pack("INACTIVE", "Posting inactive or expired", "error")

    role = (getattr(raw_job, "role_category", "") or "").strip()
    intake_detail = f"STRONG — {role}" if role else "STRONG phrase match"

    if not getattr(raw_job, "has_description", False):
        if getattr(raw_job, "jd_backfill_locked_at", None):
            return _pack("JD_BACKFILL", "Fetching job description…", "progress")
        return _pack("INTAKE", intake_detail, "warn")

    if not gate.usable:
        if getattr(raw_job, "jd_backfill_locked_at", None):
            return _pack("JD_BACKFILL", "Fetching job description…", "progress")
        return _pack("JD_BACKFILL", gate.reason_text or "JD not resume-ready", "warn")

    if _needs_enrichment(raw_job):
        missing = []
        scope = (getattr(raw_job, "scope_status", "") or "").strip()
        if scope in {"", RawJob.ScopeStatus.UNSCOPED}:
            missing.append("scope")
        if not (getattr(raw_job, "job_domain", "") or "").strip():
            missing.append("domain")
        if not missing:
            missing.append("fields")
        return _pack("ENRICH", f"Pending {' + '.join(missing)}", "progress")

    if (
        not getattr(raw_job, "is_test_run", False)
        and is_strong_intake(raw_job)
        and getattr(raw_job, "has_description", False)
        and gate.usable
        and getattr(raw_job, "is_active", True)
    ):
        return _pack("READY", "Eligible to sync to vet queue", "success")

    return _pack("ENRICH", "Finishing enrichment", "progress")


def _pack(step: str, detail: str, level: str) -> dict:
    return {
        "step": step,
        "label": _STEP_LABELS.get(step, step.replace("_", " ").title()),
        "detail": detail,
        "level": level,
    }


def _active_pending_q() -> Q:
    return (
        Q(is_test_run=False)
        & Q(is_active=True)
        & ~filtered_out_q()
        & ~duplicate_rawjob_q()
        & ~Q(sync_status__in=[RawJob.SyncStatus.SYNCED, RawJob.SyncStatus.FAILED])
    )


def unified_step_q(step: str) -> Q:
    """Query filter for funnel drill-down (approximate where gate logic is needed)."""
    step_key = (step or "").strip().upper()
    min_words = _min_jd_words()

    if step_key == "VET_QUEUE":
        return Q(sync_status=RawJob.SyncStatus.SYNCED, is_test_run=False)
    if step_key == "FAILED":
        return Q(sync_status=RawJob.SyncStatus.FAILED, is_test_run=False)
    if step_key == "DUPLICATE":
        return Q(is_test_run=False) & duplicate_rawjob_q()
    if step_key == "FILTERED_OUT":
        return Q(is_test_run=False) & filtered_out_q()
    if step_key == "INACTIVE":
        return Q(is_test_run=False, is_active=False) & ~Q(sync_status=RawJob.SyncStatus.SYNCED)
    if step_key == "TEST":
        return Q(is_test_run=True)

    active = _active_pending_q() & Q(filter_decision=STRONG)

    if step_key == "INTAKE":
        return active & Q(has_description=False, jd_backfill_locked_at__isnull=True)
    if step_key == "JD_BACKFILL":
        return active & (
            Q(jd_backfill_locked_at__isnull=False)
            | Q(has_description=False)
            | Q(has_description=True, word_count__lt=min_words)
        )
    if step_key == "READY":
        return active & ready_stage_q() & Q(word_count__gte=min_words)
    if step_key == "ENRICH":
        enrich_scope = Q(scope_status__in=["", RawJob.ScopeStatus.UNSCOPED]) | Q(job_domain="")
        has_jd = Q(has_description=True, word_count__gte=min_words)
        return active & has_jd & enrich_scope & ~ready_stage_q()
    return Q()


def build_unified_funnel_counts(base_qs) -> dict[str, int]:
    """Exclusive-ish funnel counts aligned to the single pipeline."""
    qs = base_qs.filter(is_test_run=False)
    total = qs.count()
    vet = qs.filter(unified_step_q("VET_QUEUE")).count()
    failed = qs.filter(unified_step_q("FAILED")).count()
    dupes = qs.filter(unified_step_q("DUPLICATE")).count()
    filtered = qs.filter(unified_step_q("FILTERED_OUT")).count()
    inactive = qs.filter(unified_step_q("INACTIVE")).count()
    intake = qs.filter(unified_step_q("INTAKE")).count()
    jd_backfill = qs.filter(unified_step_q("JD_BACKFILL")).count()
    ready = qs.filter(unified_step_q("READY")).count()
    enrich = qs.filter(unified_step_q("ENRICH")).count()
    return {
        "total": total,
        "intake": intake,
        "jd_backfill": jd_backfill,
        "enrich": enrich,
        "ready": ready,
        "vet_queue": vet,
        "failed": failed,
        "duplicate": dupes,
        "filtered_out": filtered,
        "inactive": inactive,
    }

from __future__ import annotations

from .config import require_approval_for_sync


def raw_job_requires_dual_classification(raw_job) -> bool:
    title = (getattr(raw_job, "title", "") or "").strip()
    description = (
        getattr(raw_job, "description_clean", "")
        or getattr(raw_job, "description", "")
        or ""
    ).strip()
    return bool(title) and len(description) >= 80


def sync_block_reason(raw_job) -> str:
    if not require_approval_for_sync():
        return ""
    if not raw_job_requires_dual_classification(raw_job):
        return ""

    snapshot = getattr(raw_job, "classification_snapshot", None)
    if not snapshot:
        return "Awaiting approved dual classification before vetting sync."
    if not getattr(snapshot, "approved_output", None):
        return "Approve backend, secondary, merged, or manual classification before vetting sync."
    if not bool(getattr(snapshot, "ready_for_vetting", False)):
        return "Approved dual classification still has blocking verifier warnings."
    return ""

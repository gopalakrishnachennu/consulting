from __future__ import annotations

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Job


JOB_LINK_HEALTH_UPDATE_FIELDS = [
    "original_link_is_live",
    "original_link_health",
    "original_link_reason",
    "original_link_status_code",
    "original_link_final_url",
    "original_link_last_checked_at",
    "possibly_filled",
]


def normalize_job_link_health_state(
    state: str | None,
    *,
    is_live: bool | None = None,
    decisive: bool | None = None,
) -> str:
    state_s = (state or "").strip().upper()
    valid = {
        Job.LinkHealthState.LIVE,
        Job.LinkHealthState.INCONCLUSIVE,
        Job.LinkHealthState.DEAD,
    }
    if state_s in valid:
        return state_s
    if decisive:
        return Job.LinkHealthState.DEAD
    if is_live is False:
        return Job.LinkHealthState.INCONCLUSIVE
    return Job.LinkHealthState.LIVE


def apply_link_health_payload_to_job(
    job: Job,
    payload: dict | None,
    *,
    checked_at=None,
) -> str:
    payload = payload or {}
    if payload:
        state = normalize_job_link_health_state(
            payload.get("state"),
            is_live=payload.get("is_live"),
            decisive=payload.get("decisive"),
        )
    else:
        state = Job.LinkHealthState.INCONCLUSIVE

    checked_at_value = checked_at
    if checked_at_value is None:
        checked_at_raw = (payload.get("checked_at") or "").strip()
        checked_at_value = parse_datetime(checked_at_raw) if checked_at_raw else None
    if checked_at_value is None:
        checked_at_value = timezone.now()

    status_code = payload.get("status_code")
    try:
        status_code = int(status_code) if status_code not in (None, "") else None
    except (TypeError, ValueError):
        status_code = None
    if status_code is not None and status_code <= 0:
        status_code = None

    final_url = (payload.get("final_url") or job.original_link or "")[:1024]

    job.original_link_health = state
    job.original_link_is_live = state != Job.LinkHealthState.DEAD
    job.original_link_reason = (payload.get("reason") or "")[:120]
    job.original_link_status_code = status_code
    job.original_link_final_url = final_url
    job.original_link_last_checked_at = checked_at_value
    job.possibly_filled = state == Job.LinkHealthState.DEAD and job.status == Job.Status.OPEN
    return state

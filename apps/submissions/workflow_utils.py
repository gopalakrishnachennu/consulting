"""
Pipeline column counts and stale cues for the consultant workflow dashboard.
Logic must stay aligned with WorkflowPanelView categorisation.
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from jobs.models import Job
from resumes.models import Resume
from submissions.models import ApplicationSubmission


# Same as WorkflowPanelView — submissions that land in column 3
_ACTIVE_SUBMISSION_STATUSES = frozenset(
    {
        ApplicationSubmission.Status.APPLIED,
        ApplicationSubmission.Status.INTERVIEW,
        ApplicationSubmission.Status.OFFER,
        ApplicationSubmission.Status.PLACED,
        ApplicationSubmission.Status.REJECTED,
        ApplicationSubmission.Status.WITHDRAWN,
    }
)


def _day_age(now, dt) -> int:
    if dt is None:
        return 0
    return max(0, int((now - dt).total_seconds() // 86400))


def get_pipeline_workflow_bulk(consultants: list, now=None) -> dict[int, dict]:
    """
    For each ConsultantProfile pk, return counts and stale flags matching the 3 workflow columns.

    Stale: assigned uses job.created_at for column-1 jobs; drafting uses the earliest of
    latest draft.created_at and submission.created_at per column-2 job, then the worst (max days).

    Returns:
      { pk: {
          "assigned", "drafting", "submitted",
          "stale_assigned", "stale_drafting",
          "stale_assigned_days", "stale_drafting_days" (optional ints for tooltips),
        } }
    """
    if not consultants:
        return {}

    now = now or timezone.now()
    stale_days = int(getattr(settings, "WORKFLOW_STALE_DAYS", 7))

    c_ids = [c.pk for c in consultants]

    open_jobs = list(
        Job.objects.filter(status=Job.Status.OPEN, is_archived=False).prefetch_related("marketing_roles")
    )
    jobs_by_id = {j.pk: j for j in open_jobs}
    job_role_sets = {j.pk: set(j.marketing_roles.values_list("id", flat=True)) for j in open_jobs}
    matched_job_ids_set = set(job_role_sets.keys())

    drafts_qs = (
        Resume.objects.filter(consultant_id__in=c_ids)
        .order_by("consultant_id", "job_id", "-version")
    )
    draft_map: dict[tuple[int, int], Resume] = {}
    for d in drafts_qs:
        key = (d.consultant_id, d.job_id)
        if key not in draft_map:
            draft_map[key] = d

    subs_qs = (
        ApplicationSubmission.objects.filter(consultant_id__in=c_ids, is_archived=False)
        .select_related("job")
        .order_by("consultant_id", "job_id", "-updated_at")
    )
    sub_map: dict[tuple[int, int], ApplicationSubmission] = {}
    for s in subs_qs:
        key = (s.consultant_id, s.job_id)
        if key not in sub_map:
            sub_map[key] = s

    result: dict[int, dict] = {}

    for c in consultants:
        cid = c.pk
        role_ids = set(c.marketing_roles.values_list("id", flat=True))
        if not role_ids:
            matched_ids = []
        else:
            matched_ids = [jid for jid, jroles in job_role_sets.items() if role_ids & jroles]

        matched_set = set(matched_ids)
        n1 = n2 = n3 = 0
        oldest_assigned_days = 0
        worst_drafting_days = 0

        for jid in matched_ids:
            sub = sub_map.get((cid, jid))
            draft = draft_map.get((cid, jid))
            job = jobs_by_id.get(jid)
            if not job:
                continue
            if sub and sub.status in _ACTIVE_SUBMISSION_STATUSES:
                n3 += 1
            elif sub or draft:
                n2 += 1
                starts = []
                if draft:
                    starts.append(draft.created_at)
                if sub:
                    starts.append(sub.created_at)
                if starts:
                    st = min(starts)
                    worst_drafting_days = max(worst_drafting_days, _day_age(now, st))
            else:
                n1 += 1
                oldest_assigned_days = max(oldest_assigned_days, _day_age(now, job.created_at))

        for (scid, sjid), sub in sub_map.items():
            if scid != cid or sjid in matched_set:
                continue
            if sub.status in _ACTIVE_SUBMISSION_STATUSES:
                n3 += 1

        stale_assigned_days = oldest_assigned_days if n1 else None
        stale_drafting_days = worst_drafting_days if n2 else None

        result[cid] = {
            "assigned": n1,
            "drafting": n2,
            "submitted": n3,
            "stale_assigned": bool(n1 and oldest_assigned_days >= stale_days),
            "stale_drafting": bool(n2 and worst_drafting_days >= stale_days),
            "stale_assigned_days": stale_assigned_days,
            "stale_drafting_days": stale_drafting_days,
        }

    return result


def get_pipeline_counts_bulk(consultants: list) -> dict[int, dict[str, int]]:
    """Thin wrapper — counts only (backwards compatible)."""
    full = get_pipeline_workflow_bulk(consultants)
    return {k: {x: v[x] for x in ("assigned", "drafting", "submitted")} for k, v in full.items()}

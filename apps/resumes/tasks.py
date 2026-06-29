"""
Celery tasks for automated resume generation.

Pipeline: Vetted Job → Marketing Role Match → Auto-Generate Resume

When jobs are synced to the vet pool, they get marketing_roles assigned.
Consultants also have marketing_roles. This task matches them and generates
tailored resumes for each consultant-job pair automatically.
"""

from __future__ import annotations

import logging
from typing import Optional

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("apps.resumes.tasks")


def _resume_draft_defaults(*, source_state: dict, generation_mode: str, generation_reason: str, idempotency_key: str, auto_generated: bool) -> dict:
    return {
        "auto_generated": auto_generated,
        "generation_mode": generation_mode,
        "generation_reason": generation_reason,
        "idempotency_key": idempotency_key,
        "source_snapshot_hash": source_state.get("snapshot_hash", ""),
        "source_snapshot_source": source_state.get("snapshot_source", ""),
        "source_snapshot_approved_at": source_state.get("approved_at"),
        "source_primary_role_slug": source_state.get("primary_role_slug", ""),
        "source_prompt_version": source_state.get("prompt_version", ""),
        "source_classification_source": source_state.get("classification_source", ""),
    }


# ── Auto-match & generate for newly vetted jobs ──────────────────────────────

@shared_task(
    bind=True,
    name="resumes.auto_generate_for_new_jobs",
    max_retries=0,
    soft_time_limit=3600,   # 1 hour soft limit
    time_limit=3900,        # 1 hour 5 min hard kill
)
def auto_generate_for_new_jobs_task(
    self,
    job_ids: Optional[list[int]] = None,
    max_jobs: int = 0,
    dry_run: bool = False,
):
    """
    For each newly vetted job (or specific job_ids), find matching consultants
    by marketing role overlap, and generate a tailored resume for each pair.

    Flow:
        1. Find jobs that are in POOL status with marketing_roles but no resume drafts yet
        2. For each job, find active consultants whose marketing_roles overlap
        3. For each consultant-job pair, generate a resume via the LLM engine
        4. Save as ResumeDraft with status=DRAFT (or ERROR on failure)

    Args:
        job_ids:   Specific Job PKs to process (None = all unprocessed)
        max_jobs:  Cap on total jobs to process (0 = no limit)
        dry_run:   If True, find matches but don't generate resumes
    """
    from jobs.models import Job
    from jobs.services import consultant_job_routing_audit
    from users.models import ConsultantProfile
    from resumes.models import ResumeDraft
    from resumes.engine import generate_resume
    from resumes.services import (
        build_resume_draft_idempotency_key,
        find_reusable_resume_draft,
        resume_generation_source_state,
    )

    # Step 1: Find eligible jobs
    qs = Job.objects.filter(
        status="POOL",
        is_archived=False,
        marketing_roles__isnull=False,
    ).distinct()

    if job_ids:
        qs = qs.filter(pk__in=job_ids)
    else:
        # Only jobs that don't already have auto-generated drafts
        qs = qs.exclude(
            pk__in=ResumeDraft.objects.filter(
                auto_generated=True,
            ).values_list("job_id", flat=True)
        )

    qs = qs.prefetch_related("marketing_roles").order_by("-created_at")
    if max_jobs:
        qs = qs[:max_jobs]

    jobs = list(qs)
    total_jobs = len(jobs)

    if not jobs:
        logger.info("auto_generate_for_new_jobs: no eligible jobs found")
        return {
            "processed_jobs": 0,
            "total_pairs": 0,
            "generated": 0,
            "skipped": 0,
            "failed": 0,
            "dry_run": dry_run,
        }

    # Step 2: Find active consultants with marketing roles
    active_consultants = list(
        ConsultantProfile.objects.filter(
            status__in=[ConsultantProfile.Status.ACTIVE, ConsultantProfile.Status.BENCH],
        ).prefetch_related("marketing_roles")
    )

    if not active_consultants:
        logger.info("auto_generate_for_new_jobs: no active consultants with marketing roles")
        return {
            "processed_jobs": total_jobs,
            "total_pairs": 0,
            "generated": 0,
            "skipped": 0,
            "failed": 0,
            "dry_run": dry_run,
        }

    # Build consultant → role slug set for fast matching
    consultant_roles: dict[int, set[str]] = {}
    for c in active_consultants:
        slugs = set(c.marketing_roles.values_list("slug", flat=True))
        if slugs:
            consultant_roles[c.pk] = slugs

    generated = 0
    skipped = 0
    failed = 0
    pairs_found = 0

    for job_idx, job in enumerate(jobs):
        job_role_slugs = set(job.marketing_roles.values_list("slug", flat=True))
        if not job_role_slugs:
            continue

        # Find matching consultants
        matched_consultants = []
        for c in active_consultants:
            c_slugs = consultant_roles.get(c.pk, set())
            if not (c_slugs & job_role_slugs):
                continue
            audit = consultant_job_routing_audit(job, c)
            if not audit.get("eligible"):
                continue
            source_state = resume_generation_source_state(job)
            if source_state.get("blocked"):
                continue
            if getattr(job, "primary_marketing_role", None):
                source_state.setdefault("primary_role_slug", getattr(job.primary_marketing_role, "slug", "") or "")
            matched_consultants.append((c, source_state))

        for consultant, source_state in matched_consultants:
            pairs_found += 1
            idempotency_key = build_resume_draft_idempotency_key(
                consultant=consultant,
                job=job,
                generation_mode="auto",
                generation_reason="new_pool_job",
                source_state=source_state,
            )

            # Check if draft already exists for this pair
            existing = find_reusable_resume_draft(
                consultant=consultant,
                job=job,
                idempotency_key=idempotency_key,
            ) or ResumeDraft.objects.filter(
                consultant=consultant,
                job=job,
                source_snapshot_hash=source_state.get("snapshot_hash", ""),
            ).exclude(status=ResumeDraft.Status.ERROR).first()
            if existing:
                skipped += 1
                continue

            if dry_run:
                generated += 1  # would generate
                logger.info(
                    "DRY RUN: Would generate resume for consultant %s (%s) × job %s (%s @ %s)",
                    consultant.pk, consultant.user.get_full_name(),
                    job.pk, job.title, job.company,
                )
                continue

            # Generate resume
            try:
                content, tokens, error, metadata = generate_resume(
                    job=job,
                    consultant=consultant,
                    actor=None,
                )
                if error:
                    draft = ResumeDraft.objects.create(
                        consultant=consultant,
                        job=job,
                        version=1,
                        status=ResumeDraft.Status.ERROR,
                        error_message=(error or "")[:500],
                        **_resume_draft_defaults(
                            source_state=source_state,
                            generation_mode="auto",
                            generation_reason="new_pool_job",
                            idempotency_key=idempotency_key,
                            auto_generated=True,
                        ),
                    )
                    logger.warning(
                        "Resume gen error for consultant %s × job %s: %s",
                        consultant.pk, job.pk, error,
                    )
                    failed += 1
                else:
                    # Run validation
                    from resumes.services import validate_resume, score_ats
                    errors_list, warnings_list = validate_resume(content or "")
                    ats = score_ats(job.description or "", content or "")

                    draft = ResumeDraft.objects.create(
                        consultant=consultant,
                        job=job,
                        version=1,
                        status=ResumeDraft.Status.REVIEW if errors_list else ResumeDraft.Status.DRAFT,
                        content=content or "",
                        tokens_used=tokens,
                        ats_score=ats,
                        validation_errors=errors_list,
                        validation_warnings=warnings_list,
                        llm_system_prompt=metadata.get("system_prompt", ""),
                        llm_user_prompt=metadata.get("user_prompt", ""),
                        llm_input_summary=metadata,
                        llm_request_payload=metadata.get("totals", {}),
                        **_resume_draft_defaults(
                            source_state=source_state,
                            generation_mode="auto",
                            generation_reason="new_pool_job",
                            idempotency_key=idempotency_key,
                            auto_generated=True,
                        ),
                    )
                    generated += 1
                    logger.info(
                        "Auto-generated resume draft %s for consultant %s × job %s (ATS: %s)",
                        draft.pk, consultant.pk, job.pk, ats,
                    )

            except Exception as exc:
                logger.error(
                    "Exception generating resume for consultant %s × job %s: %s",
                    consultant.pk, job.pk, exc,
                )
                try:
                    ResumeDraft.objects.create(
                        consultant=consultant,
                        job=job,
                        version=1,
                        status=ResumeDraft.Status.ERROR,
                        error_message=str(exc)[:500],
                        **_resume_draft_defaults(
                            source_state=source_state,
                            generation_mode="auto",
                            generation_reason="new_pool_job",
                            idempotency_key=idempotency_key,
                            auto_generated=True,
                        ),
                    )
                except Exception:
                    pass
                failed += 1

        # Progress update
        if hasattr(self, 'update_state'):
            self.update_state(
                state="PROGRESS",
                meta={
                    "current": job_idx + 1,
                    "total": total_jobs,
                    "message": f"Processed {job_idx + 1}/{total_jobs} jobs — {generated} resumes generated",
                },
            )

    result = {
        "processed_jobs": total_jobs,
        "total_pairs": pairs_found,
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }
    logger.info("auto_generate_for_new_jobs complete: %s", result)
    return result


# ── Generate for a single consultant across all their matched jobs ────────────

@shared_task(
    bind=True,
    name="resumes.generate_for_consultant",
    max_retries=0,
    soft_time_limit=1800,
    time_limit=2100,
)
def generate_for_consultant_task(
    self,
    consultant_id: int,
    max_jobs: int = 0,
    dry_run: bool = False,
):
    """
    Generate resumes for a single consultant against all their unmatched
    POOL jobs (by marketing role overlap).
    """
    from jobs.models import Job
    from users.models import ConsultantProfile
    from resumes.models import ResumeDraft
    from resumes.engine import generate_resume
    from resumes.services import (
        build_resume_draft_idempotency_key,
        find_reusable_resume_draft,
        resume_generation_source_state,
    )
    from jobs.services import match_jobs_for_consultant

    try:
        consultant = ConsultantProfile.objects.prefetch_related("marketing_roles").get(pk=consultant_id)
    except ConsultantProfile.DoesNotExist:
        return {"error": f"ConsultantProfile {consultant_id} not found"}

    role_slugs = set(consultant.marketing_roles.values_list("slug", flat=True))
    if not role_slugs:
        return {"error": "Consultant has no marketing roles assigned", "consultant_id": consultant_id}

    jobs = [job for job in match_jobs_for_consultant(consultant, limit=max_jobs or 500) if job.status == Job.Status.OPEN]
    if max_jobs:
        jobs = jobs[:max_jobs]

    total = len(jobs)
    generated = 0
    failed = 0

    for idx, job in enumerate(jobs):
        if dry_run:
            generated += 1
            continue

        try:
            source_state = resume_generation_source_state(job)
            if source_state.get("blocked"):
                failed += 1
                continue
            idempotency_key = build_resume_draft_idempotency_key(
                consultant=consultant,
                job=job,
                generation_mode="auto",
                generation_reason="consultant_batch",
                source_state=source_state,
            )
            existing = find_reusable_resume_draft(
                consultant=consultant,
                job=job,
                idempotency_key=idempotency_key,
            )
            if existing:
                continue
            content, tokens, error, metadata = generate_resume(job=job, consultant=consultant)
            if error:
                ResumeDraft.objects.create(
                    consultant=consultant, job=job, version=1,
                    status=ResumeDraft.Status.ERROR,
                    error_message=(error or "")[:500],
                    **_resume_draft_defaults(
                        source_state=source_state,
                        generation_mode="auto",
                        generation_reason="consultant_batch",
                        idempotency_key=idempotency_key,
                        auto_generated=True,
                    ),
                )
                failed += 1
            else:
                from resumes.services import validate_resume, score_ats
                errors_list, warnings_list = validate_resume(content or "")
                ats = score_ats(job.description or "", content or "")
                ResumeDraft.objects.create(
                    consultant=consultant, job=job, version=1,
                    status=ResumeDraft.Status.REVIEW if errors_list else ResumeDraft.Status.DRAFT,
                    content=content or "",
                    tokens_used=tokens,
                    ats_score=ats,
                    validation_errors=errors_list,
                    validation_warnings=warnings_list,
                    llm_system_prompt=metadata.get("system_prompt", ""),
                    llm_user_prompt=metadata.get("user_prompt", ""),
                    llm_input_summary=metadata,
                    llm_request_payload=metadata.get("totals", {}),
                    **_resume_draft_defaults(
                        source_state=source_state,
                        generation_mode="auto",
                        generation_reason="consultant_batch",
                        idempotency_key=idempotency_key,
                        auto_generated=True,
                    ),
                )
                generated += 1
        except Exception as exc:
            logger.error("Exception for consultant %s × job %s: %s", consultant_id, job.pk, exc)
            failed += 1

        if hasattr(self, 'update_state'):
            self.update_state(state="PROGRESS", meta={
                "current": idx + 1, "total": total,
                "message": f"{idx + 1}/{total} — {generated} generated",
            })

    return {
        "consultant_id": consultant_id,
        "consultant_name": consultant.user.get_full_name(),
        "jobs_matched": total,
        "generated": generated,
        "failed": failed,
        "dry_run": dry_run,
    }

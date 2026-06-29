from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from harvest.models import RawJob
from harvest.services.job_descriptions import job_description_for_sync
from users.models import MarketingRole

from jobs.models import (
    RawJobClassificationConflict,
    RawJobClassificationSnapshot,
    RawJobClassifierRun,
)
from jobs.marketing_role_routing import infer_marketing_role_slugs

from .audit import build_job_dual_classification_meta
from .merger import merge_outputs
from .providers import (
    BackendRulesProvider,
    ClassificationContext,
    ProviderResult,
    RuntimeLLMSecondaryProvider,
    SecondaryStubProvider,
)
from .schema import (
    build_raw_job_input,
    compute_approval_input_hash,
    compute_input_hash,
    validate_canonical_output,
)
from .verifier import verify_output


STALE_APPROVAL_REVIEW_REASON = "input_changed_after_approval"


def _resolve_approved_primary_role_slug(
    *,
    raw_job: RawJob,
    chosen_output: dict,
    requested_slug: str = "",
) -> str:
    requested_slug = (requested_slug or "").strip()
    if requested_slug:
        return requested_slug
    inferred = infer_marketing_role_slugs(
        title=raw_job.title or "",
        description=job_description_for_sync(raw_job),
        job_category=((chosen_output.get("classification") or {}).get("job_category")) or "",
        department_normalized=((chosen_output.get("classification") or {}).get("department_normalized")) or "",
        primary_domain=((chosen_output.get("classification") or {}).get("job_domain")) or "",
        max_roles=1,
    )
    return inferred[0] if inferred else ""


def _create_run_record(raw_job: RawJob, provider_result: ProviderResult, input_hash: str, status: str, error_message: str = ""):
    return RawJobClassifierRun.objects.create(
        raw_job=raw_job,
        provider=provider_result.provider,
        provider_role=provider_result.provider_role,
        input_hash=input_hash,
        prompt_version=provider_result.prompt_version,
        provider_version=provider_result.provider_version,
        status=status,
        confidence=provider_result.confidence,
        raw_output=provider_result.raw_output,
        normalized_output=provider_result.normalized_output,
        warnings=provider_result.warnings,
        error_message=error_message,
        completed_at=timezone.now() if status in {RawJobClassifierRun.Status.COMPLETED, RawJobClassifierRun.Status.SKIPPED, RawJobClassifierRun.Status.FAILED} else None,
    )


def _resolve_secondary_provider() -> BaseException | object:
    from .config import default_secondary_provider, secondary_runtime_enabled

    provider_code = default_secondary_provider().lower()
    if secondary_runtime_enabled() and provider_code in RuntimeLLMSecondaryProvider.available_provider_codes():
        return RuntimeLLMSecondaryProvider(provider_code)
    return SecondaryStubProvider()


def _persist_snapshot(
    *,
    raw_job: RawJob,
    snapshot: RawJobClassificationSnapshot,
    input_hash: str,
    backend_run: RawJobClassifierRun,
    secondary_run: RawJobClassifierRun | None,
    merged_output: dict,
    verifier_summary: dict,
    conflicts: list[dict],
    final_confidence: float,
    needs_review: bool,
    review_reason: str,
    status: str,
) -> RawJobClassificationSnapshot:
    with transaction.atomic():
        fresh_approval_hash = compute_approval_input_hash(raw_job)
        approval_is_stale = bool(
            snapshot.approved_output
            and snapshot.approval_input_hash
            and snapshot.approval_input_hash != fresh_approval_hash
        )
        if approval_is_stale:
            needs_review = True
            review_reason = STALE_APPROVAL_REVIEW_REASON
        snapshot.current_input_hash = input_hash
        snapshot.backend_run = backend_run
        snapshot.secondary_run = secondary_run
        snapshot.merged_output = merged_output
        snapshot.verifier_summary = verifier_summary
        snapshot.final_confidence = final_confidence
        snapshot.needs_review = needs_review
        snapshot.review_reason = review_reason
        snapshot.ready_for_vetting = False
        snapshot.status = (
            RawJobClassificationSnapshot.Status.NEEDS_REVIEW
            if approval_is_stale
            else status
        )
        snapshot.approval_is_stale = approval_is_stale
        if approval_is_stale and not snapshot.approval_stale_at:
            snapshot.approval_stale_at = timezone.now()
        snapshot.last_merged_at = timezone.now()
        snapshot.save()

        snapshot.field_conflicts.all().delete()
        for conflict in conflicts:
            RawJobClassificationConflict.objects.create(
                raw_job=raw_job,
                snapshot=snapshot,
                field_path=conflict["field_path"],
                backend_value=conflict["backend_value"],
                secondary_value=conflict["secondary_value"],
                resolved_value=conflict["resolved_value"],
                resolution=conflict["resolution"],
                severity=conflict["severity"],
                note=conflict["note"],
            )
    return snapshot


def invalidate_approved_snapshot_for_raw_job_input_change(
    raw_job: RawJob,
    *,
    fresh_input_hash: str | None = None,
) -> dict:
    snapshot = getattr(raw_job, "classification_snapshot", None)
    if not snapshot:
        return {"invalidated": False, "reason": "no_snapshot"}
    if not snapshot.approved_output:
        return {"invalidated": False, "reason": "no_approved_output"}

    fresh_hash = fresh_input_hash
    if not fresh_hash:
        fresh_hash = compute_approval_input_hash(raw_job)
    approved_hash = snapshot.approval_input_hash or ""
    if not approved_hash:
        with transaction.atomic():
            locked_snapshot = RawJobClassificationSnapshot.objects.select_for_update().get(pk=snapshot.pk)
            if not locked_snapshot.approval_input_hash and locked_snapshot.approved_output:
                locked_snapshot.approval_input_hash = fresh_hash
                locked_snapshot.save(update_fields=["approval_input_hash", "updated_at"])
        return {"invalidated": False, "reason": "approval_baseline_initialized"}
    if approved_hash == fresh_hash:
        return {"invalidated": False, "reason": "input_unchanged"}

    with transaction.atomic():
        locked_snapshot = RawJobClassificationSnapshot.objects.select_for_update().get(pk=snapshot.pk)
        if locked_snapshot.approval_is_stale:
            return {"invalidated": False, "reason": "already_stale"}
        approved_hash = locked_snapshot.approval_input_hash or ""
        if not approved_hash or approved_hash == fresh_hash or not locked_snapshot.approved_output:
            return {"invalidated": False, "reason": "input_unchanged"}

        locked_snapshot.approval_is_stale = True
        locked_snapshot.approval_stale_at = timezone.now()
        locked_snapshot.ready_for_vetting = False
        locked_snapshot.needs_review = True
        locked_snapshot.review_reason = STALE_APPROVAL_REVIEW_REASON
        locked_snapshot.save(
            update_fields=[
                "approval_is_stale",
                "approval_stale_at",
                "ready_for_vetting",
                "needs_review",
                "review_reason",
                "updated_at",
            ]
        )
    return {"invalidated": True, "reason": STALE_APPROVAL_REVIEW_REASON}


def run_shadow_classification_for_raw_job(raw_job_id: int, *, force: bool = False) -> dict:
    raw_job = RawJob.objects.get(pk=raw_job_id)
    description = (raw_job.description_clean or raw_job.description or "").strip()
    if not (raw_job.title or "").strip() or len(description) < 80:
        snapshot, _ = RawJobClassificationSnapshot.objects.get_or_create(raw_job=raw_job)
        snapshot.status = RawJobClassificationSnapshot.Status.PENDING
        snapshot.review_reason = "insufficient_jd_text"
        snapshot.needs_review = False
        snapshot.ready_for_vetting = False
        snapshot.current_input_hash = ""
        snapshot.last_merged_at = timezone.now()
        snapshot.save(
            update_fields=[
                "status",
                "review_reason",
                "needs_review",
                "ready_for_vetting",
                "current_input_hash",
                "last_merged_at",
                "updated_at",
            ]
        )
        return {"status": "skipped", "reason": "insufficient_jd_text", "raw_job_id": raw_job_id}

    input_payload = build_raw_job_input(raw_job)
    input_hash = compute_input_hash(input_payload)
    snapshot, _ = RawJobClassificationSnapshot.objects.get_or_create(raw_job=raw_job)

    if (
        not force
        and
        snapshot.current_input_hash == input_hash
        and snapshot.backend_run_id
        and snapshot.backend_run.status == RawJobClassifierRun.Status.COMPLETED
        and snapshot.status in {RawJobClassificationSnapshot.Status.PARTIAL, RawJobClassificationSnapshot.Status.MERGED, RawJobClassificationSnapshot.Status.NEEDS_REVIEW}
    ):
        return {"status": "cached", "raw_job_id": raw_job_id, "input_hash": input_hash}

    context = ClassificationContext(raw_job=raw_job, input_payload=input_payload, input_hash=input_hash)
    backend_provider = BackendRulesProvider()
    secondary_provider = _resolve_secondary_provider()

    try:
        backend_result = backend_provider.classify(context)
        backend_run = _create_run_record(raw_job, backend_result, input_hash, RawJobClassifierRun.Status.COMPLETED)
    except Exception as exc:
        failed_result = ProviderResult(
            provider=RawJobClassifierRun.Provider.BACKEND_RULES,
            provider_role=RawJobClassifierRun.ProviderRole.PRIMARY,
        )
        backend_run = _create_run_record(
            raw_job,
            failed_result,
            input_hash,
            RawJobClassifierRun.Status.FAILED,
            error_message=str(exc)[:1000],
        )
        snapshot.current_input_hash = input_hash
        snapshot.backend_run = backend_run
        snapshot.secondary_run = None
        snapshot.status = RawJobClassificationSnapshot.Status.FAILED
        snapshot.needs_review = True
        snapshot.review_reason = "backend_provider_failed"
        snapshot.ready_for_vetting = False
        snapshot.last_merged_at = timezone.now()
        snapshot.save()
        return {"status": "failed", "raw_job_id": raw_job_id, "reason": "backend_provider_failed"}

    secondary_output: dict | None = None
    secondary_confidence: float | None = None
    secondary_failed = False
    try:
        secondary_result = secondary_provider.classify(context)
        secondary_status = (
            RawJobClassifierRun.Status.SKIPPED
            if secondary_result.provider == RawJobClassifierRun.Provider.SECONDARY_STUB
            else RawJobClassifierRun.Status.COMPLETED
        )
        secondary_run = _create_run_record(raw_job, secondary_result, input_hash, secondary_status)
        if secondary_status == RawJobClassifierRun.Status.COMPLETED:
            secondary_output = secondary_result.normalized_output or {}
            secondary_confidence = secondary_result.confidence
    except Exception as exc:
        failed_secondary = ProviderResult(
            provider=getattr(secondary_provider, "code", RawJobClassifierRun.Provider.SECONDARY_STUB),
            provider_role=RawJobClassifierRun.ProviderRole.SECONDARY,
            provider_version=getattr(secondary_provider, "provider_version", "") or "",
            prompt_version=getattr(secondary_provider, "prompt_version", "") or "",
        )
        secondary_run = _create_run_record(
            raw_job,
            failed_secondary,
            input_hash,
            RawJobClassifierRun.Status.FAILED,
            error_message=str(exc)[:1000],
        )
        secondary_failed = secondary_run.provider != RawJobClassifierRun.Provider.SECONDARY_STUB

    merged_output, conflicts, final_confidence, needs_review, review_reason = merge_outputs(
        backend_output=backend_result.normalized_output,
        secondary_output=secondary_output,
        backend_confidence=backend_result.confidence,
        secondary_confidence=secondary_confidence,
    )
    verifier_summary = verify_output(raw_job, merged_output)
    if verifier_summary["status"] == "fail":
        needs_review = True
        review_reason = review_reason or "verifier_failed"
    if secondary_failed:
        needs_review = True
        review_reason = review_reason or "secondary_provider_failed"

    snapshot = _persist_snapshot(
        raw_job=raw_job,
        snapshot=snapshot,
        input_hash=input_hash,
        backend_run=backend_run,
        secondary_run=secondary_run,
        merged_output=merged_output,
        verifier_summary=verifier_summary,
        conflicts=conflicts,
        final_confidence=final_confidence,
        needs_review=needs_review,
        review_reason=review_reason,
        status=(
            RawJobClassificationSnapshot.Status.NEEDS_REVIEW
            if needs_review
            else (
                RawJobClassificationSnapshot.Status.MERGED
                if secondary_output
                else RawJobClassificationSnapshot.Status.PARTIAL
            )
        ),
    )

    return {
        "status": snapshot.status,
        "raw_job_id": raw_job_id,
        "input_hash": input_hash,
        "backend_run_id": backend_run.pk,
        "secondary_run_id": secondary_run.pk,
        "needs_review": needs_review,
        "final_confidence": final_confidence,
    }


def ingest_secondary_result_for_raw_job(
    *,
    raw_job_id: int,
    provider: str,
    prompt_version: str,
    confidence: float | None,
    normalized_output: dict,
    raw_output: dict | None = None,
) -> dict:
    if provider not in {
        RawJobClassifierRun.Provider.CODEX,
        RawJobClassifierRun.Provider.CLAUDE,
        RawJobClassifierRun.Provider.SECONDARY_STUB,
    }:
        raise ValueError("Unsupported secondary provider.")

    shadow_result = run_shadow_classification_for_raw_job(raw_job_id)
    raw_job = RawJob.objects.get(pk=raw_job_id)
    snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw_job)
    backend_run = snapshot.backend_run
    if not backend_run or backend_run.status != RawJobClassifierRun.Status.COMPLETED:
        raise ValueError("Backend classifier output is not available for comparison.")

    secondary_result = ProviderResult(
        provider=provider,
        provider_role=RawJobClassifierRun.ProviderRole.SECONDARY,
        provider_version="manual_ingest_v1",
        prompt_version=(prompt_version or "")[:40],
        confidence=confidence,
        raw_output=raw_output or normalized_output,
        normalized_output=normalized_output or {},
    )
    secondary_run = _create_run_record(
        raw_job,
        secondary_result,
        snapshot.current_input_hash,
        RawJobClassifierRun.Status.COMPLETED,
    )

    merged_output, conflicts, final_confidence, needs_review, review_reason = merge_outputs(
        backend_output=backend_run.normalized_output,
        secondary_output=normalized_output,
        backend_confidence=backend_run.confidence,
        secondary_confidence=confidence,
    )
    verifier_summary = verify_output(raw_job, merged_output)
    if verifier_summary["status"] == "fail":
        needs_review = True
        review_reason = review_reason or "verifier_failed"

    status = (
        RawJobClassificationSnapshot.Status.NEEDS_REVIEW
        if needs_review
        else RawJobClassificationSnapshot.Status.MERGED
    )
    snapshot = _persist_snapshot(
        raw_job=raw_job,
        snapshot=snapshot,
        input_hash=snapshot.current_input_hash,
        backend_run=backend_run,
        secondary_run=secondary_run,
        merged_output=merged_output,
        verifier_summary=verifier_summary,
        conflicts=conflicts,
        final_confidence=final_confidence,
        needs_review=needs_review,
        review_reason=review_reason,
        status=status,
    )
    return {
        "status": snapshot.status,
        "raw_job_id": raw_job_id,
        "secondary_run_id": secondary_run.pk,
        "needs_review": needs_review,
        "final_confidence": final_confidence,
        "shadow_status": shadow_result["status"],
    }


def approve_snapshot_for_raw_job(
    *,
    raw_job_id: int,
    source: str,
    actor,
    note: str = "",
    manual_output: dict | None = None,
    primary_role_slug: str = "",
    lock_primary_role: bool = False,
    primary_role_override_reason: str = "",
) -> dict:
    run_shadow_classification_for_raw_job(raw_job_id)
    raw_job = RawJob.objects.get(pk=raw_job_id)
    snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw_job)

    if source == "backend":
        if not snapshot.backend_run or snapshot.backend_run.status != RawJobClassifierRun.Status.COMPLETED:
            raise ValueError("Backend output is not available.")
        chosen_output = snapshot.backend_run.normalized_output or {}
        chosen_confidence = snapshot.backend_run.confidence or 0.0
        approval_state = RawJobClassificationSnapshot.ApprovalState.APPROVED
    elif source == "secondary":
        if not snapshot.secondary_run or snapshot.secondary_run.status != RawJobClassifierRun.Status.COMPLETED:
            raise ValueError("Secondary output is not available.")
        chosen_output = snapshot.secondary_run.normalized_output or {}
        chosen_confidence = snapshot.secondary_run.confidence or 0.0
        approval_state = RawJobClassificationSnapshot.ApprovalState.APPROVED
    elif source == "merged":
        if not snapshot.merged_output:
            raise ValueError("Merged output is not available.")
        chosen_output = snapshot.merged_output or {}
        chosen_confidence = snapshot.final_confidence or 0.0
        approval_state = RawJobClassificationSnapshot.ApprovalState.APPROVED
    elif source == "manual":
        chosen_output = manual_output or {}
        schema_errors = validate_canonical_output(chosen_output)
        if schema_errors:
            raise ValueError(" ; ".join(schema_errors[:4]))
        chosen_confidence = snapshot.final_confidence or 0.0
        approval_state = RawJobClassificationSnapshot.ApprovalState.OVERRIDDEN
    else:
        raise ValueError("Unsupported approval source.")

    primary_role_slug = (primary_role_slug or "").strip()
    if primary_role_slug and not MarketingRole.objects.filter(slug=primary_role_slug, is_active=True).exists():
        raise ValueError("Selected primary marketing role is not active.")

    primary_role_override_reason = (primary_role_override_reason or "").strip()
    preserved_locked_slug = ""
    preserved_locked_source = ""
    preserved_locked_reason = ""
    preserved_locked_at = None
    preserved_locked_by = None
    if snapshot.primary_role_locked and snapshot.approved_primary_role_slug:
        preserved_locked_slug = snapshot.approved_primary_role_slug
        preserved_locked_source = snapshot.primary_role_source or "manual_override"
        preserved_locked_reason = snapshot.primary_role_override_reason or ""
        preserved_locked_at = snapshot.primary_role_overridden_at
        preserved_locked_by = snapshot.primary_role_overridden_by

    if preserved_locked_slug and not primary_role_slug and not lock_primary_role:
        approved_primary_role_slug = preserved_locked_slug
        primary_role_source = preserved_locked_source or "manual_override"
        primary_role_locked = True
        primary_role_reason = preserved_locked_reason
        primary_role_overridden_at = preserved_locked_at
        primary_role_overridden_by = preserved_locked_by
    else:
        approved_primary_role_slug = _resolve_approved_primary_role_slug(
            raw_job=raw_job,
            chosen_output=chosen_output,
            requested_slug=primary_role_slug,
        )
        primary_role_locked = bool(lock_primary_role)
        if primary_role_slug:
            primary_role_source = "manual_override"
            primary_role_reason = primary_role_override_reason or note or "Manual primary role override."
            primary_role_overridden_at = timezone.now()
            primary_role_overridden_by = actor
        else:
            primary_role_source = "approved_snapshot" if approved_primary_role_slug else ""
            primary_role_reason = ""
            primary_role_overridden_at = None
            primary_role_overridden_by = None

    verifier_summary = verify_output(raw_job, chosen_output)
    ready_for_vetting = verifier_summary.get("status") != "fail"
    snapshot.approval_state = approval_state
    snapshot.approval_input_hash = compute_approval_input_hash(raw_job)
    snapshot.approval_is_stale = False
    snapshot.approval_stale_at = None
    snapshot.approved_output = chosen_output
    snapshot.approved_source = source
    snapshot.approved_primary_role_slug = approved_primary_role_slug
    snapshot.primary_role_source = primary_role_source
    snapshot.primary_role_locked = primary_role_locked
    snapshot.primary_role_override_reason = primary_role_reason
    snapshot.primary_role_overridden_at = primary_role_overridden_at
    snapshot.primary_role_overridden_by = primary_role_overridden_by
    snapshot.approval_note = note or ""
    snapshot.approved_at = timezone.now()
    snapshot.approved_by = actor
    snapshot.ready_for_vetting = ready_for_vetting
    snapshot.needs_review = False
    snapshot.review_reason = "" if ready_for_vetting else "approved_with_verifier_fail"
    snapshot.verifier_summary = verifier_summary
    snapshot.save(
        update_fields=[
            "approval_state",
            "approval_input_hash",
            "approval_is_stale",
            "approval_stale_at",
            "approved_output",
            "approved_source",
            "approved_primary_role_slug",
            "primary_role_source",
            "primary_role_locked",
            "primary_role_override_reason",
            "primary_role_overridden_at",
            "primary_role_overridden_by",
            "approval_note",
            "approved_at",
            "approved_by",
            "ready_for_vetting",
            "needs_review",
            "review_reason",
            "verifier_summary",
            "updated_at",
        ]
    )
    return {
        "raw_job_id": raw_job_id,
        "approved_source": source,
        "approval_state": snapshot.approval_state,
        "ready_for_vetting": ready_for_vetting,
        "confidence": round(float(chosen_confidence), 3),
        "approved_primary_role_slug": approved_primary_role_slug,
    }


def record_vetting_push_for_raw_job(
    *,
    raw_job_id: int,
    actor,
    job,
    note: str = "",
    pushed_with_warnings: bool = False,
) -> dict:
    with transaction.atomic():
        raw_job = RawJob.objects.select_for_update().get(pk=raw_job_id)
        snapshot = RawJobClassificationSnapshot.objects.select_for_update().get(raw_job=raw_job)
        if not snapshot.approved_output:
            raise ValueError("Approved classification is required before pushing to vetting.")

        if snapshot.pushed_job_id and snapshot.pushed_to_vetting_at:
            return {
                "raw_job_id": raw_job_id,
                "job_id": snapshot.pushed_job_id,
                "warning_count": len(snapshot.pushed_warning_codes or []),
                "pushed_with_warnings": bool(snapshot.pushed_to_vetting_with_warnings),
                "already_pushed": True,
            }

        locked_job = type(job).objects.select_for_update().get(pk=job.pk)
        warning_codes = list((snapshot.verifier_summary or {}).get("warnings") or [])
        pushed_at = timezone.now()
        snapshot.pushed_job = locked_job
        snapshot.pushed_to_vetting_at = pushed_at
        snapshot.pushed_to_vetting_by = actor
        snapshot.pushed_to_vetting_note = note or ""
        snapshot.pushed_to_vetting_with_warnings = bool(pushed_with_warnings)
        snapshot.pushed_warning_codes = warning_codes
        snapshot.save(
            update_fields=[
                "pushed_job",
                "pushed_to_vetting_at",
                "pushed_to_vetting_by",
                "pushed_to_vetting_note",
                "pushed_to_vetting_with_warnings",
                "pushed_warning_codes",
                "updated_at",
            ]
        )
        validation_result = dict(getattr(locked_job, "validation_result", {}) or {})
        dual_classification_meta = dict(validation_result.get("dual_classification") or {})
        dual_classification_meta.update(
            build_job_dual_classification_meta(
                raw_job,
                snapshot,
                pushed_with_warnings=bool(pushed_with_warnings),
                pushed_at=pushed_at,
                actor=actor,
                note=note or "",
            )
        )
        validation_result["dual_classification"] = dual_classification_meta
        locked_job.validation_result = validation_result
        locked_job.save(update_fields=["validation_result", "updated_at"])
        return {
            "raw_job_id": raw_job_id,
            "job_id": locked_job.pk,
            "warning_count": len(warning_codes),
            "pushed_with_warnings": bool(pushed_with_warnings),
            "already_pushed": False,
        }

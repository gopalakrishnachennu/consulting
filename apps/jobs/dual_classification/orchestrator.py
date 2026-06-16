from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from harvest.models import RawJob

from jobs.models import (
    RawJobClassificationConflict,
    RawJobClassificationSnapshot,
    RawJobClassifierRun,
)

from .merger import merge_outputs
from .providers import (
    BackendRulesProvider,
    ClassificationContext,
    ProviderResult,
    RuntimeLLMSecondaryProvider,
    SecondaryStubProvider,
)
from .schema import build_raw_job_input, compute_input_hash, validate_canonical_output
from .verifier import verify_output


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
        snapshot.current_input_hash = input_hash
        snapshot.backend_run = backend_run
        snapshot.secondary_run = secondary_run
        snapshot.merged_output = merged_output
        snapshot.verifier_summary = verifier_summary
        snapshot.final_confidence = final_confidence
        snapshot.needs_review = needs_review
        snapshot.review_reason = review_reason
        snapshot.ready_for_vetting = False
        snapshot.status = status
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

    verifier_summary = verify_output(raw_job, chosen_output)
    ready_for_vetting = verifier_summary.get("status") != "fail"
    snapshot.approval_state = approval_state
    snapshot.approved_output = chosen_output
    snapshot.approved_source = source
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
            "approved_output",
            "approved_source",
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
    }


def record_vetting_push_for_raw_job(
    *,
    raw_job_id: int,
    actor,
    job,
    note: str = "",
    pushed_with_warnings: bool = False,
) -> dict:
    raw_job = RawJob.objects.get(pk=raw_job_id)
    snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw_job)
    if not snapshot.approved_output:
        raise ValueError("Approved classification is required before pushing to vetting.")

    warning_codes = list((snapshot.verifier_summary or {}).get("warnings") or [])
    pushed_at = timezone.now()
    snapshot.pushed_job = job
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
    validation_result = dict(getattr(job, "validation_result", {}) or {})
    dual_classification_meta = dict(validation_result.get("dual_classification") or {})
    dual_classification_meta.update(
        {
            "approved_source": snapshot.approved_source or "",
            "approval_state": snapshot.approval_state,
            "pushed_to_vetting_at": pushed_at.isoformat(),
            "pushed_to_vetting_by": getattr(actor, "username", "") or "",
            "pushed_to_vetting_note": note or "",
            "pushed_to_vetting_with_warnings": bool(pushed_with_warnings),
            "warning_codes": warning_codes,
        }
    )
    validation_result["dual_classification"] = dual_classification_meta
    job.validation_result = validation_result
    job.save(update_fields=["validation_result", "updated_at"])
    return {
        "raw_job_id": raw_job_id,
        "job_id": getattr(job, "pk", None),
        "warning_count": len(warning_codes),
        "pushed_with_warnings": bool(pushed_with_warnings),
    }

from __future__ import annotations

from typing import Any

from .effective import effective_raw_job_classification


DOWNSTREAM_AUDIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("job_domain", "Job Domain"),
    ("job_category", "Job Category"),
    ("department_normalized", "Department"),
    ("country", "Country"),
    ("location_type", "Work Mode"),
    ("years_required", "Years Required"),
    ("skills", "Skills"),
)


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def build_job_dual_classification_meta(raw_job, snapshot, *, pushed_with_warnings=None, pushed_at=None, actor=None, note=None):
    snapshot = snapshot or type("SnapshotStub", (), {
        "approved_source": "",
        "approval_state": "UNREVIEWED",
        "ready_for_vetting": False,
        "pushed_to_vetting_with_warnings": False,
        "pushed_warning_codes": [],
        "verifier_summary": {"warnings": []},
        "pushed_to_vetting_at": None,
        "pushed_to_vetting_by": None,
        "pushed_to_vetting_note": "",
    })()
    effective = effective_raw_job_classification(raw_job)
    warnings = list(
        getattr(snapshot, "pushed_warning_codes", None)
        or ((getattr(snapshot, "verifier_summary", {}) or {}).get("warnings") or [])
    )
    skills = _as_list(effective.get("skills"))
    tech_stack = _as_list(effective.get("tech_stack"))

    approved_values = {
        "job_domain": effective.get("job_domain") or raw_job.job_domain or "",
        "job_category": effective.get("job_category") or raw_job.job_category or "",
        "department_normalized": effective.get("department_normalized") or raw_job.department_normalized or raw_job.department or "",
        "country": effective.get("country") or raw_job.country or "",
        "location_type": effective.get("location_type") or raw_job.location_type or "",
        "years_required": effective.get("years_required") if effective.get("years_required") is not None else raw_job.years_required,
        "skills": skills or _as_list(raw_job.skills)[:10],
        "tech_stack": tech_stack or _as_list(raw_job.tech_stack)[:10],
    }
    if pushed_with_warnings is None:
        pushed_with_warnings = bool(getattr(snapshot, "pushed_to_vetting_with_warnings", False))
    if pushed_at is None:
        pushed_at = getattr(snapshot, "pushed_to_vetting_at", None)
    if note is None:
        note = getattr(snapshot, "pushed_to_vetting_note", "") or ""
    pushed_by = getattr(actor, "username", "") or getattr(getattr(snapshot, "pushed_to_vetting_by", None), "username", "") or ""

    return {
        "approved_source": getattr(snapshot, "approved_source", "") or "raw_job",
        "approval_state": getattr(snapshot, "approval_state", "UNREVIEWED"),
        "ready_for_vetting": bool(getattr(snapshot, "ready_for_vetting", False)),
        "pushed_to_vetting_at": pushed_at.isoformat() if pushed_at else "",
        "pushed_to_vetting_by": pushed_by,
        "pushed_to_vetting_note": note,
        "pushed_to_vetting_with_warnings": bool(pushed_with_warnings),
        "warning_codes": warnings,
        "classification_source": getattr(raw_job, "classification_source", "") or getattr(snapshot, "approved_source", "") or "raw_job",
        "classification_provenance": getattr(raw_job, "classification_provenance", {}) or {},
        "field_provenance": getattr(raw_job, "field_provenance", {}) or {},
        "approved_values": approved_values,
        "source_raw_job_id": getattr(raw_job, "pk", None),
    }


def build_job_dual_classification_audit(job) -> dict:
    validation_result = dict(getattr(job, "validation_result", {}) or {})
    dual = dict(validation_result.get("dual_classification") or {})
    source_raw_job = getattr(job, "source_raw_job", None)

    classification_provenance = dict(dual.get("classification_provenance") or {})
    field_provenance = dict(dual.get("field_provenance") or {})
    approved_values = dict(dual.get("approved_values") or {})

    if source_raw_job is not None:
        if not classification_provenance:
            classification_provenance = getattr(source_raw_job, "classification_provenance", {}) or {}
        if not field_provenance:
            field_provenance = getattr(source_raw_job, "field_provenance", {}) or {}
        if not approved_values:
            approved_values = build_job_dual_classification_meta(
                source_raw_job,
                getattr(source_raw_job, "classification_snapshot", None) or type("SnapshotStub", (), {
                    "approved_source": dual.get("approved_source", ""),
                    "approval_state": dual.get("approval_state", "UNREVIEWED"),
                    "ready_for_vetting": dual.get("ready_for_vetting", False),
                    "pushed_to_vetting_with_warnings": dual.get("pushed_to_vetting_with_warnings", False),
                    "pushed_warning_codes": dual.get("warning_codes", []),
                    "verifier_summary": {"warnings": dual.get("warning_codes", [])},
                    "pushed_to_vetting_at": None,
                    "pushed_to_vetting_by": None,
                    "pushed_to_vetting_note": dual.get("pushed_to_vetting_note", ""),
                })(),
            ).get("approved_values", {})

    rows = []
    for key, label in DOWNSTREAM_AUDIT_FIELDS:
        value = approved_values.get(key, "—")
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value[:6]) if value else "—"
        elif value in (None, ""):
            value = "—"
        rows.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "source": field_provenance.get(key) or dual.get("approved_source") or dual.get("classification_source") or "raw_job",
            }
        )

    return {
        "present": bool(dual or source_raw_job),
        "meta": dual,
        "classification_provenance": classification_provenance,
        "field_provenance": field_provenance,
        "rows": rows,
    }

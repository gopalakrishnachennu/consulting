from __future__ import annotations

from copy import deepcopy
from typing import Any

from .schema import BACKEND_PRIORITY_PATHS, FIELD_PATHS, get_path, set_path


def _normalized_list(values: Any) -> list[Any]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _merge_list_values(backend_value: Any, secondary_value: Any) -> list[Any] | None:
    backend_list = _normalized_list(backend_value)
    secondary_list = _normalized_list(secondary_value)
    if not backend_list and not secondary_list:
        return None
    merged = list(backend_list)
    existing = {str(v).strip().lower() for v in backend_list}
    for value in secondary_list:
        key = str(value).strip().lower()
        if key not in existing:
            merged.append(value)
            existing.add(key)
    return merged


def merge_outputs(
    *,
    backend_output: dict[str, Any],
    secondary_output: dict[str, Any] | None,
    backend_confidence: float | None,
    secondary_confidence: float | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], float, bool, str]:
    merged = deepcopy(backend_output or {})
    conflicts: list[dict[str, Any]] = []
    needs_review = False
    review_reason = ""

    if not secondary_output:
        final_confidence = float(backend_confidence or 0.0)
        return merged, conflicts, round(final_confidence, 3), False, ""

    for path in FIELD_PATHS:
        backend_value = get_path(backend_output, path)
        secondary_value = get_path(secondary_output, path)
        if secondary_value in (None, "", [], {}):
            continue
        if backend_value in (None, "", [], {}):
            set_path(merged, path, secondary_value)
            conflicts.append(
                {
                    "field_path": path,
                    "backend_value": backend_value,
                    "secondary_value": secondary_value,
                    "resolved_value": secondary_value,
                    "resolution": "SECONDARY",
                    "severity": "INFO",
                    "note": "filled_blank_from_secondary",
                }
            )
            continue
        if backend_value == secondary_value:
            conflicts.append(
                {
                    "field_path": path,
                    "backend_value": backend_value,
                    "secondary_value": secondary_value,
                    "resolved_value": backend_value,
                    "resolution": "AGREED",
                    "severity": "INFO",
                    "note": "agreed",
                }
            )
            continue

        if path.startswith("skills."):
            merged_list = _merge_list_values(backend_value, secondary_value)
            backend_list = _normalized_list(backend_value)
            secondary_list = _normalized_list(secondary_value)
            overlap = {
                str(value).strip().lower()
                for value in backend_list
            }.intersection({str(value).strip().lower() for value in secondary_list})
            if merged_list is not None and (overlap or not backend_list or not secondary_list):
                set_path(merged, path, merged_list)
                conflicts.append(
                    {
                        "field_path": path,
                        "backend_value": backend_value,
                        "secondary_value": secondary_value,
                        "resolved_value": merged_list,
                        "resolution": "SECONDARY" if secondary_list and not backend_list else "AGREED",
                        "severity": "INFO" if overlap else "WARN",
                        "note": "merged_skill_lists",
                    }
                )
                continue

        if path in BACKEND_PRIORITY_PATHS:
            resolved = backend_value
            resolution = "BACKEND"
            severity = "WARN"
            note = "backend_priority_rule"
        else:
            secondary_conf = float(secondary_confidence or 0.0)
            backend_conf = float(backend_confidence or 0.0)
            if secondary_conf > backend_conf + 0.1:
                resolved = secondary_value
                resolution = "SECONDARY"
                severity = "WARN"
                note = "secondary_higher_confidence"
            else:
                resolved = backend_value
                resolution = "REVIEW"
                severity = "CRITICAL"
                note = "critical_conflict_needs_review"
                needs_review = True
                if not review_reason:
                    review_reason = path

        set_path(merged, path, resolved)
        conflicts.append(
            {
                "field_path": path,
                "backend_value": backend_value,
                "secondary_value": secondary_value,
                "resolved_value": resolved,
                "resolution": resolution,
                "severity": severity,
                "note": note,
            }
        )

    final_confidence = round(max(float(backend_confidence or 0.0), float(secondary_confidence or 0.0)), 3)
    return merged, conflicts, final_confidence, needs_review, review_reason

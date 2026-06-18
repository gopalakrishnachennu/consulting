from __future__ import annotations

from core.feature_flags import feature_enabled_for


FLAG_CLASSIFICATION_WORKSPACE_V2 = "employee_classification_workspace_v2"
FLAG_CLASSIFICATION_SETTINGS_V2 = "employee_classification_settings_v2"
FLAG_CLASSIFICATION_METRICS_V2 = "employee_classification_metrics_v2"
FLAG_LEGACY_RAWJOB_REVIEW_BRIDGE = "employee_legacy_rawjob_review_bridge"


def classification_workspace_v2_enabled(user) -> bool:
    return feature_enabled_for(user, FLAG_CLASSIFICATION_WORKSPACE_V2)


def classification_settings_v2_enabled(user) -> bool:
    return feature_enabled_for(user, FLAG_CLASSIFICATION_SETTINGS_V2)


def classification_metrics_v2_enabled(user) -> bool:
    return feature_enabled_for(user, FLAG_CLASSIFICATION_METRICS_V2)


def legacy_rawjob_review_bridge_enabled(user) -> bool:
    return feature_enabled_for(user, FLAG_LEGACY_RAWJOB_REVIEW_BRIDGE)

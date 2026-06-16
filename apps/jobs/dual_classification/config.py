from __future__ import annotations


def _platform_config():
    from core.models import PlatformConfig

    return PlatformConfig.load()


def shadow_enabled() -> bool:
    try:
        return bool(getattr(_platform_config(), "dual_classification_shadow_enabled", True))
    except Exception:
        return True


def require_approval_for_sync() -> bool:
    try:
        return bool(getattr(_platform_config(), "dual_classification_require_approval_for_sync", False))
    except Exception:
        return False


def allow_push_with_warnings() -> bool:
    try:
        return bool(getattr(_platform_config(), "dual_classification_allow_push_with_warnings", True))
    except Exception:
        return True


def backfill_batch_size() -> int:
    try:
        value = int(getattr(_platform_config(), "dual_classification_backfill_batch_size", 200) or 200)
    except Exception:
        value = 200
    return max(1, min(value, 5000))


def default_secondary_provider() -> str:
    try:
        return str(getattr(_platform_config(), "dual_classification_secondary_provider_default", "") or "").strip()
    except Exception:
        return ""


def secondary_runtime_enabled() -> bool:
    try:
        return bool(getattr(_platform_config(), "dual_classification_secondary_runtime_enabled", False))
    except Exception:
        return False


def secondary_prompt_version() -> str:
    try:
        value = str(getattr(_platform_config(), "dual_classification_secondary_prompt_version", "") or "").strip()
        return value or "runtime_v1"
    except Exception:
        return "runtime_v1"

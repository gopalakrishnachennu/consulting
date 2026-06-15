from __future__ import annotations

from config.constants.limits import MAX_UPLOAD_SIZE_MB as DEFAULT_MAX_UPLOAD_SIZE_MB
from config.constants.limits import SESSION_TIMEOUT_MINUTES as DEFAULT_SESSION_TIMEOUT_MINUTES


def _bounded_int(value, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def get_session_timeout_minutes() -> int:
    from .models import PlatformConfig

    config = PlatformConfig.load()
    return _bounded_int(
        getattr(config, "session_timeout_minutes", DEFAULT_SESSION_TIMEOUT_MINUTES),
        DEFAULT_SESSION_TIMEOUT_MINUTES,
        minimum=5,
        maximum=24 * 60,
    )


def get_session_timeout_seconds() -> int:
    return get_session_timeout_minutes() * 60


def get_max_upload_size_mb() -> int:
    from .models import PlatformConfig

    config = PlatformConfig.load()
    return _bounded_int(
        getattr(config, "max_upload_size_mb", DEFAULT_MAX_UPLOAD_SIZE_MB),
        DEFAULT_MAX_UPLOAD_SIZE_MB,
        minimum=1,
        maximum=100,
    )


def get_max_upload_size_bytes() -> int:
    return get_max_upload_size_mb() * 1024 * 1024

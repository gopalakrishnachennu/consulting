"""Phase 3: Redis-backed per-platform rate limiter.

Honors PlatformConfig.inter_request_delay_ms. One key per platform slug.
Fall-through to in-process sleep if Redis isn't reachable (never blocks deploys).

Usage:
    from harvest.rate_limiter import throttle
    throttle('workday')   # blocks just long enough to honor cadence
    # ... make HTTP request ...
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache

log = logging.getLogger(__name__)

_DEFAULT_DELAY_MS = 1500


def _delay_ms_for(platform_slug: str) -> int:
    """Return configured inter-request delay, with safe fallback."""
    if not platform_slug:
        return _DEFAULT_DELAY_MS
    try:
        from .models import JobBoardPlatform, PlatformConfig
        cfg = PlatformConfig.objects.filter(
            platform__slug=platform_slug, is_active=True
        ).only('inter_request_delay_ms').first()
        if cfg:
            return max(0, int(cfg.inter_request_delay_ms or 0))
        JobBoardPlatform.objects  # touch for import clarity
    except Exception:
        log.debug("rate_limiter: PlatformConfig lookup failed; using default", exc_info=True)
    return _DEFAULT_DELAY_MS


def throttle(platform_slug: str) -> None:
    """Block until at least `delay_ms` has elapsed since the last call for this slug."""
    delay_ms = _delay_ms_for(platform_slug)
    if delay_ms <= 0:
        return

    key = f"harvest:ratelimit:{platform_slug or 'default'}"
    now_ms = int(time.time() * 1000)
    try:
        last_ms = cache.get(key)
        if last_ms is not None:
            elapsed = now_ms - int(last_ms)
            wait_ms = delay_ms - elapsed
            if wait_ms > 0:
                time.sleep(wait_ms / 1000.0)
        cache.set(key, int(time.time() * 1000), timeout=max(delay_ms // 1000 * 4, 60))
    except Exception:
        log.debug("rate_limiter: cache unavailable; sleeping full delay", exc_info=True)
        time.sleep(delay_ms / 1000.0)

"""Shared selective-intake enforcement — STRONG phrase matches only."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .role_filter import STRONG, ClassifyResult

if TYPE_CHECKING:
    from .models import HarvestEngineConfig


def selective_enforcement_active(cfg: HarvestEngineConfig, *, fetch_all: bool = False) -> bool:
    """True when intake rules should drop rows before they reach RawJob."""
    if not getattr(cfg, "selective_filter_enabled", False):
        return False
    if fetch_all and not getattr(cfg, "filter_full_crawl", False):
        return False
    return True


def should_pre_storage_drop(
    cfg: HarvestEngineConfig,
    filter_result: ClassifyResult | None,
    *,
    title: str,
    enforcement_active: bool,
) -> bool:
    """
    Return True when a harvested job must NOT be written to RawJob.

    Selective intake is STRONG-only: the title must match an include phrase from
    Role Categories. COLD / POSSIBLE / NO_MATCH / UNKNOWN are never stored.
    """
    if not enforcement_active or filter_result is None:
        return False

    if not (title or "").strip():
        return True

    return filter_result.decision != STRONG


def ensure_selective_intake_enabled() -> bool:
    """
    Turn on selective intake enforcement when role categories exist.
    Returns True when config was changed.
    """
    from .models import HarvestEngineConfig, HarvestRoleCategory

    has_active_rules = any(
        bool(cat.include_phrases)
        for cat in HarvestRoleCategory.objects.filter(is_active=True).only("include_phrases")
    )
    if not has_active_rules:
        return False

    cfg = HarvestEngineConfig.get()
    if cfg.selective_filter_enabled:
        return False

    cfg.selective_filter_enabled = True
    cfg.save()
    return True

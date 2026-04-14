"""Centralized feature flag resolution and cache invalidation."""

from __future__ import annotations

from django.core.cache import cache

CACHE_KEY_ALL = 'feature_flags:all_v1'
CACHE_TTL = 300


def invalidate_feature_flag_cache() -> None:
    cache.delete(CACHE_KEY_ALL)


def _get_all_flags_map():
    data = cache.get(CACHE_KEY_ALL)
    if data is not None:
        return data
    from core.models import FeatureFlag

    flags = {f.key: f for f in FeatureFlag.objects.all()}
    cache.set(CACHE_KEY_ALL, flags, CACHE_TTL)
    return flags


def get_feature_flag(key: str):
    return _get_all_flags_map().get(key)


def feature_enabled_for(user, key: str) -> bool:
    """
    Runtime check: superuser and ADMIN always pass.
    Consultants: master switch + enabled_for_consultants.
    Employees: master + enabled_for_employees + designation M2M.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if getattr(user, 'role', None) == 'ADMIN':
        return True

    from users.models import User as UserModel
    from core.models import FeatureFlag

    flag = get_feature_flag(key)
    if flag is None:
        return True

    if not flag.is_enabled:
        return False

    role = getattr(user, 'role', None)

    if role == UserModel.Role.CONSULTANT:
        if flag.applies_to == FeatureFlag.AppliesTo.EMPLOYEE:
            return False
        if flag.applies_to == FeatureFlag.AppliesTo.SYSTEM:
            return False
        return bool(flag.enabled_for_consultants)

    if role == UserModel.Role.EMPLOYEE:
        if flag.applies_to == FeatureFlag.AppliesTo.CONSULTANT:
            return False
        if not flag.enabled_for_employees:
            return False
        ep = getattr(user, 'employee_profile', None)
        des = getattr(ep, 'designation', None) if ep else None
        if not des:
            from core.models import EmployeeDesignation

            des = EmployeeDesignation.objects.filter(slug='recruiter', is_active=True).first()
        if not des:
            return False
        return des.allowed_features.filter(pk=flag.pk).exists()

    return False


def consultant_public_feature_enabled(owner_user, key: str) -> bool:
    """
    For anonymous views of a consultant (e.g. public profile): evaluate flags as the profile owner.
    """
    if not owner_user or not owner_user.is_authenticated:
        return False
    if owner_user.is_superuser:
        return True
    if getattr(owner_user, 'role', None) == 'ADMIN':
        return True

    from users.models import User as UserModel
    from core.models import FeatureFlag

    if getattr(owner_user, 'role', None) != UserModel.Role.CONSULTANT:
        return False

    flag = get_feature_flag(key)
    if flag is None:
        return True
    if not flag.is_enabled:
        return False
    if flag.applies_to == FeatureFlag.AppliesTo.EMPLOYEE:
        return False
    if flag.applies_to == FeatureFlag.AppliesTo.SYSTEM:
        return False
    return bool(flag.enabled_for_consultants)

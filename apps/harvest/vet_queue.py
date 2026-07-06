"""Country scope for the vetting (POOL) queue — sync, gate, and UI."""

from __future__ import annotations

from django.db.models import Q

from harvest.location_resolver import COUNTRY_CODE_TO_NAME


def get_vet_queue_country_codes() -> list[str]:
    """ISO codes allowed into the vet queue for sync/gate. Empty = no extra country filter."""
    from harvest.models import HarvestEngineConfig, VetGateConfig

    vet_cfg = VetGateConfig.get()
    configured = vet_cfg.vet_queue_country_codes if isinstance(vet_cfg.vet_queue_country_codes, list) else []
    cleaned = [str(code).strip().upper() for code in configured if str(code).strip()]
    if cleaned:
        return cleaned
    return HarvestEngineConfig.get().get_target_countries()


def get_vet_queue_pool_country_codes() -> list[str]:
    """ISO codes for POOL UI/badge filtering. Only applies when explicitly configured."""
    from harvest.models import VetGateConfig

    vet_cfg = VetGateConfig.get()
    configured = vet_cfg.vet_queue_country_codes if isinstance(vet_cfg.vet_queue_country_codes, list) else []
    return [str(code).strip().upper() for code in configured if str(code).strip()]


def raw_job_matches_vet_queue_countries(raw_job, codes: list[str] | None = None) -> bool:
    codes = codes if codes is not None else get_vet_queue_country_codes()
    if not codes:
        return True
    primary = (getattr(raw_job, "country_code", "") or "").strip().upper()
    if primary and primary in codes:
        return True
    extra = getattr(raw_job, "country_codes", None)
    if isinstance(extra, list):
        for item in extra:
            code = str(item or "").strip().upper()
            if code in codes:
                return True
    return False


def raw_job_vet_country_q(codes: list[str] | None = None) -> Q:
    codes = codes if codes is not None else get_vet_queue_country_codes()
    if not codes:
        return Q()
    q = Q(country_code__in=codes)
    for code in codes:
        q |= Q(country_codes__contains=[code])
    return q


def vet_queue_job_queryset():
    """Active POOL jobs visible in the vetting queue (country-scoped)."""
    from jobs.models import Job

    qs = Job.objects.filter(status=Job.Status.POOL, is_archived=False)
    country_q = job_vet_pool_country_q(get_vet_queue_pool_country_codes())
    if country_q:
        qs = qs.filter(country_q)
    return qs


def job_vet_country_q(codes: list[str] | None = None) -> Q:
    """Filter POOL/live Job rows to allowed vet-queue countries."""
    codes = _normalize_country_codes(codes if codes is not None else get_vet_queue_country_codes())
    if not codes:
        return Q()
    q = Q(source_raw_job__country_code__in=codes)
    for code in codes:
        q |= Q(source_raw_job__country_codes__contains=[code])
        name = COUNTRY_CODE_TO_NAME.get(code.upper(), "")
        if name:
            q |= Q(country__iexact=name)
        q |= Q(country__iexact=code)
    return q


def _normalize_country_codes(codes: list[str]) -> list[str]:
    return [str(code).strip().upper() for code in codes if str(code).strip()]


def job_vet_pool_country_q(codes: list[str] | None = None) -> Q:
    """Stricter POOL filter: exclude explicit non-target countries on Job or RawJob."""
    codes = _normalize_country_codes(codes if codes is not None else get_vet_queue_pool_country_codes())
    if not codes:
        return Q()

    include = Q(source_raw_job__country_code__in=codes)
    for code in codes:
        name = COUNTRY_CODE_TO_NAME.get(code, "")
        if name:
            include |= Q(country__iexact=name)
        include |= Q(country__iexact=code)

    exclude = Q()
    for code, name in COUNTRY_CODE_TO_NAME.items():
        if code in codes:
            continue
        exclude |= Q(country__iexact=name) | Q(country__iexact=code)
        exclude |= Q(source_raw_job__country_code=code)

    # Unknown/blank location rows should not appear in a USA-only vet list.
    exclude |= (
        (Q(country="") | Q(country__isnull=True))
        & (Q(source_raw_job__isnull=True) | Q(source_raw_job__country_code=""))
    )

    return include & ~exclude

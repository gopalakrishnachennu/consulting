from __future__ import annotations

import hashlib

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from jobs.dedup import find_existing_job_by_url
from jobs.models import Job

from .job_descriptions import job_description_for_sync


def _sync_lock_id(raw_job) -> int:
    seed = "|".join(
        [
            "rawjob-to-pool",
            str(getattr(raw_job, "pk", "") or ""),
            str(getattr(raw_job, "url_hash", "") or ""),
            str(getattr(raw_job, "original_url", "") or ""),
        ]
    ).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big", signed=False)
    if value >= 2**63:
        value -= 2**64
    return value


def acquire_raw_job_sync_lock(raw_job) -> None:
    """
    Transaction-scoped cross-worker lock for RawJob -> Job promotion.

    Production uses PostgreSQL, so this becomes a real advisory lock there.
    On other backends the surrounding row lock still helps.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_sync_lock_id(raw_job)])


def find_existing_active_job_for_raw_job(raw_job):
    """
    Return the active Job already representing this RawJob, if any.
    """
    if getattr(raw_job, "pk", None):
        by_source = (
            Job.objects.filter(source_raw_job_id=raw_job.pk, is_archived=False)
            .order_by("created_at")
            .first()
        )
        if by_source:
            return by_source
    if getattr(raw_job, "url_hash", ""):
        by_hash = (
            Job.objects.filter(url_hash=raw_job.url_hash, is_archived=False)
            .order_by("created_at")
            .first()
        )
        if by_hash:
            return by_hash
    if getattr(raw_job, "original_url", ""):
        by_url = find_existing_job_by_url(raw_job.original_url)
        if by_url and not by_url.is_archived:
            return by_url
        if len(raw_job.original_url) <= 500:
            by_link = (
                Job.objects.filter(original_link=raw_job.original_url, is_archived=False)
                .order_by("created_at")
                .first()
            )
            if by_link:
                return by_link
    return None


def create_or_get_vetting_job_from_raw_job(
    raw_job,
    *,
    posted_by,
    job_location: str,
    job_country: str,
    mapped_department: str,
):
    """
    Idempotent RawJob -> vetting Job creation.

    Returns (job, created_new, locked_raw_job).
    """
    with transaction.atomic():
        locked_raw = (
            raw_job.__class__.objects.select_for_update()
            .select_related("company", "job_platform")
            .get(pk=raw_job.pk)
        )
        acquire_raw_job_sync_lock(locked_raw)

        existing = find_existing_active_job_for_raw_job(locked_raw)
        if existing:
            return existing, False, locked_raw

        platform_slug = locked_raw.platform_slug or (locked_raw.job_platform.slug if locked_raw.job_platform else "")
        try:
            job = Job.objects.create(
                title=(locked_raw.title or "")[:200],
                company=(locked_raw.company_name or (locked_raw.company.name if locked_raw.company else ""))[:200],
                company_obj=locked_raw.company,
                location=(job_location or "")[:200],
                description=job_description_for_sync(locked_raw),
                original_link=(locked_raw.original_url or "")[:500],
                salary_range=(locked_raw.salary_raw or "")[:100],
                job_type=(locked_raw.employment_type if locked_raw.employment_type and locked_raw.employment_type != "UNKNOWN" else "FULL_TIME")[:20],
                status=Job.Status.POOL,
                stage=Job.Stage.VETTED,
                stage_changed_at=timezone.now(),
                url_hash=locked_raw.url_hash or "",
                job_source=(f"HARVESTED_{platform_slug.upper()}" if platform_slug else "HARVESTED")[:100],
                posted_by=posted_by,
                source_raw_job=locked_raw,
                queue_entered_at=timezone.now(),
                country=(job_country or "")[:100],
                department=(mapped_department or "")[:20],
            )
        except IntegrityError:
            existing = find_existing_active_job_for_raw_job(locked_raw)
            if existing:
                return existing, False, locked_raw
            raise

        return job, True, locked_raw

"""
Local Harvesting Agent — Production-side receive API.

Three endpoints (all protected by Bearer token = settings.HARVEST_PUSH_SECRET):

  GET  /harvest/api/push/labels/   → Export company+label list for local harvester
  POST /harvest/api/push/jobs/     → Receive enriched RawJobs from local agent
  GET  /harvest/api/push/status/   → Stats on pushed vs synced jobs
"""

import hashlib
import json
import logging
from datetime import date, datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger("harvest.push_api")


# ── Auth ──────────────────────────────────────────────────────────────────────

def _check_token(request) -> bool:
    """Validate Authorization: Bearer <HARVEST_PUSH_SECRET>."""
    secret = getattr(settings, "HARVEST_PUSH_SECRET", "").strip()
    if not secret:
        logger.error("push_api: HARVEST_PUSH_SECRET not set — all push requests will be rejected")
        return False
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[len("Bearer "):].strip() == secret


def _unauth():
    return JsonResponse({"error": "Unauthorized — check HARVEST_PUSH_SECRET"}, status=401)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(val) -> "date | None":
    if not val:
        return None
    if isinstance(val, date):
        return val
    try:
        return date.fromisoformat(str(val)[:10])
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> "float | None":
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _resolve_company(company_name: str):
    """Find or create a Company stub by name. Returns Company or None."""
    if not company_name:
        return None
    try:
        from companies.models import Company
        company = Company.objects.filter(name__iexact=company_name.strip()).first()
        if company:
            return company
        company, _ = Company.objects.get_or_create(
            name=company_name.strip(),
            defaults={"name": company_name.strip()},
        )
        return company
    except Exception:
        logger.exception("push_api: company lookup failed for %r", company_name)
        return None


def _resolve_platform(slug: str):
    """Find JobBoardPlatform by slug. Returns instance or None."""
    if not slug:
        return None
    try:
        from harvest.models import JobBoardPlatform
        return JobBoardPlatform.objects.filter(slug=slug).first()
    except Exception:
        return None


def _trigger_pipeline():
    """Fire sync-to-pool task via Celery (best-effort, non-blocking)."""
    try:
        from harvest.tasks import sync_harvested_to_pool_task
        sync_harvested_to_pool_task.delay(max_jobs=500)
    except Exception:
        logger.warning("push_api: could not queue sync_harvested_to_pool_task", exc_info=True)


# ── View 1: Export labels ─────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class ExportLabelsView(View):
    """
    GET /harvest/api/push/labels/
    Returns the list of verified company+platform labels.
    The local harvesting agent uses this to know WHAT to harvest.

    Query params:
      platform  — filter by platform slug (optional)
      limit     — max results (default 2000)
    """

    def get(self, request):
        if not _check_token(request):
            return _unauth()

        from harvest.models import CompanyPlatformLabel

        qs = (
            CompanyPlatformLabel.objects
            .select_related("company", "platform")
            .filter(
                platform__is_enabled=True,
                company__isnull=False,
            )
            .exclude(tenant_id="")
        )

        platform_filter = request.GET.get("platform", "").strip()
        if platform_filter:
            qs = qs.filter(platform__slug=platform_filter)

        try:
            limit = min(int(request.GET.get("limit", 2000)), 5000)
        except (TypeError, ValueError):
            limit = 2000

        qs = qs[:limit]

        labels = []
        for lbl in qs:
            labels.append({
                "company_name": lbl.company.name,
                "career_url": lbl.custom_career_url or lbl.company.career_site_url or "",
                "domain": lbl.company.domain or "",
                "platform_slug": lbl.platform.slug if lbl.platform else "",
                "platform_api_type": lbl.platform.api_type if lbl.platform else "",
                "tenant_id": lbl.tenant_id,
                "is_verified": lbl.is_verified,
                "confidence": lbl.confidence,
            })

        return JsonResponse({"labels": labels, "count": len(labels)})


# ── View 2: Receive pushed jobs ───────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class PushJobsView(View):
    """
    POST /harvest/api/push/jobs/
    Receive a batch of enriched RawJobs from the local harvesting agent.

    Body (JSON):
      {
        "jobs": [...],           # list of serialised RawJob dicts (max 1000)
        "trigger_pipeline": true # queue sync-to-pool after insert (default true)
      }

    Response:
      {"received": N, "created": N, "skipped": N, "errors": N}
    """

    MAX_BATCH = 1000

    def post(self, request):
        if not _check_token(request):
            return _unauth()

        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "Invalid JSON body"}, status=400)

        jobs = payload.get("jobs", [])
        if not isinstance(jobs, list):
            return JsonResponse({"error": "'jobs' must be a JSON array"}, status=400)
        if len(jobs) > self.MAX_BATCH:
            return JsonResponse(
                {"error": f"Max {self.MAX_BATCH} jobs per request, got {len(jobs)}"},
                status=400,
            )

        trigger_pipeline = bool(payload.get("trigger_pipeline", True))

        created = skipped = errors = 0

        # Cache platform lookups to avoid N+1
        _platform_cache: dict[str, object] = {}

        def get_platform(slug):
            if slug not in _platform_cache:
                _platform_cache[slug] = _resolve_platform(slug)
            return _platform_cache[slug]

        from harvest.models import RawJob

        for job_data in jobs:
            try:
                url_hash = job_data.get("url_hash", "").strip()
                if not url_hash:
                    # Compute it here if the local agent forgot
                    original_url = job_data.get("original_url", "").strip()
                    if original_url:
                        url_hash = hashlib.sha256(original_url.encode("utf-8")).hexdigest()
                    else:
                        errors += 1
                        continue

                if RawJob.objects.filter(url_hash=url_hash).exists():
                    skipped += 1
                    continue

                company = _resolve_company(job_data.get("company_name", ""))
                if company is None:
                    logger.warning(
                        "push_api: skipping job %s — cannot resolve company %r",
                        url_hash, job_data.get("company_name"),
                    )
                    errors += 1
                    continue

                platform_slug = job_data.get("platform_slug", "").strip()
                platform = get_platform(platform_slug)

                rj = RawJob(
                    company=company,
                    job_platform=platform,
                    url_hash=url_hash,
                    external_id=str(job_data.get("external_id", ""))[:512],
                    original_url=str(job_data.get("original_url", ""))[:1024],
                    apply_url=str(job_data.get("apply_url", ""))[:1024],
                    title=str(job_data.get("title", ""))[:512],
                    company_name=str(job_data.get("company_name", ""))[:256],
                    department=str(job_data.get("department", ""))[:256],
                    team=str(job_data.get("team", ""))[:256],
                    location_raw=str(job_data.get("location_raw", ""))[:512],
                    city=str(job_data.get("city", ""))[:128],
                    state=str(job_data.get("state", ""))[:128],
                    country=str(job_data.get("country", ""))[:128],
                    postal_code=str(job_data.get("postal_code", ""))[:32],
                    location_type=job_data.get("location_type") or RawJob.LocationType.UNKNOWN,
                    is_remote=bool(job_data.get("is_remote", False)),
                    employment_type=job_data.get("employment_type") or RawJob.EmploymentType.UNKNOWN,
                    experience_level=job_data.get("experience_level") or RawJob.ExperienceLevel.UNKNOWN,
                    salary_min=_safe_float(job_data.get("salary_min")),
                    salary_max=_safe_float(job_data.get("salary_max")),
                    salary_currency=str(job_data.get("salary_currency", "USD"))[:8],
                    salary_period=str(job_data.get("salary_period", ""))[:16],
                    salary_raw=str(job_data.get("salary_raw", ""))[:256],
                    description=job_data.get("description", ""),
                    requirements=job_data.get("requirements", ""),
                    benefits=job_data.get("benefits", ""),
                    posted_date=_parse_date(job_data.get("posted_date")),
                    closing_date=_parse_date(job_data.get("closing_date")),
                    platform_slug=platform_slug[:64],
                    raw_payload=job_data.get("raw_payload") or {},
                    # Enrichment (pre-computed on local machine)
                    skills=job_data.get("skills") or [],
                    tech_stack=job_data.get("tech_stack") or [],
                    job_category=str(job_data.get("job_category", ""))[:64],
                    years_required=job_data.get("years_required"),
                    years_required_max=job_data.get("years_required_max"),
                    education_required=str(job_data.get("education_required", ""))[:12],
                    visa_sponsorship=job_data.get("visa_sponsorship"),
                    work_authorization=str(job_data.get("work_authorization", ""))[:64],
                    clearance_required=bool(job_data.get("clearance_required", False)),
                    salary_equity=bool(job_data.get("salary_equity", False)),
                    signing_bonus=bool(job_data.get("signing_bonus", False)),
                    relocation_assistance=bool(job_data.get("relocation_assistance", False)),
                    travel_required=str(job_data.get("travel_required", ""))[:64],
                    certifications=job_data.get("certifications") or [],
                    benefits_list=job_data.get("benefits_list") or [],
                    languages_required=job_data.get("languages_required") or [],
                    word_count=int(job_data.get("word_count", 0) or 0),
                    quality_score=_safe_float(job_data.get("quality_score")),
                    sync_status=RawJob.SyncStatus.PENDING,
                    is_active=True,
                )
                rj.save()
                created += 1

            except IntegrityError:
                # Race condition: another worker created the same url_hash between our check and save
                skipped += 1
            except Exception:
                logger.exception(
                    "push_api: unexpected error saving job %r", job_data.get("url_hash")
                )
                errors += 1

        if trigger_pipeline and created > 0:
            _trigger_pipeline()

        logger.info(
            "push_api: batch complete — received=%d created=%d skipped=%d errors=%d",
            len(jobs), created, skipped, errors,
        )

        return JsonResponse({
            "received": len(jobs),
            "created": created,
            "skipped": skipped,
            "errors": errors,
        })


# ── View 3: Push status ───────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class PushStatusView(View):
    """
    GET /harvest/api/push/status/
    Returns RawJob counts broken down by platform_slug and sync_status.
    Useful for monitoring local-agent push progress from the prod side.
    """

    def get(self, request):
        if not _check_token(request):
            return _unauth()

        from django.db.models import Count
        from harvest.models import RawJob

        by_platform = list(
            RawJob.objects
            .values("platform_slug", "sync_status")
            .annotate(count=Count("id"))
            .order_by("platform_slug", "sync_status")
        )

        totals = {
            "total": RawJob.objects.count(),
            "pending": RawJob.objects.filter(sync_status=RawJob.SyncStatus.PENDING).count(),
            "synced": RawJob.objects.filter(sync_status=RawJob.SyncStatus.SYNCED).count(),
            "skipped": RawJob.objects.filter(sync_status=RawJob.SyncStatus.SKIPPED).count(),
            "failed": RawJob.objects.filter(sync_status=RawJob.SyncStatus.FAILED).count(),
        }

        return JsonResponse({
            **totals,
            "by_platform_status": by_platform,
            "timestamp": timezone.now().isoformat(),
        })

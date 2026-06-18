from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from typing import List, Tuple

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from .models import Company, CompanyDoNotSubmit
from jobs.models import Job


LEGAL_SUFFIXES = (
    "inc",
    "inc.",
    "llc",
    "llc.",
    "ltd",
    "ltd.",
    "corp",
    "corp.",
    "co",
    "co.",
    "gmbh",
    "s.a.",
    "s.a",
)


def normalize_company_name(raw: str) -> str:
    """
    Normalize a raw company name:
    - strip legal suffixes (Inc, LLC, Corp, Ltd, Co, etc.)
    - collapse whitespace
    - title-case the result
    """
    if not raw:
        return ""
    name = raw.strip()
    # Remove common legal suffixes at the end
    lower = name.lower()
    for suffix in LEGAL_SUFFIXES:
        token = " " + suffix
        if lower.endswith(token):
            name = name[: -len(token)]
            break
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name.title()


def _norm_name(name: str) -> str:
    if not name:
        return ""
    name = name.lower()
    # Remove punctuation and collapse whitespace
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _tokenize(name: str) -> set[str]:
    name = _norm_name(name)
    return set(name.split()) if name else set()


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def normalize_domain(value: str) -> str:
    """
    Take a URL or bare hostname and return a canonical domain, e.g.:
    - https://www.google.com/ → google.com
    - amazon.com/jobs → amazon.com
    """
    if not value:
        return ""
    text = value.strip().lower()
    if not text:
        return ""
    # Ensure we have a scheme so regex can work reliably
    if not re.match(r"^[a-z]+://", text):
        text = "https://" + text
    m = re.search(r"//([^/]+)", text)
    if not m:
        return ""
    host = m.group(1)
    for prefix in ("www.", "careers.", "jobs."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host


def _extract_domain(url: str) -> str:
    return normalize_domain(url)


def find_potential_duplicate_companies(
    name: str,
    website: str | None = None,
    threshold: float = 0.7,
    limit: int = 5,
) -> List[Tuple[Company, float]]:
    """
    Rules-first duplicate detection for companies.
    Returns a list of (company, score) sorted by score (1.0 = perfect match).
    """
    name_tokens = _tokenize(name or "")
    domain = _extract_domain(website or "")

    qs = Company.objects.all()
    if name:
        qs = qs.filter(name__icontains=name) | qs.filter(alias__icontains=name)

    candidates: list[tuple[Company, float]] = []
    for company in qs[:50]:
        score = 0.0
        existing_tokens = _tokenize(company.name) | _tokenize(company.alias)
        score = _jaccard(name_tokens, existing_tokens)

        # Boost when domains match
        existing_domain = _extract_domain(company.website or "")
        if domain and existing_domain and domain == existing_domain:
            score = max(score, 0.9)

        if score >= threshold:
            candidates.append((company, score))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:limit]


@transaction.atomic
def merge_companies(source: Company, target: Company) -> None:
    """
    Merge source company into target.
    - Re-point jobs to target (and sync legacy text field).
    - Merge DoNotSubmit rules.
    - Aggregate simple counters.
    - Delete source.
    """
    if source.pk == target.pk:
        return

    # Jobs
    Job.objects.filter(company_obj=source).update(company_obj=target, company=target.name)

    # DoNotSubmit rules – avoid unique_together conflicts
    for dnd in CompanyDoNotSubmit.objects.filter(company=source):
        existing, created = CompanyDoNotSubmit.objects.get_or_create(
            company=target,
            consultant=dnd.consultant,
            defaults={"until": dnd.until, "reason": dnd.reason},
        )
        if not created:
            # Prefer the later until date and concatenate reasons
            if dnd.until and (not existing.until or dnd.until > existing.until):
                existing.until = dnd.until
            if dnd.reason and dnd.reason not in (existing.reason or ""):
                existing.reason = (existing.reason or "").strip()
                if existing.reason:
                    existing.reason += "\n"
                existing.reason += dnd.reason
            existing.save()
        dnd.delete()

    # Simple metric aggregation
    target.total_submissions += source.total_submissions
    target.total_interviews += source.total_interviews
    target.total_offers += source.total_offers
    target.total_placements += source.total_placements
    if source.last_activity_at and (
        not target.last_activity_at or source.last_activity_at > target.last_activity_at
    ):
        target.last_activity_at = source.last_activity_at
    target.save()

    # Finally, remove the source company
    source.delete()


def _company_platform_label(company: Company):
    try:
        return company.platform_label
    except ObjectDoesNotExist:
        return None


def company_raw_job_queryset(company: Company):
    from harvest.models import RawJob

    qs = RawJob.objects.filter(company=company)
    if qs.exists():
        return qs
    return RawJob.objects.filter(company_name__iexact=company.name)


def build_company_harvest_summary(company: Company) -> dict:
    from harvest.models import CompanyFetchRun

    label = _company_platform_label(company)
    default = {
        "label": label,
        "latest_run": None,
        "last_success": None,
        "last_failure": None,
        "success_rate_7d": None,
        "completed_runs_7d": 0,
        "status_counts_7d": {},
        "trend_7d": [],
        "board_url": "",
        "tenant_id": "",
        "platform_name": "",
        "platform_slug": "",
        "failed_runs_7d": 0,
        "success_runs_7d": 0,
        "empty_runs_7d": 0,
        "partial_runs_7d": 0,
    }
    if not label:
        return default

    runs_qs = CompanyFetchRun.objects.filter(label=label).select_related("label__platform", "batch").order_by("-started_at", "-id")
    latest_run = runs_qs.first()
    last_success = runs_qs.filter(status__in=[CompanyFetchRun.Status.SUCCESS, CompanyFetchRun.Status.PARTIAL]).first()
    last_failure = runs_qs.filter(status__in=[CompanyFetchRun.Status.FAILED, CompanyFetchRun.Status.SKIPPED]).first()

    since_7d = timezone.now() - timedelta(days=7)
    recent_runs = list(runs_qs.filter(started_at__gte=since_7d))
    completed_runs = [run for run in recent_runs if run.status not in {CompanyFetchRun.Status.PENDING, CompanyFetchRun.Status.RUNNING}]
    success_runs = [run for run in completed_runs if run.status in {CompanyFetchRun.Status.SUCCESS, CompanyFetchRun.Status.PARTIAL}]
    status_counts = Counter(run.status for run in completed_runs)

    today = timezone.localdate()
    trend = []
    for days_back in range(6, -1, -1):
        day = today - timedelta(days=days_back)
        day_runs = [run for run in recent_runs if run.started_at and timezone.localtime(run.started_at).date() == day]
        day_completed = [run for run in day_runs if run.status not in {CompanyFetchRun.Status.PENDING, CompanyFetchRun.Status.RUNNING}]
        trend.append(
            {
                "date": day,
                "label": day.strftime("%b %d"),
                "total": len(day_completed),
                "ok": sum(1 for run in day_completed if run.status in {CompanyFetchRun.Status.SUCCESS, CompanyFetchRun.Status.PARTIAL}),
                "bad": sum(1 for run in day_completed if run.status in {CompanyFetchRun.Status.FAILED, CompanyFetchRun.Status.SKIPPED}),
                "empty": sum(1 for run in day_completed if run.status == CompanyFetchRun.Status.EMPTY),
            }
        )

    completed_count = len(completed_runs)
    success_rate = round((len(success_runs) / completed_count) * 100) if completed_count else None

    return {
        "label": label,
        "latest_run": latest_run,
        "last_success": last_success,
        "last_failure": last_failure,
        "success_rate_7d": success_rate,
        "completed_runs_7d": completed_count,
        "status_counts_7d": dict(status_counts),
        "trend_7d": trend,
        "board_url": label.custom_career_url or label.career_page_url or company.career_site_url or company.website or "",
        "tenant_id": label.tenant_id or "",
        "platform_name": label.platform.name if label.platform else "",
        "platform_slug": label.platform.slug if label.platform else "",
        "failed_runs_7d": status_counts.get(CompanyFetchRun.Status.FAILED, 0) + status_counts.get(CompanyFetchRun.Status.SKIPPED, 0),
        "success_runs_7d": status_counts.get(CompanyFetchRun.Status.SUCCESS, 0),
        "empty_runs_7d": status_counts.get(CompanyFetchRun.Status.EMPTY, 0),
        "partial_runs_7d": status_counts.get(CompanyFetchRun.Status.PARTIAL, 0),
    }


def build_company_recent_fetch_runs(company: Company, limit: int = 10) -> list:
    from harvest.models import CompanyFetchRun

    label = _company_platform_label(company)
    if not label:
        return []
    return list(
        CompanyFetchRun.objects.filter(label=label)
        .select_related("label__platform", "batch")
        .order_by("-started_at", "-id")[:limit]
    )


def build_company_pipeline_snapshot(company: Company) -> dict:
    from harvest.models import RawJob

    raw_qs = company_raw_job_queryset(company)
    raw_agg = raw_qs.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(sync_status=RawJob.SyncStatus.PENDING)),
        synced=Count("id", filter=Q(sync_status=RawJob.SyncStatus.SYNCED)),
        failed=Count("id", filter=Q(sync_status=RawJob.SyncStatus.FAILED)),
        duplicate=Count("id", filter=Q(sync_status=RawJob.SyncStatus.DUPLICATE)),
        inactive=Count("id", filter=Q(is_active=False)),
        classified=Count("id", filter=Q(classification_snapshot__isnull=False)),
        needs_review=Count("id", filter=Q(classification_snapshot__needs_review=True)),
        fresh_week=Count("id", filter=Q(fetched_at__gte=timezone.now() - timedelta(days=7))),
    )
    job_qs = Job.objects.filter(company_obj=company)
    job_agg = job_qs.aggregate(
        total=Count("id"),
        pool=Count("id", filter=Q(status=Job.Status.POOL)),
        open=Count("id", filter=Q(status=Job.Status.OPEN)),
        closed=Count("id", filter=Q(status=Job.Status.CLOSED)),
        draft=Count("id", filter=Q(status=Job.Status.DRAFT)),
        live=Count("id", filter=Q(stage=Job.Stage.LIVE)),
        vetted=Count("id", filter=Q(stage=Job.Stage.VETTED)),
        archived=Count("id", filter=Q(stage=Job.Stage.ARCHIVED) | Q(is_archived=True)),
    )

    freshness_days = None
    latest_raw = raw_qs.order_by("-fetched_at").first()
    if latest_raw and latest_raw.fetched_at:
        freshness_days = (timezone.now() - latest_raw.fetched_at).days

    return {
        "raw_total": raw_agg["total"] or 0,
        "raw_pending": raw_agg["pending"] or 0,
        "raw_synced": raw_agg["synced"] or 0,
        "raw_failed": raw_agg["failed"] or 0,
        "raw_duplicate": raw_agg["duplicate"] or 0,
        "raw_inactive": raw_agg["inactive"] or 0,
        "classified": raw_agg["classified"] or 0,
        "needs_review": raw_agg["needs_review"] or 0,
        "new_this_week": raw_agg["fresh_week"] or 0,
        "job_total": job_agg["total"] or 0,
        "job_pool": job_agg["pool"] or 0,
        "job_open": job_agg["open"] or 0,
        "job_closed": job_agg["closed"] or 0,
        "job_draft": job_agg["draft"] or 0,
        "job_live": job_agg["live"] or 0,
        "job_vetted": job_agg["vetted"] or 0,
        "job_archived": job_agg["archived"] or 0,
        "freshness_days": freshness_days,
    }


def build_company_data_completeness(company: Company, *, label=None, harvest_summary=None) -> dict:
    label = label if label is not None else _company_platform_label(company)
    harvest_summary = harvest_summary or {}
    checks = [
        ("Website", bool(company.website)),
        ("LinkedIn", bool(company.linkedin_url)),
        ("HQ", bool(company.hq_location)),
        ("Industry", bool(company.industry)),
        ("Headcount", bool(company.headcount_range)),
        ("Career Site", bool(company.career_site_url)),
        ("ATS Mapping", bool(label and label.platform_id)),
        ("Recent Success", bool(harvest_summary.get("last_success"))),
    ]
    present = sum(1 for _, ok in checks if ok)
    total = len(checks)
    return {
        "score_pct": round((present / total) * 100) if total else 0,
        "present": present,
        "total": total,
        "checks": [{"label": label_text, "ok": ok} for label_text, ok in checks],
        "missing": [label_text for label_text, ok in checks if not ok],
    }


def build_company_link_health(company: Company, *, label=None) -> dict:
    label = label if label is not None else _company_platform_label(company)

    def item(name, url, state, checked_at, detail=""):
        return {
            "name": name,
            "url": url,
            "state": state,
            "checked_at": checked_at,
            "detail": detail,
        }

    website_state = "missing"
    if company.website:
        website_state = "live" if company.website_is_valid else "down"

    linkedin_state = "missing"
    if company.linkedin_url:
        linkedin_state = "live" if company.linkedin_is_valid else "down"

    career_state = "missing"
    if company.career_site_url:
        career_state = "configured"

    ats_url = ""
    ats_state = "missing"
    ats_checked = None
    ats_detail = ""
    if label:
        ats_url = label.custom_career_url or label.career_page_url or ""
        ats_checked = label.portal_last_verified or label.last_checked_at
        if label.portal_alive is True:
            ats_state = "live"
        elif label.portal_alive is False:
            ats_state = "down"
        elif label.platform_id or ats_url:
            ats_state = "unchecked"
        ats_detail = label.platform.name if label.platform else ""

    return {
        "items": [
            item("Website", company.website, website_state, company.website_last_checked_at),
            item("LinkedIn", company.linkedin_url, linkedin_state, company.linkedin_last_checked_at),
            item("Career Site", company.career_site_url, career_state, None),
            item("ATS Board", ats_url, ats_state, ats_checked, ats_detail),
        ]
    }


def build_company_warnings(company: Company, *, label=None, harvest_summary=None, pipeline_snapshot=None, completeness=None) -> list[dict]:
    label = label if label is not None else _company_platform_label(company)
    harvest_summary = harvest_summary or {}
    pipeline_snapshot = pipeline_snapshot or {}
    completeness = completeness or {}
    warnings = []

    def add(code: str, severity: str, title: str, body: str):
        warnings.append({"code": code, "severity": severity, "title": title, "body": body})

    if company.needs_review:
        add("duplicate_review", "high", "Duplicate review pending", "This company is flagged for duplicate review and should be reconciled before more manual cleanup.")
    if company.is_blacklisted:
        add("blacklisted", "high", "Blacklisted company", company.blacklist_reason or "New submissions should remain blocked.")
    if not label:
        add("ats_unlabeled", "high", "ATS mapping missing", "No platform label is attached to this company yet, so harvest health cannot be trusted.")
    elif label.detection_method == label.DetectionMethod.UNDETECTED:
        add("ats_undetected", "medium", "No ATS detected", "The current company mapping could not identify a supported platform.")
    else:
        if label.portal_alive is False:
            add("portal_down", "high", "Career portal looks down", "The last portal health check failed. Re-verify before relying on harvest results.")
        if not label.tenant_id:
            add("missing_tenant", "medium", "ATS tenant missing", "The platform is known, but tenant or board identity is still missing.")
        if label.confidence in {label.Confidence.LOW, label.Confidence.UNKNOWN}:
            add("low_confidence", "medium", "ATS mapping confidence is low", "The company-to-platform mapping should be manually reviewed.")

    last_success = harvest_summary.get("last_success")
    if not last_success:
        add("no_success", "high", "No successful harvest found", "This company has no recent successful company fetch run.")
    else:
        age_days = (timezone.now() - last_success.started_at).days if last_success.started_at else None
        if age_days is not None and age_days >= 14:
            add("stale_success", "medium", "Harvest success is stale", f"The last successful fetch was {age_days} days ago.")

    if (harvest_summary.get("failed_runs_7d") or 0) >= 3:
        add("failed_streak", "high", "Repeated failed harvests", "Three or more failed or skipped company fetch runs were recorded in the last 7 days.")
    if (harvest_summary.get("empty_runs_7d") or 0) >= 3:
        add("empty_streak", "medium", "Repeated zero-yield harvests", "Recent company fetch runs keep completing with zero new jobs.")
    if (pipeline_snapshot.get("raw_duplicate") or 0) >= 5 and (pipeline_snapshot.get("new_this_week") or 0) == 0:
        add("duplicate_only", "medium", "Only duplicates recently", "Recent harvest output appears to be mostly duplicate jobs, not new inventory.")

    missing = list(completeness.get("missing") or [])
    if missing:
        add("data_gaps", "low", "Company profile still has data gaps", "Missing: " + ", ".join(missing[:6]) + ("." if len(missing) <= 6 else ", and more."))

    return warnings


def build_company_role_signals(company: Company, *, limit: int = 100) -> dict:
    from jobs.dual_classification.effective import effective_raw_job_classification

    raw_jobs = list(company_raw_job_queryset(company).select_related("classification_snapshot").order_by("-fetched_at")[:limit])
    domains = Counter()
    categories = Counter()
    departments = Counter()
    countries = Counter()
    work_modes = Counter()

    for raw_job in raw_jobs:
        effective = effective_raw_job_classification(raw_job)
        domain = (effective.get("job_domain") or raw_job.job_domain or "").strip()
        category = (effective.get("job_category") or raw_job.job_category or "").strip()
        department = (effective.get("department_normalized") or raw_job.department_normalized or raw_job.department or "").strip()
        country = (effective.get("country") or raw_job.country or "").strip()
        work_mode = (effective.get("location_type") or raw_job.location_type or "").strip()
        if domain:
            domains[domain] += 1
        if category:
            categories[category] += 1
        if department:
            departments[department] += 1
        if country:
            countries[country] += 1
        if work_mode:
            work_modes[work_mode] += 1

    def top_rows(counter: Counter, *, top_n: int = 4):
        return [{"label": label, "count": count} for label, count in counter.most_common(top_n)]

    return {
        "sample_size": len(raw_jobs),
        "top_domains": top_rows(domains),
        "top_categories": top_rows(categories),
        "top_departments": top_rows(departments),
        "top_countries": top_rows(countries),
        "work_modes": top_rows(work_modes),
    }


def build_company_action_panel(company: Company, *, label=None) -> dict:
    from urllib.parse import quote

    label = label if label is not None else _company_platform_label(company)
    company_query = quote(company.name)
    label_search_url = f"{reverse('harvest-labels')}?q={company_query}"
    return {
        "jobs_url": f"{reverse('job-list')}?company={company.pk}",
        "raw_jobs_url": f"{reverse('harvest-rawjobs')}?company_id={company.pk}",
        "pipeline_url": f"{reverse('jobs-pipeline')}?tab=raw&search_by=company&q={company_query}",
        "failed_runs_url": f"{reverse('company-detail', kwargs={'pk': company.pk})}#recent-fetch-runs",
        "mapping_url": label_search_url,
        "labels_url": label_search_url,
        "fetch_label_pk": getattr(label, "pk", None),
        "can_fetch_now": bool(label and label.platform_id and label.tenant_id),
    }


def build_company_pipeline_performance(company: Company, *, funnel=None, harvest_summary=None, pipeline_snapshot=None) -> dict:
    funnel = funnel or {}
    harvest_summary = harvest_summary or {}
    pipeline_snapshot = pipeline_snapshot or {}
    success_runs_total = (harvest_summary.get("status_counts_7d", {}).get("SUCCESS", 0) or 0) + (harvest_summary.get("status_counts_7d", {}).get("PARTIAL", 0) or 0)
    avg_jobs_per_success = None
    if success_runs_total:
        avg_jobs_per_success = round((pipeline_snapshot.get("new_this_week") or 0) / success_runs_total, 1)
    return {
        "fill_rate_pct": funnel.get("offer_rate_pct"),
        "interview_rate_pct": funnel.get("interview_rate_pct"),
        "freshness_days": pipeline_snapshot.get("freshness_days"),
        "avg_jobs_per_successful_harvest": avg_jobs_per_success,
    }

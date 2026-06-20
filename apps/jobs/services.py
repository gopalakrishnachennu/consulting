from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import List

from django.utils import timezone
from django.db.models import Count, Min, Q

from .models import Job
from resumes.services import LLMService
from users.models import ConsultantProfile
from django.db.models import Q
from submissions.models import ApplicationSubmission
from harvest.enrichments import infer_country_from_location
from resumes.prompt_strings import JD_PARSER_SYSTEM_PROMPT, JD_PARSER_USER_PROMPT
from .routing import effective_routing_profile

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a","an","and","are","as","at","be","by","for","from","has","have","in","is","it","its","of","on","or","that",
    "the","to","with","will","you","your","we","our","they","their","this","these","those",
}


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokenize(s: str) -> set[str]:
    s = _norm_text(s)
    tokens = set(re.findall(r"[a-z0-9][a-z0-9\+\.\#\-]{1,}", s))
    return {t for t in tokens if t not in _STOPWORDS and len(t) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


_ACTIVE_SUBMISSION_STATUSES = {
    ApplicationSubmission.Status.IN_PROGRESS,
    ApplicationSubmission.Status.APPLIED,
    ApplicationSubmission.Status.INTERVIEW,
    ApplicationSubmission.Status.OFFER,
    ApplicationSubmission.Status.PLACED,
}


def _job_country(job: Job) -> str:
    routing = effective_routing_profile(job)
    country_labels = routing.get("country_labels") or []
    if country_labels:
        return str(country_labels[0]).strip()
    direct = (getattr(job, "country", "") or "").strip()
    if direct:
        return direct
    return infer_country_from_location(getattr(job, "location", "") or "") or ""


def _job_seniority_bucket(job: Job) -> str:
    routing = effective_routing_profile(job)
    routed = (routing.get("seniority_primary") or getattr(job, "routing_seniority", "") or "").strip().lower()
    if routed and routed != "unknown":
        return routed
    title = (job.title or "").lower()
    if re.search(r"\b(intern|entry\s*level|junior|jr\.?)\b", title):
        return "junior"
    if re.search(r"\b(principal|staff|distinguished|director|vp|head of|chief)\b", title):
        return "executive"
    if re.search(r"\b(lead|manager)\b", title):
        return "lead"
    if re.search(r"\b(senior|sr\.?)\b", title):
        return "senior"
    return "mid"


def _job_country_match(job: Job, consultant: ConsultantProfile) -> bool:
    work_countries = {str(c).strip().lower() for c in (consultant.work_countries or []) if str(c).strip()}
    if not work_countries:
        return True
    routing = effective_routing_profile(job)
    country_labels = {str(c).strip().lower() for c in (routing.get("country_labels") or []) if str(c).strip()}
    country_codes = {str(c).strip().lower() for c in (routing.get("country_codes") or []) if str(c).strip()}
    job_country = (_job_country(job) or "").strip().lower()
    if job_country:
        country_labels.add(job_country)
    normalized_country_tokens = set(country_labels) | set(country_codes)
    return not normalized_country_tokens or bool(work_countries & normalized_country_tokens)


def _routing_config():
    from core.models import PlatformConfig

    return PlatformConfig.load()


def _consultant_country_tokens(values) -> set[str]:
    return {str(c).strip().lower() for c in (values or []) if str(c).strip()}


def _job_routing_country_tokens(job: Job) -> set[str]:
    routing = effective_routing_profile(job)
    country_labels = {str(c).strip().lower() for c in (routing.get("country_labels") or []) if str(c).strip()}
    country_codes = {str(c).strip().lower() for c in (routing.get("country_codes") or []) if str(c).strip()}
    job_country = (_job_country(job) or "").strip().lower()
    if job_country:
        country_labels.add(job_country)
    return country_labels | country_codes


def _job_work_authorization_match(job: Job, consultant: ConsultantProfile) -> bool:
    cfg = _routing_config()
    if not getattr(cfg, "routing_enforce_work_authorization", True):
        return True

    routing = effective_routing_profile(job)
    requires_sponsorship = getattr(consultant, "requires_visa_sponsorship", None)
    if routing.get("visa_sponsorship") is False and requires_sponsorship is True:
        return False

    work_auth_category = str(routing.get("work_auth_category") or getattr(job, "routing_work_auth_category", "")).strip().lower()
    visa_status = str(getattr(consultant, "visa_status", "") or "").strip().lower()
    if work_auth_category == "citizen_only" and not any(token in visa_status for token in {"citizen", "usc", "us citizen"}):
        return False
    if work_auth_category == "gc_or_citizen" and not any(
        token in visa_status for token in {"citizen", "green card", "permanent resident", "gc"}
    ):
        return False
    if work_auth_category == "opt_only" and "opt" not in visa_status:
        return False
    if work_auth_category == "h1b_transfer" and "h1b" not in visa_status:
        return False

    job_country_tokens = _job_routing_country_tokens(job)
    consultant_auth_tokens = _consultant_country_tokens(getattr(consultant, "work_authorization_countries", []))
    consultant_citizenship_tokens = _consultant_country_tokens(getattr(consultant, "citizenship_countries", []))
    eligible_country_tokens = consultant_auth_tokens | consultant_citizenship_tokens
    if job_country_tokens and eligible_country_tokens and not (job_country_tokens & eligible_country_tokens):
        return False

    return True


def _employment_preferences_match(job: Job, consultant: ConsultantProfile) -> bool:
    cfg = _routing_config()
    if not getattr(cfg, "routing_enforce_employment_preferences", True):
        return True

    preferences = {
        str(value).strip().lower().replace(" ", "_")
        for value in (getattr(consultant, "employment_preferences", []) or [])
        if str(value).strip()
    }
    if not preferences:
        return True

    routing = effective_routing_profile(job)
    employment_type = str(routing.get("employment_type") or "").strip().lower().replace(" ", "_")
    constraints = {str(value).strip().lower() for value in (routing.get("contract_constraints") or []) if str(value).strip()}
    employment_terms = {
        str(value).strip().lower().replace(" ", "_")
        for value in (routing.get("employment_terms") or getattr(job, "routing_employment_terms", []) or [])
        if str(value).strip()
    }

    if "w2 only" in constraints and "w2" not in preferences:
        return False
    if "no c2c" in constraints and preferences == {"c2c"}:
        return False
    if "no third party" in constraints and preferences <= {"c2c", "1099"}:
        return False
    if employment_type and employment_type not in {"unknown", ""}:
        normalized_map = {
            "full_time": {"full_time", "permanent", "fte"},
            "contract": {"contract", "c2c", "1099", "w2"},
            "full-time": {"full_time", "permanent", "fte"},
        }
        accepted = normalized_map.get(employment_type, {employment_type})
        if preferences.isdisjoint(accepted):
            return False
    if employment_terms and preferences.isdisjoint(employment_terms) and "contract" not in employment_terms:
        return False

    return True


def _work_mode_match(job: Job, consultant: ConsultantProfile) -> bool:
    cfg = _routing_config()
    if not getattr(cfg, "routing_enforce_work_mode", False):
        return True

    preferred_work_modes = {
        str(value).strip().lower()
        for value in (getattr(consultant, "preferred_work_modes", []) or [])
        if str(value).strip()
    }
    if not preferred_work_modes:
        return True

    work_mode = str((effective_routing_profile(job).get("work_mode") or "")).strip().lower()
    return not work_mode or work_mode == "unknown" or work_mode in preferred_work_modes


def _clearance_match(job: Job, consultant: ConsultantProfile) -> bool:
    cfg = _routing_config()
    if not getattr(cfg, "routing_enforce_clearance", True):
        return True
    routing = effective_routing_profile(job)
    return not bool(routing.get("clearance_required")) or bool(getattr(consultant, "clearance_eligible", False))


def consultant_job_routing_audit(job: Job, consultant: ConsultantProfile) -> dict:
    consultant_roles = set(consultant.marketing_roles.values_list("id", flat=True))
    job_roles = set(job.marketing_roles.values_list("id", flat=True))
    preferred_seniority = {
        str(level).strip().lower()
        for level in (consultant.preferred_seniority_levels or [])
        if str(level).strip()
    }
    cfg = _routing_config()
    routing_status = (getattr(job, "routing_status", "") or "").strip()
    parsed_jd_ok = bool(getattr(job, "parsed_jd", None)) and (getattr(job, "parsed_jd_status", "") or "").upper() == "OK"

    checks = [
        ("role", bool(consultant_roles and job_roles and consultant_roles & job_roles), "Marketing role overlap missing."),
        ("country", (not getattr(cfg, "routing_enforce_country_match", True)) or _job_country_match(job, consultant), "Country preference mismatch."),
        ("seniority", (not getattr(cfg, "routing_enforce_seniority_match", True)) or (not preferred_seniority) or (_job_seniority_bucket(job) in preferred_seniority), "Seniority preference mismatch."),
        ("work_auth", _job_work_authorization_match(job, consultant), "Visa or work authorization mismatch."),
        ("employment", _employment_preferences_match(job, consultant), "Employment preference mismatch."),
        ("work_mode", _work_mode_match(job, consultant), "Work mode mismatch."),
        ("clearance", _clearance_match(job, consultant), "Clearance requirement mismatch."),
        ("claimed", not _job_claimed_by_other(job, consultant), "Job is already claimed by another active submission."),
        ("routing", routing_status not in {Job.RoutingStatus.REVIEW, Job.RoutingStatus.FAILED, Job.RoutingStatus.PENDING}, "Job routing still needs review."),
        ("parsed_jd", parsed_jd_ok, "Parsed JD is missing or failed."),
    ]
    passed = [code for code, ok, _msg in checks if ok]
    blocked = [{"code": code, "message": msg} for code, ok, msg in checks if not ok]
    detail = consultant_job_match_detail(job, consultant) if not blocked else {
        "raw_score": 0,
        "match_pct": 0,
        "matched_required": 0,
        "total_required": 0,
        "required_skills": [],
    }
    return {
        "job": job,
        "eligible": not blocked,
        "passed_checks": passed,
        "blocked_reasons": blocked,
        **detail,
    }


def consultant_job_routing_audit_rows(
    consultant: ConsultantProfile,
    *,
    limit_eligible: int = 8,
    limit_blocked: int = 8,
    scan_limit: int = 200,
):
    consultant_role_ids = list(consultant.marketing_roles.values_list("id", flat=True))
    if not consultant_role_ids:
        return [], [], []
    qs = (
        Job.objects.filter(status=Job.Status.OPEN, is_archived=False, marketing_roles__in=consultant_role_ids)
        .distinct()
        .prefetch_related("marketing_roles")
        .order_by("-created_at")[: max(scan_limit, limit_eligible + limit_blocked)]
    )
    eligible_rows: list[dict] = []
    blocked_rows: list[dict] = []
    reason_counts: Counter[str] = Counter()
    for job in qs:
        audit = consultant_job_routing_audit(job, consultant)
        if audit["eligible"]:
            eligible_rows.append(audit)
        else:
            blocked_rows.append(audit)
            for reason in audit["blocked_reasons"]:
                reason_counts[reason["code"]] += 1
    eligible_rows.sort(key=lambda row: (-row["match_pct"], -row["raw_score"], -(row["job"].pk or 0)))
    blocked_rows.sort(key=lambda row: (-len(row["blocked_reasons"]), -(row["job"].pk or 0)))
    summary = [{"code": code, "count": count} for code, count in reason_counts.most_common(6)]
    return eligible_rows[:limit_eligible], blocked_rows[:limit_blocked], summary


def active_job_identity_conflicts(limit: int = 10) -> list[dict]:
    groups: list[dict] = []
    url_groups = (
        Job.objects.filter(is_archived=False)
        .exclude(url_hash="")
        .values("url_hash")
        .annotate(count=Count("id"), first_created=Min("created_at"))
        .filter(count__gt=1)
        .order_by("-count", "first_created")[:limit]
    )
    for row in url_groups:
        jobs = list(Job.objects.filter(is_archived=False, url_hash=row["url_hash"]).order_by("status", "created_at", "id"))
        groups.append({
            "group_type": "url_hash",
            "group_key": row["url_hash"],
            "count": row["count"],
            "jobs": jobs,
            "survivor": _pick_conflict_survivor(jobs),
        })
    if len(groups) < limit:
        src_groups = (
            Job.objects.filter(is_archived=False, source_raw_job__isnull=False)
            .values("source_raw_job_id")
            .annotate(count=Count("id"), first_created=Min("created_at"))
            .filter(count__gt=1)
            .order_by("-count", "first_created")[: max(0, limit - len(groups))]
        )
        for row in src_groups:
            jobs = list(Job.objects.filter(is_archived=False, source_raw_job_id=row["source_raw_job_id"]).order_by("status", "created_at", "id"))
            groups.append({
                "group_type": "source_raw_job",
                "group_key": str(row["source_raw_job_id"]),
                "count": row["count"],
                "jobs": jobs,
                "survivor": _pick_conflict_survivor(jobs),
            })
    return groups[:limit]


def _pick_conflict_survivor(jobs: list[Job]) -> Job | None:
    if not jobs:
        return None
    return sorted(
        jobs,
        key=lambda job: (
            0 if job.status == Job.Status.OPEN else 1,
            0 if getattr(job, "vet_approved_at", None) else 1,
            job.created_at or timezone.now(),
            job.pk or 0,
        ),
    )[0]


def archive_identity_conflict(group_type: str, group_key: str, *, actor=None) -> dict:
    if group_type == "url_hash":
        jobs = list(Job.objects.filter(is_archived=False, url_hash=group_key).order_by("status", "created_at", "id"))
    elif group_type == "source_raw_job":
        jobs = list(Job.objects.filter(is_archived=False, source_raw_job_id=group_key).order_by("status", "created_at", "id"))
    else:
        return {"archived": 0, "survivor": None}
    survivor = _pick_conflict_survivor(jobs)
    archived = 0
    now = timezone.now()
    for job in jobs:
        if not survivor or job.pk == survivor.pk:
            continue
        job.is_archived = True
        job.archived_at = now
        job.archived_by = actor
        job.save(update_fields=["is_archived", "archived_at", "archived_by", "updated_at"])
        archived += 1
    return {"archived": archived, "survivor": survivor}


def _job_claimed_by_other(job: Job, consultant: ConsultantProfile) -> bool:
    return ApplicationSubmission.objects.filter(
        job=job,
        status__in=_ACTIVE_SUBMISSION_STATUSES,
        is_archived=False,
    ).exclude(consultant=consultant).exists()


def _job_matches_consultant_preferences(job: Job, consultant: ConsultantProfile) -> bool:
    cfg = _routing_config()
    consultant_roles = set(consultant.marketing_roles.values_list("id", flat=True))
    job_roles = set(job.marketing_roles.values_list("id", flat=True))
    if not consultant_roles or not job_roles:
        return False
    if not (consultant_roles & job_roles):
        return False

    if getattr(cfg, "routing_enforce_country_match", True) and not _job_country_match(job, consultant):
        return False

    preferred_seniority = {str(level).strip().lower() for level in (consultant.preferred_seniority_levels or []) if str(level).strip()}
    if getattr(cfg, "routing_enforce_seniority_match", True) and preferred_seniority:
        if _job_seniority_bucket(job) not in preferred_seniority:
            return False

    if not _job_work_authorization_match(job, consultant):
        return False

    if not _employment_preferences_match(job, consultant):
        return False

    if not _work_mode_match(job, consultant):
        return False

    if not _clearance_match(job, consultant):
        return False

    if _job_claimed_by_other(job, consultant):
        return False

    return True


def find_potential_duplicate_jobs(
    *,
    title: str,
    company: str,
    description: str = "",
    exclude_job_id: int | None = None,
    limit: int = 5,
):
    """
    Rules-based duplicate detection:
    - Strong signal: same company + very similar title
    - Secondary: description similarity (Jaccard on tokens)

    Returns list of dicts: {job, title_score, desc_score, overall_score}
    """
    title_n = _norm_text(title)
    company_n = _norm_text(company)
    desc_tokens = _tokenize(description or "")

    if not title_n or not company_n:
        return []

    qs = Job.objects.all()
    if exclude_job_id:
        qs = qs.exclude(pk=exclude_job_id)
    # Narrow candidate set cheaply
    qs = qs.filter(company__icontains=company.strip()).only("id", "title", "company", "description", "status", "created_at")

    title_tokens = _tokenize(title_n)
    results = []
    for j in qs[:200]:  # safety cap
        jt = _tokenize(j.title)
        title_score = _jaccard(title_tokens, jt)
        if title_score < 0.55 and company_n != _norm_text(j.company):
            continue
        desc_score = _jaccard(desc_tokens, _tokenize(j.description or "")) if desc_tokens else 0.0
        overall = (title_score * 0.75) + (desc_score * 0.25)
        if overall >= 0.62 or (title_score >= 0.72 and desc_score >= 0.35):
            results.append(
                {
                    "job": j,
                    "title_score": round(title_score, 2),
                    "desc_score": round(desc_score, 2),
                    "overall_score": round(overall, 2),
                }
            )
    results.sort(key=lambda r: r["overall_score"], reverse=True)
    return results[:limit]


def rule_parse_jd(description: str) -> dict:
    """
    Rules-first JD parsing (no LLM):
    - Extract required_skills by matching against existing consultant skills
    - Lightweight extraction of keywords from common 'Requirements' style sections
    """
    text = (description or "").strip()
    if not text:
        return {}

    low = text.lower()

    # Build known skill universe from stored consultant skills (data-driven, no tokens)
    known = set()
    for skills in ConsultantProfile.objects.values_list("skills", flat=True):
        if not skills:
            continue
        try:
            for s in skills:
                if isinstance(s, str) and s.strip():
                    known.add(s.strip().lower())
        except Exception:
            continue

    required = []
    if known:
        # Prefer exact/phrase hits (substring match) for multi-word skills
        for skill in sorted(known, key=lambda x: (-len(x), x))[:2500]:
            if len(skill) < 2:
                continue
            if skill in low:
                required.append(skill)
            if len(required) >= 40:
                break

    # Fallback: try to capture bullet-ish requirement lines as keywords
    req_section = ""
    m = re.search(r"(requirements|what you will do|qualifications)\s*:?\s*(.+)", low, re.IGNORECASE | re.DOTALL)
    if m:
        req_section = m.group(2)[:1500]
    if not required and req_section:
        bullets = re.findall(r"(?:^|\n)\s*[-•\*]\s*([^\n]{3,120})", req_section)
        # keep short phrases as "required_skills" candidates
        for b in bullets[:20]:
            phrase = re.sub(r"[^a-z0-9\+\.\#\-\s]", " ", b.lower()).strip()
            phrase = re.sub(r"\s+", " ", phrase)
            if phrase and phrase not in required and len(phrase) <= 40:
                required.append(phrase)

    return {
        "required_skills": required[:40],
        "source": "rules",
    }


class JDParserService:
    @staticmethod
    def parse_job(job: Job, actor=None):
        """
        Parse JD into structured JSON and persist it on the Job.
        Uses the shared JD extraction engine first so routing fields and parsed_jd
        stay in sync. Falls back to the legacy parser path only if the extractor
        itself fails unexpectedly.
        """
        if not job or not job.description:
            return False, "Missing job description"
        try:
            from resumes.pipeline.jd_extractor import extract_jd

            data = extract_jd(job, force=True, save=True)
            status = getattr(job, "parsed_jd_status", "") or ((data.get("parser_metadata") or {}).get("status") if isinstance(data, dict) else "")
            if status and "FAILED" not in status:
                return True, ""
        except Exception:
            logger.exception("JD extractor pipeline failed for job %s; falling back to legacy parser", getattr(job, "pk", None))

        # 1) Rules-first parse
        data = rule_parse_jd(job.description)
        if data and data.get("required_skills"):
            job.parsed_jd = data
            job.parsed_jd_status = "OK_RULES"
            job.parsed_jd_error = ""
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd", "parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return True, ""

        # 2) LLM fallback (only if configured)
        llm = LLMService()
        if not llm.client:
            job.parsed_jd_status = "ERROR"
            job.parsed_jd_error = "No rules parse result and LLM not configured"
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return False, job.parsed_jd_error

        system_prompt = JD_PARSER_SYSTEM_PROMPT
        user_prompt = JD_PARSER_USER_PROMPT.replace("{jd_text}", job.description)
        content, _, error = llm.generate_with_prompts(job, None, system_prompt, user_prompt, actor=actor, force_new=True)
        if error or not content:
            job.parsed_jd_status = "ERROR"
            job.parsed_jd_error = error or "Empty parser response"
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return False, job.parsed_jd_error

        try:
            data = json.loads(content)
        except Exception:
            # Try to extract JSON block
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                try:
                    data = json.loads(content[start:end+1])
                except Exception as exc:
                    job.parsed_jd_status = "ERROR"
                    job.parsed_jd_error = f"Parser JSON decode failed: {exc}"
                    job.parsed_jd_updated_at = timezone.now()
                    job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
                    return False, job.parsed_jd_error
            else:
                job.parsed_jd_status = "ERROR"
                job.parsed_jd_error = "Parser returned non-JSON"
                job.parsed_jd_updated_at = timezone.now()
                job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
                return False, job.parsed_jd_error

        if not isinstance(data, dict):
            job.parsed_jd_status = "ERROR"
            job.parsed_jd_error = "Parser output is not a JSON object"
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return False, job.parsed_jd_error

        job.parsed_jd = data
        job.parsed_jd_status = "OK"
        job.parsed_jd_error = ""
        job.parsed_jd_updated_at = timezone.now()
        job.save(update_fields=["parsed_jd", "parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
        return True, ""


def ensure_parsed_jd(job: Job, actor=None):
    if not job.parsed_jd:
        return JDParserService.parse_job(job, actor=actor)
    return True, ""


def _normalize_list(values):
    if not values:
        return []
    out = []
    for v in values:
        if not v:
            continue
        if isinstance(v, str):
            out.append(v.strip().lower())
        else:
            out.append(str(v).strip().lower())
    return out


def consultant_job_match_detail(job: Job, consultant: ConsultantProfile) -> dict:
    """
    Heuristic match for UI: raw score, 0–100% coverage, and skill overlap counts.
    """
    if not _job_matches_consultant_preferences(job, consultant):
        return {
            "raw_score": 0,
            "match_pct": 0,
            "matched_required": 0,
            "total_required": 0,
            "required_skills": [],
        }
    raw_score = _score_job_for_consultant(job, consultant)
    skills = set(_normalize_list(consultant.skills))
    parsed = job.parsed_jd or {}
    required = _normalize_list(parsed.get("required_skills") or [])
    if required:
        req_set = set(required)
        overlap = skills & req_set
        match_pct = min(100, round(100 * len(overlap) / len(required)))
        return {
            "raw_score": raw_score,
            "match_pct": match_pct,
            "matched_required": len(overlap),
            "total_required": len(required),
            "required_skills": required,
        }
    # No parsed requirements: estimate from skills appearing in JD text
    desc = (job.description or "").lower()
    if skills and desc:
        hits = sum(1 for s in skills if s and len(s) >= 2 and s in desc)
        match_pct = min(100, round(100 * hits / max(1, len(skills))))
    else:
        match_pct = min(100, raw_score) if raw_score else 0
    return {
        "raw_score": raw_score,
        "match_pct": match_pct,
        "matched_required": 0,
        "total_required": 0,
        "required_skills": [],
    }


def ranked_consultants_for_job(job: Job, limit: int = 25) -> List[dict]:
    """
    All active consultants with a match % and raw score, sorted best-first.
    """
    qs = ConsultantProfile.objects.filter(status=ConsultantProfile.Status.ACTIVE).prefetch_related(
        "marketing_roles", "user"
    )
    rows = []
    for consultant in qs:
        if not _job_matches_consultant_preferences(job, consultant):
            continue
        detail = consultant_job_match_detail(job, consultant)
        rows.append(
            {
                "consultant": consultant,
                "match_pct": detail["match_pct"],
                "raw_score": detail["raw_score"],
                "matched_required": detail["matched_required"],
                "total_required": detail["total_required"],
            }
        )
    rows.sort(key=lambda r: (-r["match_pct"], -r["raw_score"], r["consultant"].user.get_full_name() or r["consultant"].user.username))
    return rows[:limit]


def _score_job_for_consultant(job: Job, consultant: ConsultantProfile) -> int:
    """
    Heuristic score:
    - Overlap between consultant skills and parsed_jd.required_skills
    - Bonus for matching marketing roles
    """
    score = 0
    skills = _normalize_list(consultant.skills)
    parsed = job.parsed_jd or {}
    required = _normalize_list(parsed.get("required_skills") or [])

    # Skill overlap
    if skills and required:
        overlap = set(skills) & set(required)
        score += len(overlap) * 5

    # Marketing role alignment
    consultant_roles = set(
        consultant.marketing_roles.values_list("id", flat=True)
    )
    job_roles = set(job.marketing_roles.values_list("id", flat=True))
    if consultant_roles and job_roles:
        score += len(consultant_roles & job_roles) * 3

    # Fallback: slight score if at least one skill term appears in description
    if score == 0 and skills and job.description:
        desc = job.description.lower()
        for s in skills[:10]:
            if s and s in desc:
                score += 1

    return score


def validate_job_quality(job: Job) -> dict:
    """
    Score a job 0–100 across 9 quality checks.
    Returns:
      {
        "score": int,
        "issues": [{"code": str, "severity": str, "message": str}],
        "passed": [str],
        "auto_approved": bool,
      }
    """
    score = 0
    issues = []
    passed = []

    # 1. Title meaningful (10 pts)
    title = (job.title or "").strip()
    generic_titles = {"job", "position", "role", "opportunity", "opening", "vacancy"}
    if len(title) > 5 and title.lower() not in generic_titles:
        score += 10
        passed.append("TITLE_OK")
    else:
        issues.append({"code": "TITLE_WEAK", "severity": "high", "message": "Title is missing or too generic."})

    # 2. Description length (15 pts — partial credit)
    desc_words = len((job.description or "").split())
    if desc_words >= 150:
        score += 15
        passed.append("DESCRIPTION_FULL")
    elif desc_words >= 60:
        score += 8
        issues.append({"code": "DESCRIPTION_SHORT", "severity": "low", "message": f"Description is short ({desc_words} words). 150+ words recommended."})
    else:
        issues.append({"code": "DESCRIPTION_MISSING", "severity": "high", "message": f"Description is very short ({desc_words} words). Add a full job description."})

    # 3. Job URL present (10 pts)
    if (job.original_link or "").strip():
        score += 10
        passed.append("URL_PRESENT")
    else:
        issues.append({"code": "NO_URL", "severity": "medium", "message": "No original job posting URL. Link is required for tracking."})

    # 4. URL live check (10 pts — use stored flag; background task sets this)
    if (job.original_link or "").strip():
        if job.original_link_is_live:
            score += 10
            passed.append("URL_LIVE")
        elif job.original_link_last_checked_at is None:
            # Not yet checked — give benefit of the doubt
            score += 5
            issues.append({"code": "URL_UNCHECKED", "severity": "low", "message": "URL has not been validated yet. Will be checked by background task."})
        else:
            issues.append({"code": "URL_DEAD", "severity": "high", "message": "Original posting URL appears to be unavailable."})
    else:
        # Already flagged above (no URL)
        pass

    # 5. Company not blacklisted (15 pts)
    if job.company_obj_id and job.company_obj:
        if not getattr(job.company_obj, 'is_blacklisted', False):
            score += 15
            passed.append("COMPANY_OK")
        else:
            issues.append({"code": "COMPANY_BLACKLISTED", "severity": "critical", "message": f"Company '{job.company}' is on the blacklist. This job must not be submitted."})
    else:
        # No structured company — give partial credit (can't check blacklist)
        score += 8
        issues.append({"code": "NO_COMPANY_PROFILE", "severity": "low", "message": "No structured company profile linked. Link a company to enable blacklist checking."})

    # 6. Duplicate check (15 pts)
    dups = find_potential_duplicate_jobs(
        title=job.title or "",
        company=job.company or "",
        description=job.description or "",
        exclude_job_id=job.pk,
        limit=1,
    )
    if not dups:
        score += 15
        passed.append("NO_DUPLICATE")
    else:
        top = dups[0]
        issues.append({
            "code": "DUPLICATE_RISK",
            "severity": "high",
            "message": f"Similar job exists: '{top['job'].title}' at {top['job'].company} (match score {top['overall_score']:.0%}, Job #{top['job'].id}).",
        })

    # 7. Skills parsed from JD (10 pts)
    parsed_skills = (job.parsed_jd or {}).get("required_skills", [])
    if parsed_skills:
        score += 10
        passed.append("SKILLS_PARSED")
    else:
        issues.append({"code": "NO_SKILLS", "severity": "medium", "message": "No required skills extracted from the JD. Run JD parse or add more detail to the description."})

    # 8. Marketing roles tagged (10 pts)
    try:
        roles_count = job.marketing_roles.count()
    except Exception:
        roles_count = 0
    if roles_count > 0:
        score += 10
        passed.append("ROLES_TAGGED")
    else:
        issues.append({"code": "NO_ROLES", "severity": "medium", "message": "No marketing roles tagged. Add roles so consultants are matched correctly."})

    # 9. Salary range present (5 pts)
    if (job.salary_range or "").strip():
        score += 5
        passed.append("SALARY_OK")
    else:
        issues.append({"code": "NO_SALARY", "severity": "low", "message": "No salary range provided. Adding it improves consultant matching."})

    # Auto-approve threshold
    from core.models import PlatformConfig
    try:
        cfg = PlatformConfig.load()
        threshold = getattr(cfg, 'auto_approve_pool_threshold', 0) or 0
    except Exception:
        threshold = 0
    auto_approved = bool(threshold > 0 and score >= threshold)

    return {
        "score": score,
        "issues": issues,
        "passed": passed,
        "auto_approved": auto_approved,
    }


def match_jobs_for_consultant(
    consultant: ConsultantProfile, limit: int = 10
):
    """
    Return a list of best matching OPEN jobs for a consultant.
    """
    qs = Job.objects.filter(status=Job.Status.OPEN, is_archived=False)
    consultant_role_ids = list(consultant.marketing_roles.values_list("id", flat=True))
    if not consultant_role_ids:
        return []
    if consultant_role_ids:
        qs = qs.filter(marketing_roles__in=consultant_role_ids).distinct()
    scores = []
    for job in qs.prefetch_related("marketing_roles"):
        if not _job_matches_consultant_preferences(job, consultant):
            continue
        s = _score_job_for_consultant(job, consultant)
        if s > 0:
            scores.append((s, job))
    scores.sort(key=lambda x: x[0], reverse=True)
    return [job for _, job in scores[:limit]]


def eligible_jobs_for_consultant(consultant: ConsultantProfile, *, limit: int | None = None):
    qs = Job.objects.filter(status=Job.Status.OPEN, is_archived=False)
    consultant_role_ids = list(consultant.marketing_roles.values_list("id", flat=True))
    if not consultant_role_ids:
        return []
    if consultant_role_ids:
        qs = qs.filter(marketing_roles__in=consultant_role_ids).distinct()
    jobs = [job for job in qs.prefetch_related("marketing_roles") if _job_matches_consultant_preferences(job, consultant)]
    if limit:
        return jobs[:limit]
    return jobs


def consultant_routing_metrics(consultant: ConsultantProfile) -> dict:
    qs = Job.objects.filter(status=Job.Status.OPEN, is_archived=False)
    consultant_role_ids = list(consultant.marketing_roles.values_list("id", flat=True))
    if not consultant_role_ids:
        return {
            "role_scoped_jobs": 0,
            "country_fit_jobs": 0,
            "seniority_fit_jobs": 0,
            "work_auth_fit_jobs": 0,
            "employment_fit_jobs": 0,
            "work_mode_fit_jobs": 0,
            "clearance_fit_jobs": 0,
            "eligible_jobs": 0,
            "routing_review_jobs": 0,
            "blocked_jobs": 0,
        }
    if consultant_role_ids:
        role_qs = qs.filter(marketing_roles__in=consultant_role_ids).distinct()
    else:
        role_qs = Job.objects.none()

    role_jobs = list(role_qs.prefetch_related("marketing_roles"))
    country_fit = 0
    seniority_fit = 0
    work_auth_fit = 0
    employment_fit = 0
    work_mode_fit = 0
    clearance_fit = 0
    fully_eligible = 0
    review_jobs = 0
    blocked_jobs = 0
    for job in role_jobs:
        if _job_country_match(job, consultant):
            country_fit += 1
        preferred_seniority = {str(level).strip().lower() for level in (consultant.preferred_seniority_levels or []) if str(level).strip()}
        if not preferred_seniority or _job_seniority_bucket(job) in preferred_seniority:
            seniority_fit += 1
        if _job_work_authorization_match(job, consultant):
            work_auth_fit += 1
        if _employment_preferences_match(job, consultant):
            employment_fit += 1
        if _work_mode_match(job, consultant):
            work_mode_fit += 1
        if _clearance_match(job, consultant):
            clearance_fit += 1
        if _job_matches_consultant_preferences(job, consultant):
            fully_eligible += 1
        else:
            blocked_jobs += 1
        if (getattr(job, "routing_status", "") or "") in {Job.RoutingStatus.REVIEW, Job.RoutingStatus.FAILED, Job.RoutingStatus.PENDING}:
            review_jobs += 1
    return {
        "role_scoped_jobs": len(role_jobs),
        "country_fit_jobs": country_fit,
        "seniority_fit_jobs": seniority_fit,
        "work_auth_fit_jobs": work_auth_fit,
        "employment_fit_jobs": employment_fit,
        "work_mode_fit_jobs": work_mode_fit,
        "clearance_fit_jobs": clearance_fit,
        "eligible_jobs": fully_eligible,
        "routing_review_jobs": review_jobs,
        "blocked_jobs": blocked_jobs,
    }


def match_consultants_for_job(
    job: Job, limit: int = 10
):
    """
    Return a list of best matching consultants for a given job (backward compatible).
    Uses ranked match % then raw score.
    """
    ranked = ranked_consultants_for_job(job, limit=limit * 3)
    out = []
    for row in ranked:
        if row["match_pct"] > 0 or row["raw_score"] > 0:
            out.append(row["consultant"])
        if len(out) >= limit:
            break
    # If nobody scored >0, still show top few by match_pct (even 0) for visibility
    if not out and ranked:
        out = [row["consultant"] for row in ranked[:limit]]
    return out

"""
WorkdayHarvester — Public Workday REST API

Workday provides a PUBLICLY documented job board API at:
  https://{tenant}.myworkdayjobs.com/wday/cxs/{tenant}/{path}/jobs

This is their intended public interface for job boards. No authentication
is required. We identify ourselves honestly as GoCareers-Bot.

Compliance:
  - Honest User-Agent (inherited from BaseHarvester)
  - 1-second minimum delay between path attempts (rate_limit)
  - Max 20 results per request (their recommended page size)
  - Stops as soon as a valid path returns results (no unnecessary calls)
  - Retries with backoff on 5xx / timeouts (BaseHarvester)
  - fetch_all=True paginates through ALL results with polite delays
"""
import re as _re
import time
from typing import Any

from .base import BaseHarvester, MIN_DELAY_API

# Matches Workday's multi-location count placeholder, e.g. "2 Locations".
_LOC_COUNT_RE = _re.compile(r"^\d+\s+locations?$", _re.I)

# Generic Workday job-board path fallbacks (used only when no specific path
# is stored in tenant_id). Real paths are highly company-specific.
WORKDAY_PATHS_FALLBACK = [
    "External",
    "EXT",
    "External_Career_Site",
    "Careers",
    "Search",
    "US",
    "All",
    "US-External",
    "Jobs",
    "Global",
]

PAGE_SIZE = 20
# Max per-job detail calls per company fetch (0 = unlimited).
# Raised from 80: the CXS JSON API is fast (~0.5s/call), so fetching up to 300
# inline adds ~5 min at most for the largest companies. Jobs beyond this cap
# are caught by the background backfill task (which also uses the CXS API).
DETAIL_FETCH_CAP = 300

# Req ID patterns embedded in Workday externalPath segments.
# Matches: JR12345, JR-001234, R-2025-98765, REQ-2024-00123, 123456789
_REQ_ID_RE = _re.compile(
    r'[_/]((?:[A-Z]{1,4}[-_]?\d{3,}(?:[-_]\d+)*|\d{5,12}))(?:[_/]|$|\?)',
    _re.I,
)


# ── Small helpers ─────────────────────────────────────────────────────────────

# Minimal set of tech-signal words used for early pagination exit.
# Not exhaustive — just enough to detect "this page has tech roles" quickly.
# Full classification uses role_filter.classify_title_v2() with all phrases.
_TECH_SIGNAL_RE = _re.compile(
    r"\b(engineer|developer|devops|sre|architect|analyst|administrator|"
    r"data|cloud|platform|security|infrastructure|python|java|aws|azure|gcp|"
    r"kubernetes|docker|software|backend|frontend|fullstack|full.stack|"
    r"mlops|devsecops|servicenow|salesforce|epic|cerner|sap|workday.admin|"
    r"it\s|it$|technical|system|network|database|dba|qa\s|qa$)\b",
    _re.I,
)


def _page_has_tech_signal(page_results: list[dict]) -> bool:
    """
    Return True if at least one job on this page has a tech-looking title.
    Used for Workday early pagination exit: 5 consecutive zero-signal pages → stop.
    """
    for job in page_results:
        title = job.get("title") or ""
        if _TECH_SIGNAL_RE.search(title):
            return True
    return False


def _save_fetch_offset(company, offset: int) -> None:
    """
    Persist pagination checkpoint to CompanyPlatformLabel.last_fetch_offset.
    Offset=0 means "completed" or "start fresh". Non-zero means "resume here".
    Silent no-op if DB save fails — pagination is advisory.
    """
    try:
        from harvest.models import CompanyPlatformLabel
        CompanyPlatformLabel.objects.filter(company=company).update(
            last_fetch_offset=offset
        )
    except Exception:
        pass


def _wd_str(val: Any, *fallback_keys: str) -> str:
    """Safely coerce a Workday field (str / dict / list / None) to a plain string."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in fallback_keys or ("descriptor", "displayValue", "value", "name", "id"):
            if val.get(k):
                return str(val[k]).strip()
        return ""
    if isinstance(val, list):
        parts = [_wd_str(v) for v in val]
        return " | ".join(p for p in parts if p)
    return str(val).strip()


def _wd_req_id(ext_path: str, bullet_fields: list) -> str:
    """Extract req ID from externalPath or bulletFields."""
    if ext_path:
        m = _REQ_ID_RE.search(ext_path)
        if m:
            return m.group(1)
        # Sometimes the path ends with just a numeric ID
        tail = ext_path.rstrip("/").rsplit("/", 1)[-1]
        num = _re.match(r"^(\d{4,12})$", tail)
        if num:
            return num.group(1)
    if bullet_fields and isinstance(bullet_fields, list):
        candidate = str(bullet_fields[0]).strip()
        # bulletFields[0] is the req ID only when it looks like one
        if _re.match(r'^[A-Z]{0,4}[\-_]?\d{3,12}$', candidate, _re.I):
            return candidate
    return ""


def _wd_department(job: dict) -> str:
    """Extract department with a prioritised fallback chain."""
    # 1. jobFamilyGroup
    jfg = job.get("jobFamilyGroup") or []
    if isinstance(jfg, list) and jfg:
        name = _wd_str(jfg[0], "jobFamilyGroupName", "descriptor", "name")
        if name:
            return name
    if isinstance(jfg, str) and jfg:
        return jfg
    # 2. jobFamily
    jf = job.get("jobFamily") or []
    if isinstance(jf, list) and jf:
        name = _wd_str(jf[0], "jobFamilyName", "descriptor", "name")
        if name:
            return name
    if isinstance(jf, str) and jf:
        return jf
    # 3. jobCategory (can also be a list)
    cat = job.get("jobCategory") or []
    if isinstance(cat, list) and cat:
        return _wd_str(cat[0], "descriptor", "name") or ""
    if isinstance(cat, str) and cat:
        return cat
    # 4. hiringOrganization / department flat fields
    return (
        _wd_str(job.get("hiringOrganization"), "descriptor", "name")
        or _wd_str(job.get("department"), "descriptor", "name")
        or ""
    )


def _wd_parse_location(location_raw: str) -> tuple[str, str, str]:
    """
    Best-effort city/state/country from a Workday locationsText string.
    Handles: "Austin, TX, USA", "Austin, TX, United States", "Remote" etc.
    Multi-location strings ("City1, ST1, USA | City2, ST2, USA") return
    only the first entry's components.
    """
    if not location_raw:
        return "", "", ""
    # Take only the first segment in a pipe-delimited multi-location string
    first = location_raw.split("|")[0].strip()
    parts = [p.strip() for p in first.split(",") if p.strip()]
    if len(parts) >= 3:
        city = parts[0]
        state = parts[1]
        country = _wd_normalize_country(parts[-1])
        return city, state, country
    if len(parts) == 2:
        # Could be "City, State" or "City, Country"
        second = parts[1]
        if len(second) == 2 and second.isupper():
            return parts[0], second, ""
        if second.lower() in ("usa", "us", "united states", "canada", "uk", "united kingdom", "australia"):
            return parts[0], "", _wd_normalize_country(second)
        return parts[0], second, ""
    return "", "", ""


def _wd_normalize_country(raw: str) -> str:
    v = (raw or "").strip()
    _map = {"US": "United States", "USA": "United States", "UK": "United Kingdom", "GB": "United Kingdom"}
    return _map.get(v.upper(), v)


def _wd_employment_type(job: dict) -> str:
    """Map Workday schedule/type fields to our canonical employment type."""
    text = " ".join(filter(None, [
        _wd_str(job.get("jobScheduleType")),
        _wd_str(job.get("workerSubType")),
        _wd_str(job.get("workerType")),
        _wd_str(job.get("employmentType")),
    ])).lower()
    if not text:
        return "UNKNOWN"
    if "intern" in text:
        return "INTERNSHIP"
    if "contract" in text or "contingent" in text or "temp" in text:
        return "CONTRACT"
    if "part" in text:
        return "PART_TIME"
    if "full" in text or "regular" in text or "permanent" in text:
        return "FULL_TIME"
    return "UNKNOWN"


def _wd_parse_salary(job: dict) -> tuple[float | None, float | None, str, str]:
    """
    Return (sal_min, sal_max, sal_period, salary_raw) from list-API job fields.
    Tries numeric compensation fields first, then free-text.
    """
    # Numeric fields (detail API sometimes exposes these on list too)
    from_amt = job.get("compensationFromAmt") or job.get("salaryFromAmt")
    to_amt   = job.get("compensationToAmt")   or job.get("salaryToAmt")
    if from_amt and isinstance(from_amt, (int, float)) and float(from_amt) > 0:
        sal_min = float(from_amt)
        sal_max = float(to_amt) if to_amt and isinstance(to_amt, (int, float)) and float(to_amt) > 0 else sal_min
        sal_period = _wd_str(job.get("compensationPeriod") or job.get("salaryPeriod")) or "YEAR"
        raw = f"{sal_min:,.0f}–{sal_max:,.0f}" if sal_max != sal_min else f"{sal_min:,.0f}"
        return sal_min, sal_max, sal_period.upper(), raw

    # Free-text fallback
    raw_text = (
        _wd_str(job.get("annualCompensationSummary"), "descriptor", "summary")
        or _wd_str(job.get("compensationGrade"), "descriptor")
        or _wd_str(job.get("compensationRangeStr"))
        or ""
    )
    if not raw_text:
        return None, None, "", ""

    nums = _re.findall(r"[\d,]+(?:\.\d+)?", raw_text.replace(",", ""))
    cleaned = []
    for n in nums:
        try:
            v = float(n.replace(",", ""))
            if 1_000 < v < 10_000_000:
                cleaned.append(v)
        except ValueError:
            pass
    sal_min = cleaned[0] if cleaned else None
    sal_max = cleaned[1] if len(cleaned) > 1 else sal_min
    tl = raw_text.lower()
    if "hour" in tl or "/hr" in tl:
        sal_period = "HOUR"
    elif "month" in tl:
        sal_period = "MONTH"
    else:
        sal_period = "YEAR"
    return sal_min, sal_max, sal_period, raw_text


def _wd_location_strings(value) -> list[str]:
    """Flatten a Workday location value (str | list | dict) into clean strings.

    Workday CXS exposes the real per-location list under jobPostingInfo.location
    (primary) and jobPostingInfo.additionalLocations. Entries are usually plain
    strings ("Madrid, Spain") but some tenants nest a {descriptor|name|...}.
    """
    out: list[str] = []
    if not value:
        return out
    if isinstance(value, str):
        text = value.strip()
        if text:
            out.append(text)
        return out
    if isinstance(value, dict):
        for key in ("descriptor", "name", "label", "text", "value"):
            text = str(value.get(key) or "").strip()
            if text:
                out.append(text)
                break
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_wd_location_strings(item))
    return out


def _workday_location_candidates(data: dict) -> list[str]:
    try:
        from harvest.location_resolver import extract_location_candidates
    except Exception:
        return []
    info = data.get("jobPostingInfo") or data
    loc_raw = str(info.get("locationsText") or data.get("locationsText") or "")

    # locationsText is a count placeholder ("2 Locations") for multi-location reqs.
    # The real list lives in location (primary) + additionalLocations — read them
    # explicitly so multi-location jobs resolve instead of falling back to UNKNOWN.
    explicit: list[str] = []
    explicit.extend(_wd_location_strings(info.get("location") or data.get("location")))
    explicit.extend(_wd_location_strings(info.get("additionalLocations") or data.get("additionalLocations")))
    explicit = [loc for loc in explicit if loc and not _LOC_COUNT_RE.match(loc)]

    # If locationsText is just a count, replace it with the real primary location.
    if explicit and _LOC_COUNT_RE.match(loc_raw.strip()):
        loc_raw = explicit[0]

    vendor_block = str(
        info.get("jobLocation") or data.get("jobLocation") or ""
    )
    city, state, country = _wd_parse_location(loc_raw)
    candidates = extract_location_candidates(
        location_raw=loc_raw,
        city=city,
        state=state,
        country=country,
        vendor_location_block=vendor_block,
        raw_payload=data,
    )
    # Guarantee every explicit per-location entry is present as a candidate.
    seen = {c.lower() for c in candidates}
    for loc in explicit:
        if loc.lower() not in seen:
            candidates.append(loc)
            seen.add(loc.lower())
    return candidates


def _fetch_workday_detail(session, full_subdomain: str, tenant: str, jobboard: str, ext_path: str) -> dict:
    """
    GET the Workday CXS detail endpoint for a single job and return detail fields.
    Returns {} on any failure — never raises.

    Endpoint: https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{jobboard}{ext_path}
    Returns JSON with full job description (same API Jarvis uses for backfill).
    No Playwright — pure HTTP JSON, fast and CPU-light.
    """
    if not ext_path:
        return {}
    url = (
        f"https://{full_subdomain}.myworkdayjobs.com"
        f"/wday/cxs/{tenant}/{jobboard}{ext_path}"
    )
    try:
        resp = session.get(url, headers={"Accept": "application/json"}, timeout=10)
        if not resp.ok:
            return {}
        data = resp.json()
        if not isinstance(data, dict):
            return {}
        info = data.get("jobPostingInfo") or data
        result: dict[str, Any] = {"raw_payload": data}

        # Description — check progressively deeper nesting
        for src in (info, data):
            for key in (
                "jobDescription", "jobPostingDescription",
                "externalJobDescription", "jobSummary", "shortDescription",
            ):
                val = src.get(key) or ""
                if isinstance(val, dict):
                    val = val.get("content", "") or val.get("descriptor", "") or ""
                val = str(val).strip()
                if val:
                    result["description"] = val
                    break
            if "description" in result:
                break

        # Location
        location_candidates = _workday_location_candidates(data)
        if location_candidates:
            result["location_candidates"] = location_candidates
            loc_raw = str(info.get("locationsText") or data.get("locationsText") or "")
            # Don't keep the "2 Locations" count placeholder — replace it with the
            # real per-location list so display and re-resolution both work.
            if not loc_raw or _LOC_COUNT_RE.match(loc_raw.strip()):
                loc_raw = " | ".join(location_candidates)
            result["location_raw"] = loc_raw[:512]
            result["city"], result["state"], result["country"] = _wd_parse_location(loc_raw)

        # Salary from detail (numeric fields are more reliable than free-text)
        sal_min, sal_max, sal_period, sal_raw = _wd_parse_salary(info)
        if sal_min:
            result["salary_min"] = sal_min
            result["salary_max"] = sal_max
            result["salary_period"] = sal_period
            result["salary_raw"] = sal_raw

        # vendor_degree_level from detail
        deg = (
            _wd_str(info.get("minimumQualifications"), "descriptor")
            or _wd_str(info.get("educationLevel"), "descriptor")
            or _wd_str(info.get("degreeLevel"), "descriptor")
            or ""
        )
        if deg:
            result["vendor_degree_level"] = deg[:128]

        # Department from detail (may be more specific than list API)
        dept = _wd_department(info) or _wd_department(data)
        if dept:
            result["department"] = dept

        return result
    except Exception:
        pass
    return {}


def _normalize_workday_job(job: dict, job_domain: str, company_name: str, jobboard: str = "") -> dict:
    """Normalize a single Workday job posting dict to the canonical RawJob schema."""
    ext_path = job.get("externalPath", "")
    # Correct Workday URL format: https://{subdomain}.myworkdayjobs.com/{jobboard}/job/...
    # Without the jobboard prefix the URL 404s — Workday requires it.
    if ext_path:
        if jobboard:
            job_url = f"https://{job_domain}.myworkdayjobs.com/{jobboard}{ext_path}"
        else:
            job_url = f"https://{job_domain}.myworkdayjobs.com{ext_path}"
    else:
        job_url = ""

    # ── External ID ───────────────────────────────────────────────────────────
    ext_id = _wd_req_id(ext_path, job.get("bulletFields") or [])

    # ── Location ──────────────────────────────────────────────────────────────
    location_raw = _wd_str(job.get("locationsText"))
    location_candidates = _workday_location_candidates(job)
    city, state, country = _wd_parse_location(location_raw)
    loc_lower = location_raw.lower()
    if "remote" in loc_lower:
        is_remote = True
        location_type = "REMOTE"
    elif "hybrid" in loc_lower:
        is_remote = False
        location_type = "HYBRID"
    elif location_raw:
        is_remote = False
        location_type = "ONSITE"
    else:
        is_remote = False
        location_type = "UNKNOWN"

    # ── Title & description ───────────────────────────────────────────────────
    title = _wd_str(job.get("title"))

    description = ""
    for key in ("jobDescription", "jobPostingDescription", "externalJobDescription", "shortDescription"):
        val = job.get(key) or ""
        if isinstance(val, dict):
            val = val.get("content", "") or val.get("descriptor", "") or ""
        val = str(val).strip()
        if val:
            description = val
            break

    exp_level = _detect_experience_level(title, description[:500])

    # ── Department ────────────────────────────────────────────────────────────
    dept = _wd_department(job)
    vendor_job_category = dept  # preserve original before truncation

    # ── Employment type ───────────────────────────────────────────────────────
    employment_type = _wd_employment_type(job)

    # ── Salary ────────────────────────────────────────────────────────────────
    sal_min, sal_max, sal_period, salary_raw = _wd_parse_salary(job)

    # ── Vendor fields ─────────────────────────────────────────────────────────
    vendor_job_identification = ext_id
    vendor_job_schedule = _wd_str(job.get("jobScheduleType"))[:128]
    vendor_degree_level = (
        _wd_str(job.get("minimumQualifications"), "descriptor")
        or _wd_str(job.get("educationLevel"), "descriptor")
        or _wd_str(job.get("degreeLevel"), "descriptor")
    )[:128]
    vendor_location_block = location_raw[:512]

    return {
        "external_id": ext_id,
        "original_url": job_url,
        "apply_url": job_url,
        "title": title,
        "company_name": company_name,
        "department": dept,
        "team": "",
        "location_raw": location_raw,
        "location_candidates": location_candidates,
        "city": city,
        "state": state,
        "country": country,
        "is_remote": is_remote,
        "location_type": location_type,
        "employment_type": employment_type,
        "experience_level": exp_level,
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": "USD",
        "salary_period": sal_period,
        "salary_raw": salary_raw,
        "description": description,
        "requirements": "",
        "responsibilities": "",
        "benefits": "",
        "vendor_job_identification": vendor_job_identification,
        "vendor_job_category": vendor_job_category[:128],
        "vendor_job_schedule": vendor_job_schedule,
        "vendor_degree_level": vendor_degree_level,
        "vendor_location_block": vendor_location_block,
        "posted_date_raw": _wd_str(job.get("postedOn")),
        "closing_date": _wd_str(job.get("closingDate")),
        "raw_payload": job,
        "source_payloads": [
            {
                "kind": "list",
                "payload": job,
                "source_url": job_url,
                "metadata": {"platform": "workday", "source": "workday_search_api"},
            }
        ],
    }


def _detect_experience_level(title: str, description: str) -> str:
    combined = (title + " " + description).lower()
    if any(k in combined for k in ("intern", "internship", "co-op", "coop")):
        return "ENTRY"
    if any(k in combined for k in ("chief ", "cto", "ceo", "coo", "cfo", "svp", "evp", "vp ", "vice president")):
        return "EXECUTIVE"
    if any(k in combined for k in ("director", "head of")):
        return "DIRECTOR"
    if any(k in combined for k in ("manager", "mgr")):
        return "MANAGER"
    if any(k in combined for k in ("lead ", "principal", "staff ")):
        return "LEAD"
    if any(k in combined for k in ("senior", "sr.", "sr ")):
        return "SENIOR"
    if any(k in combined for k in ("junior", "jr.", "jr ", "entry", "associate", "i ", "level 1")):
        return "ENTRY"
    return "MID"


class WorkdayHarvester(BaseHarvester):
    """Harvests jobs from Workday public REST API."""

    platform_slug = "workday"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24, fetch_all: bool = False
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        # tenant_id stored as "{full_subdomain}|{jobboard}"
        # e.g. "inotivco.wd5|EXT" or legacy "inotivco|EXT"
        if "|" in tenant_id:
            full_subdomain, jobboard = tenant_id.split("|", 1)
            tenant = _re.sub(r"\.wd\d+$", "", full_subdomain, flags=_re.I)
            # When a specific path is stored we ONLY try that path — never
            # fall through to the generic list. Trying 10 extra paths × 3
            # retries per company wastes resources and risks IP bans.
            paths_to_try = [jobboard]
        else:
            full_subdomain = tenant_id
            tenant = _re.sub(r"\.wd\d+$", "", tenant_id, flags=_re.I)
            paths_to_try = [tenant] + WORKDAY_PATHS_FALLBACK

        job_domain = full_subdomain

        for path in paths_to_try:
            url = (
                f"https://{full_subdomain}.myworkdayjobs.com"
                f"/wday/cxs/{tenant}/{path}/jobs"
            )

            # ── First page ────────────────────────────────────────────────────
            payload = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": 0,
                "searchText": "",
            }
            data = self._post(url, json_data=payload)

            if not isinstance(data, dict) or "error" in data:
                time.sleep(MIN_DELAY_API)
                continue

            postings = data.get("jobPostings") or []
            if not postings:
                time.sleep(MIN_DELAY_API)
                continue

            # Found a valid path — collect results
            # `path` is the jobboard (e.g. "Search", "External") needed for valid URLs
            results = [_normalize_workday_job(j, job_domain, company.name, jobboard=path) for j in postings]
            self.last_total_available = int(data.get("total") or len(postings))

            if fetch_all:
                total = data.get("total", len(postings))
                # ── Resume from checkpoint if previous run timed out ──────────
                # CompanyPlatformLabel.last_fetch_offset stores where we stopped.
                # On timeout, the next run resumes instead of restarting from 0.
                resume_offset = 0
                try:
                    label = getattr(company, "platform_label", None)
                    if label and getattr(label, "last_fetch_offset", 0) > PAGE_SIZE:
                        resume_offset = label.last_fetch_offset
                except Exception:
                    pass

                offset = max(PAGE_SIZE, resume_offset)

                # Zero-signal early stop: if N consecutive pages have NO title
                # that passes the basic tech-signal check, stop paginating.
                # Avoids fetching 3000+ jobs for a company with 0% hit rate.
                ZERO_SIGNAL_PAGE_LIMIT = 5   # 5 pages (100 jobs) with no tech signal → stop
                zero_signal_pages = 0

                while offset < total:
                    time.sleep(MIN_DELAY_API)
                    next_payload = {
                        "appliedFacets": {},
                        "limit": PAGE_SIZE,
                        "offset": offset,
                        "searchText": "",
                    }
                    next_data = self._post(url, json_data=next_payload)
                    if not isinstance(next_data, dict) or "error" in next_data:
                        break
                    page_postings = next_data.get("jobPostings") or []
                    if not page_postings:
                        break

                    page_results = [
                        _normalize_workday_job(j, job_domain, company.name, jobboard=path)
                        for j in page_postings
                    ]
                    results.extend(page_results)

                    # Check if this page had any tech-looking titles
                    page_has_signal = _page_has_tech_signal(page_results)
                    if page_has_signal:
                        zero_signal_pages = 0
                    else:
                        zero_signal_pages += 1
                        if zero_signal_pages >= ZERO_SIGNAL_PAGE_LIMIT:
                            import logging as _logging
                            _logging.getLogger(__name__).info(
                                "Workday early exit: %d consecutive zero-signal pages "
                                "for %s at offset %d/%d",
                                zero_signal_pages, company, offset, total,
                            )
                            # Save checkpoint for next run (resume from here)
                            _save_fetch_offset(company, offset + PAGE_SIZE)
                            break

                    offset += PAGE_SIZE

                # Save offset=0 on clean completion (reset checkpoint)
                if zero_signal_pages < ZERO_SIGNAL_PAGE_LIMIT:
                    _save_fetch_offset(company, 0)

            # ── Inline detail fetch for jobs with no description ──────────────
            # Workday's list/search API returns mostly metadata, so this enriches
            # each role with the detail endpoint.
            #
            # IMPORTANT: for fetch_all=True (Jarvis full company crawl), skip
            # inline detail calls entirely. On large boards this can exceed the
            # task soft time limit and fail before upserts complete.
            # Missing descriptions are filled by background JD backfill.
            if fetch_all:
                return results

            # Capped to keep runtime bounded on incremental runs.
            tenant_val = _re.sub(r"\.wd\d+$", "", full_subdomain, flags=_re.I)
            detail_fetched = 0
            for job_dict in results:
                needs_location_detail = (
                    "locations" in (job_dict.get("location_raw") or "").lower()
                    and not job_dict.get("location_candidates")
                )
                if job_dict.get("description") and not needs_location_detail:
                    continue  # already has description/location from list API
                if detail_fetched >= DETAIL_FETCH_CAP:
                    break     # remaining jobs handled by background backfill

                # Extract the ext_path from the stored URL
                job_url = job_dict.get("original_url", "")
                ext_path_m = _re.search(
                    rf"myworkdayjobs\.com/{_re.escape(path)}(/(?:details|job)/.+?)(?:\?|$)",
                    job_url, _re.I,
                )
                if not ext_path_m:
                    continue
                ext_path_val = ext_path_m.group(1).split("?")[0]

                time.sleep(MIN_DELAY_API)   # polite delay between detail calls
                detail = _fetch_workday_detail(
                    self._session, full_subdomain, tenant_val, path, ext_path_val
                )
                detail_fetched += 1

                if detail.get("description"):
                    job_dict["description"] = detail["description"]
                if detail.get("location_raw"):
                    job_dict["location_raw"] = detail["location_raw"]
                    job_dict["location_candidates"] = detail.get("location_candidates") or []
                    if detail.get("city"):
                        job_dict["city"] = detail["city"]
                    if detail.get("state"):
                        job_dict["state"] = detail["state"]
                    if detail.get("country"):
                        job_dict["country"] = detail["country"]
                    loc_lower = job_dict["location_raw"].lower()
                    job_dict["is_remote"] = "remote" in loc_lower
                    job_dict["location_type"] = (
                        "REMOTE" if "remote" in loc_lower else
                        "HYBRID" if "hybrid" in loc_lower else
                        "ONSITE"
                    )
                # Upgrade salary if detail has better (numeric) data
                if detail.get("salary_min") and not job_dict.get("salary_min"):
                    job_dict["salary_min"] = detail["salary_min"]
                    job_dict["salary_max"] = detail.get("salary_max")
                    job_dict["salary_period"] = detail.get("salary_period", "YEAR")
                    job_dict["salary_raw"] = detail.get("salary_raw", "")
                # Upgrade department from detail when list-API had nothing
                if detail.get("department") and not job_dict.get("department"):
                    job_dict["department"] = detail["department"]
                    job_dict["vendor_job_category"] = detail["department"][:128]
                # Upgrade vendor_degree_level from detail
                if detail.get("vendor_degree_level") and not job_dict.get("vendor_degree_level"):
                    job_dict["vendor_degree_level"] = detail["vendor_degree_level"]
                if detail.get("raw_payload"):
                    existing = dict(job_dict.get("raw_payload") or {})
                    existing["detail"] = detail["raw_payload"]
                    job_dict["raw_payload"] = existing
                    source_payloads = list(job_dict.get("source_payloads") or [])
                    source_payloads.append({
                        "kind": "detail",
                        "payload": detail["raw_payload"],
                        "source_url": job_dict.get("original_url") or "",
                        "metadata": {"platform": self.platform_slug, "source": "workday_detail_api"},
                    })
                    job_dict["source_payloads"] = source_payloads

            self.last_detail_fetched = detail_fetched
            return results

        return []

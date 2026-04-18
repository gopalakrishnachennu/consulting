"""
GreenhouseHarvester — Public Greenhouse Job Board API

Greenhouse provides an officially documented public API at:
  https://boards-api.greenhouse.io/v1/boards/{token}/jobs

This is intended for public consumption. No authentication required.
Documentation: https://developers.greenhouse.io/job-board.html

Compliance:
  - Honest User-Agent (inherited from BaseHarvester)
  - 1-second minimum delay (BaseHarvester rate limit)
  - Retry + backoff on server errors (BaseHarvester)
  - Returns all jobs in one call (Greenhouse returns full list in a single response)
  - fetch_all=True is respected (no extra pages needed — API returns everything)
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import BaseHarvester

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

_EMPLOYMENT_MAP = {
    "full time": "FULL_TIME",
    "full-time": "FULL_TIME",
    "fulltime": "FULL_TIME",
    "part time": "PART_TIME",
    "part-time": "PART_TIME",
    "parttime": "PART_TIME",
    "contract": "CONTRACT",
    "contractor": "CONTRACT",
    "intern": "INTERNSHIP",
    "internship": "INTERNSHIP",
    "temporary": "TEMPORARY",
    "temp": "TEMPORARY",
}


def _parse_salary(text: str):
    """Extract min/max salary from a raw salary string. Returns (min, max, period)."""
    if not text:
        return None, None, ""
    nums = re.findall(r"[\d,]+(?:\.\d+)?", text.replace(",", ""))
    cleaned = []
    for n in nums:
        try:
            v = float(n.replace(",", ""))
            if v > 0:
                cleaned.append(v)
        except ValueError:
            pass
    sal_min = cleaned[0] if cleaned else None
    sal_max = cleaned[1] if len(cleaned) > 1 else sal_min
    period = ""
    tl = text.lower()
    if "hour" in tl or "/hr" in tl:
        period = "HOUR"
    elif "month" in tl:
        period = "MONTH"
    elif "year" in tl or "annual" in tl or "/yr" in tl:
        period = "YEAR"
    return sal_min, sal_max, period


def _detect_location_type(location_raw) -> tuple[str, bool]:
    # Defensively coerce to str — Greenhouse API occasionally returns list/bool/dict
    if not isinstance(location_raw, str):
        location_raw = str(location_raw) if location_raw else ""
    loc_lower = location_raw.lower()
    if "remote" in loc_lower:
        return "REMOTE", True
    if "hybrid" in loc_lower:
        return "HYBRID", False
    if location_raw.strip():
        return "ONSITE", False
    return "UNKNOWN", False


def _detect_employment_type(job: dict) -> str:
    # Greenhouse metadata field
    meta = job.get("metadata") or []
    for m in meta:
        val = (m.get("value") or "").lower()
        if val in _EMPLOYMENT_MAP:
            return _EMPLOYMENT_MAP[val]
        for key, mapped in _EMPLOYMENT_MAP.items():
            if key in val:
                return mapped
    return "UNKNOWN"


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
    if any(k in combined for k in ("junior", "jr.", "jr ", "entry", "associate")):
        return "ENTRY"
    return "MID"


class GreenhouseHarvester(BaseHarvester):
    """Harvests jobs from Greenhouse public JSON API."""

    platform_slug = "greenhouse"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24, fetch_all: bool = False
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        # With fetch_all=False we still respect since_hours filter.
        # With fetch_all=True we return everything (ignore time filter).
        cutoff = None
        if not fetch_all:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)

        url = BASE_URL.format(token=tenant_id)
        data = self._get(url, params={"content": "true"})

        if isinstance(data, dict) and "error" in data:
            return []

        all_jobs = data.get("jobs") or []
        self.last_total_available = len(all_jobs)
        results = []
        for job in all_jobs:
            updated_raw = job.get("updated_at", "")

            if cutoff and updated_raw:
                try:
                    updated_at = datetime.fromisoformat(
                        updated_raw.replace("Z", "+00:00")
                    )
                    if updated_at < cutoff:
                        continue
                except Exception:
                    pass

            loc = job.get("location") or {}
            if isinstance(loc, dict):
                name = loc.get("name") or ""
                location_raw = name if isinstance(name, str) else (str(name) if name else "")
            else:
                location_raw = str(loc) if loc else ""

            dept = ""
            depts = job.get("departments", [])
            if depts and isinstance(depts, list):
                dept = depts[0].get("name", "")

            offices = job.get("offices", [])
            city = ""
            state = ""
            country = ""
            if offices and isinstance(offices, list):
                first_office = offices[0]
                city = first_office.get("city", "") or ""
                state = first_office.get("state", "") or ""
                country = first_office.get("country", "") or ""

            location_type, is_remote = _detect_location_type(location_raw)
            employment_type = _detect_employment_type(job)

            # Description content
            content = job.get("content", "")
            description = content if content else ""
            experience_level = _detect_experience_level(job.get("title", ""), description[:500])

            results.append({
                "external_id": str(job.get("id", "")),
                "original_url": job.get("absolute_url", ""),
                "apply_url": job.get("absolute_url", ""),
                "title": job.get("title", ""),
                "company_name": company.name,
                "department": dept,
                "team": "",
                "location_raw": location_raw,
                "city": city,
                "state": state,
                "country": country,
                "is_remote": is_remote,
                "location_type": location_type,
                "employment_type": employment_type,
                "experience_level": experience_level,
                "salary_min": None,
                "salary_max": None,
                "salary_currency": "USD",
                "salary_period": "",
                "salary_raw": "",
                "description": description,
                "requirements": "",
                "benefits": "",
                "posted_date_raw": updated_raw,
                "closing_date": "",
                "raw_payload": job,
            })

        return results

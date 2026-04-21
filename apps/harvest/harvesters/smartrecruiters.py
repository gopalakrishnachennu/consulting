"""
SmartRecruitersHarvester — Public SmartRecruiters Postings API

SmartRecruiters provides a documented public REST API for job postings:
  List:   GET https://api.smartrecruiters.com/v1/companies/{company}/postings
  Detail: GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}

No authentication required for published postings.
Detail endpoint returns jobAd.sections with full description HTML.
"""
import re
import time
from typing import Any, Optional

from .base import BaseHarvester, MIN_DELAY_API

PAGE_SIZE = 100
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"


def _normalize_posting_id_for_api(raw: str) -> str:
    """Strip SEO slug from a path segment; must match :func:`jarvis._smartrecruiters_normalize_posting_id`."""
    s = (raw or "").strip().strip("-")
    if not s:
        return ""
    um = re.match(
        r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        s,
        re.I,
    )
    if um:
        return um.group(1)
    nm = re.match(r"^(\d{6,})", s)
    if nm:
        return nm.group(1)
    return s


def _detect_experience_level(title: str, description: str) -> str:
    combined = (title + " " + description).lower()
    if any(k in combined for k in ("intern", "internship", "co-op")):
        return "ENTRY"
    if any(k in combined for k in ("chief ", "cto", "ceo", "svp", "evp", "vp ", "vice president")):
        return "EXECUTIVE"
    if any(k in combined for k in ("director", "head of")):
        return "DIRECTOR"
    if any(k in combined for k in ("manager", "mgr")):
        return "MANAGER"
    if any(k in combined for k in ("lead ", "principal", "staff ")):
        return "LEAD"
    if any(k in combined for k in ("senior", "sr.", "sr ")):
        return "SENIOR"
    if any(k in combined for k in ("junior", "jr.", "entry", "associate")):
        return "ENTRY"
    return "MID"


class SmartRecruitersHarvester(BaseHarvester):
    platform_slug = "smartrecruiters"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24, fetch_all: bool = False
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        slug = tenant_id.strip()
        base_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"

        results: list[dict] = []
        offset = 0

        while True:
            data = self._get(base_url, params={"limit": PAGE_SIZE, "offset": offset})
            if not isinstance(data, dict) or "error" in data:
                break

            postings = data.get("content") or []
            for p in postings:
                results.append(self._normalize(p, slug, company.name))

            total = int(data.get("totalFound") or 0)
            if total:
                self.last_total_available = total
            offset += len(postings)

            if not fetch_all or not postings or offset >= total:
                break
            time.sleep(MIN_DELAY_API)

        return results

    # ── Normalization ─────────────────────────────────────────────────────────

    def _normalize(self, p: dict, slug: str, company_name: str) -> dict:
        loc = p.get("location") or {}
        city = loc.get("city") or ""
        state = loc.get("region") or ""
        country = loc.get("country") or ""
        is_remote = bool(loc.get("remote", False))
        location_raw = ", ".join(x for x in [city, state, country] if x)
        if is_remote:
            location_type = "REMOTE"
        elif location_raw:
            location_type = "ONSITE"
        else:
            location_type = "UNKNOWN"

        dept = (p.get("department") or {}).get("label") or ""

        emp_raw = ((p.get("typeOfEmployment") or {}).get("label") or "").lower()
        emp_map = {
            "full-time": "FULL_TIME",
            "permanent": "FULL_TIME",
            "part-time": "PART_TIME",
            "contract": "CONTRACT",
            "temporary": "TEMPORARY",
            "internship": "INTERN",
            "intern": "INTERN",
            "freelance": "CONTRACT",
        }
        employment_type = emp_map.get(emp_raw, "UNKNOWN")

        exp_raw = ((p.get("experienceLevel") or {}).get("label") or "").lower()
        exp_map = {
            "entry level": "ENTRY",
            "mid level": "MID",
            "senior level": "SENIOR",
            "director": "DIRECTOR",
            "executive": "EXECUTIVE",
            "manager": "MANAGER",
        }
        experience_level = exp_map.get(exp_raw, "UNKNOWN")

        job_id = p.get("id") or ""
        # Authoritative API slug is on each posting (case-sensitive); label tenant_id can differ.
        api_slug = ((p.get("company") or {}).get("identifier") or slug or "").strip()
        job_url = (
            p.get("ref")
            or f"https://jobs.smartrecruiters.com/{api_slug}/{job_id}"
        )
        api_posting_id = _normalize_posting_id_for_api(str(job_id)) if job_id else ""

        # ── Fetch full description from detail endpoint ────────────────────
        description = requirements = benefits = ""
        detail: Optional[dict] = None
        if api_posting_id and api_slug:
            try:
                detail = self._get(DETAIL_URL.format(slug=api_slug, job_id=api_posting_id))
                if isinstance(detail, dict) and "error" not in detail:
                    sections = (detail.get("jobAd") or {}).get("sections") or {}
                    description  = (sections.get("jobDescription") or {}).get("text") or ""
                    requirements = (sections.get("qualifications") or {}).get("text") or ""
                    benefits     = (sections.get("additionalInformation") or {}).get("text") or ""
            except Exception:
                pass  # fall through — description stays empty, no crash

        experience_level = _detect_experience_level(p.get("name") or "", description[:500])

        raw_payload = dict(p)
        if isinstance(detail, dict) and detail and "error" not in detail:
            raw_payload["active"] = detail.get("active", True)

        return {
            "external_id": job_id,
            "original_url": job_url,
            "apply_url": job_url,
            "title": p.get("name") or "",
            "company_name": company_name,
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
            "requirements": requirements,
            "benefits": benefits,
            "posted_date_raw": p.get("releasedDate") or "",
            "closing_date": "",
            "raw_payload": raw_payload,
        }

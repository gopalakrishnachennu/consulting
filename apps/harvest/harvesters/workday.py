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
"""
import time
from typing import Any

from .base import BaseHarvester, MIN_DELAY_API

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


class WorkdayHarvester(BaseHarvester):
    """Harvests jobs from Workday public REST API."""

    platform_slug = "workday"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        # tenant_id stored as "{full_subdomain}|{jobboard}"
        # e.g. "inotivco.wd5|EXT" or legacy "inotivco|EXT"
        # API uses bare company name (strip .wd{N} suffix for the CXS endpoint).
        import re as _re
        if "|" in tenant_id:
            full_subdomain, jobboard = tenant_id.split("|", 1)
            # Strip .wd{N} to get bare company name for API path
            tenant = _re.sub(r"\.wd\d+$", "", full_subdomain, flags=_re.I)
            paths_to_try = [jobboard] + [
                p for p in WORKDAY_PATHS_FALLBACK if p.lower() != jobboard.lower()
            ]
        else:
            tenant = _re.sub(r"\.wd\d+$", "", tenant_id, flags=_re.I)
            paths_to_try = [tenant] + WORKDAY_PATHS_FALLBACK

        for path in paths_to_try:
            url = (
                f"https://{tenant}.myworkdayjobs.com"
                f"/wday/cxs/{tenant}/{path}/jobs"
            )
            payload = {
                "appliedFacets": {},
                "limit": 20,
                "offset": 0,
                "searchText": "",
            }

            data = self._post(url, json_data=payload)

            if isinstance(data, dict) and "error" not in data:
                postings = data.get("jobPostings") or []
                if postings:
                    results = []
                    for job in postings:
                        ext_path = job.get("externalPath", "")
                        # Use full_subdomain (with .wd{N}) for the public job URL
                        job_domain = full_subdomain if "|" in tenant_id else tenant
                        job_url = (
                            f"https://{job_domain}.myworkdayjobs.com{ext_path}"
                            if ext_path
                            else ""
                        )
                        results.append({
                            "external_id": (
                                job.get("bulletFields", [""])[0]
                                if job.get("bulletFields")
                                else ""
                            ),
                            "original_url": job_url,
                            "title": job.get("title", ""),
                            "company_name": company.name,
                            "location": job.get("locationsText", ""),
                            "posted_date_raw": job.get("postedOn", ""),
                            "raw_payload": job,
                        })
                    return results   # found valid path — stop here

            # Respectful delay before trying next path
            time.sleep(MIN_DELAY_API)

        return []

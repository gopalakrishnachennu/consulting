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

# Workday tenant-specific career page path names to try (most common first)
WORKDAY_PATHS = [
    "External",
    "Careers",
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

        for path in WORKDAY_PATHS:
            url = (
                f"https://{tenant_id}.myworkdayjobs.com"
                f"/wday/cxs/{tenant_id}/{path}/jobs"
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
                        job_url = (
                            f"https://{tenant_id}.myworkdayjobs.com{ext_path}"
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

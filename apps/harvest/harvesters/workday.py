import time
from typing import Any

from .base import BaseHarvester

WORKDAY_PATHS = ["External", "Careers", "US", "All", "US-External", "Jobs", "Global"]


class WorkdayHarvester(BaseHarvester):
    """Harvests jobs from Workday REST API (no auth required for public postings)."""

    platform_slug = "workday"

    def fetch_jobs(self, company, tenant_id: str, since_hours: int = 24) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        results = []

        for path in WORKDAY_PATHS:
            url = (
                f"https://{tenant_id}.myworkdayjobs.com/wday/cxs/{tenant_id}/{path}/jobs"
            )
            payload = {
                "appliedFacets": {},
                "limit": 20,
                "offset": 0,
                "searchText": "",
            }

            data = self._post(url, json_data=payload)
            if isinstance(data, dict) and "error" not in data:
                postings = data.get("jobPostings", [])
                if postings is not None:
                    for job in postings:
                        ext_path = job.get("externalPath", "")
                        job_url = (
                            f"https://{tenant_id}.myworkdayjobs.com{ext_path}"
                            if ext_path
                            else ""
                        )
                        results.append({
                            "external_id": job.get("bulletFields", [""])[0]
                            if job.get("bulletFields")
                            else "",
                            "original_url": job_url,
                            "title": job.get("title", ""),
                            "company_name": company.name,
                            "location": job.get("locationsText", ""),
                            "posted_date_raw": job.get("postedOn", ""),
                            "raw_payload": job,
                        })
                    if results:
                        break

            time.sleep(0.5)

        return results

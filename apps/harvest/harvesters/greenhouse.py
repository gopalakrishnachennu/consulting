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
  - date filtering — only returns jobs updated within since_hours window
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import BaseHarvester

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class GreenhouseHarvester(BaseHarvester):
    """Harvests jobs from Greenhouse public JSON API."""

    platform_slug = "greenhouse"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)
        url = BASE_URL.format(token=tenant_id)

        data = self._get(url, params={"content": "true"})

        if isinstance(data, dict) and "error" in data:
            return []

        results = []
        for job in (data.get("jobs") or []):
            updated_raw = job.get("updated_at", "")
            if updated_raw:
                try:
                    updated_at = datetime.fromisoformat(
                        updated_raw.replace("Z", "+00:00")
                    )
                    if updated_at < cutoff:
                        continue
                except Exception:
                    pass

            loc = job.get("location", {})
            location = loc.get("name", "") if isinstance(loc, dict) else str(loc)

            dept = ""
            depts = job.get("departments", [])
            if depts and isinstance(depts, list):
                dept = depts[0].get("name", "")

            results.append({
                "external_id": str(job.get("id", "")),
                "original_url": job.get("absolute_url", ""),
                "title": job.get("title", ""),
                "company_name": company.name,
                "location": location,
                "department": dept,
                "posted_date_raw": updated_raw,
                "raw_payload": job,
            })

        return results

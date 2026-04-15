"""
LeverHarvester — Public Lever Posting API

Lever exposes a publicly documented API at:
  https://api.lever.co/v0/postings/{company}

This is their official public posting endpoint — no auth required.
Documentation: https://hire.lever.co/developer/postings

Compliance:
  - Honest User-Agent (inherited from BaseHarvester)
  - 1-second minimum delay (BaseHarvester rate limit)
  - Retry + backoff on server errors (BaseHarvester)
  - date filtering — only returns jobs created within since_hours window
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import BaseHarvester

BASE_URL = "https://api.lever.co/v0/postings/{company}?mode=json&limit=250"


class LeverHarvester(BaseHarvester):
    """Harvests jobs from Lever public REST API."""

    platform_slug = "lever"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        cutoff_ms = int(
            (datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)).timestamp()
            * 1000
        )
        url = BASE_URL.format(company=tenant_id)
        data = self._get(url)

        if isinstance(data, dict) and "error" in data:
            return []
        if not isinstance(data, list):
            return []

        results = []
        for job in data:
            created_ms = job.get("createdAt", 0)
            if created_ms and created_ms < cutoff_ms:
                continue

            cats = job.get("categories", {})
            commitment = cats.get("commitment", "").lower()
            job_type = "FULL_TIME" if "full" in commitment else "UNKNOWN"

            results.append({
                "external_id": job.get("id", ""),
                "original_url": job.get("hostedUrl", ""),
                "title": job.get("text", ""),
                "company_name": company.name,
                "location": cats.get("location", ""),
                "department": cats.get("department", ""),
                "job_type": job_type,
                "raw_payload": job,
            })

        return results

"""
AshbyHarvester — Public Ashby GraphQL API

Ashby exposes a publicly accessible GraphQL endpoint used by their own
job board widgets. It returns only published, public-facing postings.

Endpoint: https://jobs.ashbyhq.com/api/non-user-graphql

Compliance:
  - Honest User-Agent (inherited from BaseHarvester)
  - 1-second minimum delay (BaseHarvester rate limit)
  - Retry + backoff on server errors (BaseHarvester)
  - Only queries `jobPostingsForOrganization` — public data only
"""
from typing import Any

from .base import BaseHarvester

GQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"

ASHBY_QUERY = """
query ApiJobBoardJobPostingsForOrganization($organizationHostedJobsPageName: String!) {
  jobBoard: jobPostingsForOrganization(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    jobPostings {
      id
      title
      department { name }
      team { name }
      locationName
      employmentType
      isRemote
      descriptionHtml
      publishedDate
      externalLink
    }
  }
}
"""

ETYPE_MAP = {
    "FullTime":   "FULL_TIME",
    "PartTime":   "PART_TIME",
    "Contract":   "CONTRACT",
    "Internship": "INTERNSHIP",
}


class AshbyHarvester(BaseHarvester):
    """Harvests jobs from Ashby public GraphQL API."""

    platform_slug = "ashby"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        payload = {
            "operationName": "ApiJobBoardJobPostingsForOrganization",
            "query": ASHBY_QUERY,
            "variables": {"organizationHostedJobsPageName": tenant_id},
        }

        data = self._post(GQL_URL, json_data=payload)
        if isinstance(data, dict) and "error" in data:
            return []

        postings = (
            ((data.get("data") or {}).get("jobBoard") or {}).get("jobPostings") or []
        )

        results = []
        for job in postings:
            job_id = job.get("id", "")
            job_url = (
                job.get("externalLink")
                or f"https://jobs.ashbyhq.com/{tenant_id}/{job_id}"
            )
            dept = (job.get("department") or {}).get("name", "")

            results.append({
                "external_id": job_id,
                "original_url": job_url,
                "title": job.get("title", ""),
                "company_name": company.name,
                "location": job.get("locationName", ""),
                "is_remote": job.get("isRemote"),
                "department": dept,
                "job_type": ETYPE_MAP.get(job.get("employmentType", ""), "UNKNOWN"),
                "description_html": job.get("descriptionHtml", ""),
                "posted_date_raw": job.get("publishedDate", ""),
                "raw_payload": job,
            })

        return results

"""
BambooHRHarvester — Public BambooHR Jobs API

BambooHR exposes a public JSON API for career listings:
  List:   GET https://{company}.bamboohr.com/careers/list
  Detail: GET https://{company}.bamboohr.com/careers/{job_id}/detail

The list endpoint returns structured metadata (no description).
The detail endpoint returns the full job description HTML in:
  result.jobOpening.description

tenant_id = subdomain slug e.g. "netflix", "acme"
"""
import time
import re
from typing import Any
from urllib.parse import urljoin

from .base import BaseHarvester, MIN_DELAY_API, MIN_DELAY_SCRAPE, DEFAULT_TIMEOUT, BOT_USER_AGENT

_DETAIL_HEADERS = {
    "User-Agent": BOT_USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


class BambooHRHarvester(BaseHarvester):
    platform_slug = "bamboohr"

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24, fetch_all: bool = False
    ) -> list[dict[str, Any]]:
        if not tenant_id:
            return []

        slug = tenant_id.strip()

        # Path 1: JSON list endpoint (preferred — returns clean structured data)
        jobs = self._fetch_json_list(slug, company.name)
        if jobs:
            return jobs

        # Path 2: Embed JS script fallback (some tenants redirect list→HTML)
        return self._fetch_embed(slug, company.name)

    # ── Path 1: JSON list ─────────────────────────────────────────────────────

    def _fetch_json_list(self, slug: str, company_name: str) -> list[dict]:
        import time as _time
        url = f"https://{slug}.bamboohr.com/careers/list"

        self._enforce_rate_limit()
        try:
            resp = self._session.get(
                url,
                timeout=DEFAULT_TIMEOUT,
                headers={
                    "User-Agent": BOT_USER_AGENT,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            self._last_request_at = _time.monotonic()

            if not resp.ok:
                return []

            try:
                data = resp.json()
            except Exception:
                return []

            # BambooHR v2 returns {"meta": {"totalCount": N}, "result": [...]}
            # BambooHR v1 (legacy) returns a plain list
            if isinstance(data, dict):
                items = data.get("result") or data.get("results") or []
                total = (data.get("meta") or {}).get("totalCount") or len(items)
                self.last_total_available = int(total)
            elif isinstance(data, list):
                items = data
                self.last_total_available = len(items)
            else:
                return []

            results: list[dict] = []
            for j in items:
                record = self._normalize_list(j, slug, company_name)
                # Fetch full description from detail endpoint
                job_id = j.get("id") or ""
                if job_id:
                    try:
                        detail_url = f"https://{slug}.bamboohr.com/careers/{job_id}/detail"
                        dr = self._session.get(
                            detail_url,
                            timeout=DEFAULT_TIMEOUT,
                            headers=_DETAIL_HEADERS,
                        )
                        self._last_request_at = _time.monotonic()
                        if dr.ok:
                            d = dr.json()
                            jo = (d.get("result") or {}).get("jobOpening") or {}
                            record["description"] = jo.get("description") or ""
                            # Better location from detail if missing
                            if not record["city"]:
                                dloc = jo.get("location") or {}
                                record["city"]    = dloc.get("city") or ""
                                record["state"]   = dloc.get("state") or ""
                                record["country"] = dloc.get("addressCountry") or ""
                                if not record["location_raw"]:
                                    record["location_raw"] = ", ".join(
                                        x for x in [record["city"], record["state"], record["country"]] if x
                                    )
                    except Exception:
                        pass
                    _time.sleep(MIN_DELAY_API)
                results.append(record)
            return results

        except Exception:
            return []

    def _normalize_list(self, j: dict, slug: str, company_name: str) -> dict:
        # API v2 nests location under "location": {"city": ..., "state": ...}
        loc_obj = j.get("location") or {}
        city = (loc_obj.get("city") or j.get("locationCity") or j.get("city") or "").strip()
        state = (loc_obj.get("state") or j.get("locationState") or j.get("state") or "").strip()
        country = (loc_obj.get("country") or j.get("locationCountry") or j.get("country") or "").strip()

        is_remote = bool(j.get("isRemote", False))
        # locationType "1"=Remote, "2"=Hybrid, "3"=OnSite (BambooHR v2)
        loc_type_code = str(j.get("locationType") or "")
        if is_remote or loc_type_code == "1":
            is_remote = True

        location_raw = ", ".join(x for x in [city, state, country] if x)
        if is_remote:
            location_type = "REMOTE"
        elif loc_type_code == "2":
            location_type = "HYBRID"
        elif location_raw:
            location_type = "ONSITE"
        else:
            location_type = "UNKNOWN"

        # employmentStatusLabel values like "Regular Full-Time", "Full-Time", "Part-Time"
        emp_raw = (j.get("employmentStatusLabel") or "").lower()
        emp_map = {
            "regular full-time": "FULL_TIME",
            "full-time": "FULL_TIME",
            "full time": "FULL_TIME",
            "regular part-time": "PART_TIME",
            "part-time": "PART_TIME",
            "part time": "PART_TIME",
            "contractor": "CONTRACT",
            "contract": "CONTRACT",
            "temporary": "TEMPORARY",
            "intern": "INTERNSHIP",
            "internship": "INTERNSHIP",
        }
        employment_type = emp_map.get(emp_raw, "UNKNOWN")

        job_id = j.get("id") or ""
        # v2 API may have no link — construct from slug + id
        job_url = (
            j.get("link")
            or j.get("url")
            or f"https://{slug}.bamboohr.com/careers/{job_id}"
        )

        return {
            "external_id": str(job_id),
            "original_url": job_url,
            "apply_url": job_url,
            "title": j.get("jobOpeningName") or j.get("title") or "",
            "company_name": company_name,
            "department": j.get("departmentLabel") or j.get("department") or "",
            "team": "",
            "location_raw": location_raw,
            "city": city,
            "state": state,
            "country": country,
            "is_remote": is_remote,
            "location_type": location_type,
            "employment_type": employment_type,
            "experience_level": "UNKNOWN",
            "salary_min": None,
            "salary_max": None,
            "salary_currency": "USD",
            "salary_period": "",
            "salary_raw": "",
            "description": "",
            "requirements": "",
            "benefits": "",
            "posted_date_raw": j.get("datePosted") or j.get("created_at") or "",
            "closing_date": "",
            "raw_payload": j,
        }

    # ── Path 2: HTML embed scraper fallback ───────────────────────────────────

    def _fetch_embed(self, slug: str, company_name: str) -> list[dict]:
        import time as _time
        url = f"https://{slug}.bamboohr.com/careers"

        self._enforce_rate_limit()
        try:
            resp = self._session.get(
                url,
                timeout=DEFAULT_TIMEOUT,
                headers={
                    "User-Agent": BOT_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            self._last_request_at = _time.monotonic()
            if not resp.ok:
                return []
            return self._parse_careers_html(resp.text, slug, company_name)
        except Exception:
            return []

    def _parse_careers_html(self, html: str, slug: str, company_name: str) -> list[dict]:
        results: list[dict] = []
        seen: set[str] = set()
        base = f"https://{slug}.bamboohr.com"

        # BambooHR career page: links like /careers/{id}-{title}
        for m in re.finditer(
            r'<a[^>]*href=["\'](/careers/(\d+)[^"\']*)["\'][^>]*>([\s\S]*?)</a>',
            html, re.I,
        ):
            path = m.group(1)
            job_id = m.group(2)
            link_html = m.group(3)
            abs_url = urljoin(base, path)
            if abs_url in seen:
                continue
            seen.add(abs_url)
            title = re.sub(r"<[^>]+>", " ", link_html).strip()
            if not title or len(title) < 3 or len(title) > 300:
                continue
            results.append({
                "external_id": job_id,
                "original_url": abs_url,
                "apply_url": abs_url,
                "title": title,
                "company_name": company_name,
                "department": "",
                "team": "",
                "location_raw": "",
                "city": "",
                "state": "",
                "country": "",
                "is_remote": False,
                "location_type": "UNKNOWN",
                "employment_type": "UNKNOWN",
                "experience_level": "UNKNOWN",
                "salary_min": None,
                "salary_max": None,
                "salary_currency": "USD",
                "salary_period": "",
                "salary_raw": "",
                "description": "",
                "requirements": "",
                "benefits": "",
                "posted_date_raw": "",
                "closing_date": "",
                "raw_payload": {"source": "html_fallback"},
            })

        return results

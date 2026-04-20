"""
Job Jarvis — paste any job URL, extract everything.

Multi-strategy extraction pipeline:
  1. URL pattern → detect ATS platform
  2. Platform-specific API (Greenhouse, Lever, Ashby) — richest data
  3. JSON-LD structured data (@type: JobPosting) — widely supported
  4. Open Graph + meta fallback
  5. BeautifulSoup HTML scrape — last resort

Returns a normalised dict ready to persist as a RawJob.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _str_val(val) -> str:
    """
    Safely coerce a JSON-LD value to a plain string.

    JSON-LD fields like addressCountry / addressRegion can be either:
      - a plain string:  "US"
      - a schema.org object:  {"@type": "Country", "name": "United States"}
    This helper extracts the "name" key for objects, or returns str(val).
    """
    if not val:
        return ""
    if isinstance(val, dict):
        return str(val.get("name") or val.get("@id") or "")
    return str(val)


# Use an honest, human-readable UA — same policy as the bulk harvesters.
_JARVIS_UA = (
    "Mozilla/5.0 (compatible; GoCareers-Jarvis/1.0; "
    "+https://gocareers.io/bot; contact: admin@gocareers.io)"
)
_TIMEOUT = 18  # seconds

# ── Platform URL patterns ─────────────────────────────────────────────────────
PLATFORM_PATTERNS: dict[str, list[str]] = {
    "greenhouse":      ["boards.greenhouse.io", "boards-api.greenhouse.io"],
    "lever":           ["jobs.lever.co"],
    "ashby":           ["jobs.ashbyhq.com", "ashbyhq.com/jobs"],
    "workday":         ["myworkdayjobs.com"],
    "smartrecruiters": ["smartrecruiters.com/jobs", "jobs.smartrecruiters.com"],
    "workable":        ["apply.workable.com", "jobs.workable.com"],
    "bamboohr":        ["bamboohr.com/careers", "bamboohr.com/jobs"],
    "recruitee":       [".recruitee.com/o/", "recruitee.com/o/"],
    "icims":           [".icims.com/jobs/", "icims.com/jobs"],
    "jobvite":         ["jobs.jobvite.com"],
    "taleo":           ["taleo.net/careersection"],
    "oracle":          [".oraclecloud.com/hcmUI/CandidateExperience"],
    "ultipro":         ["recruiting.ultipro.com"],
    "dayforce":        ["jobs.dayforcehcm.com"],
    "breezy":          [".breezy.hr/p/"],
    "teamtailor":      [".teamtailor.com/jobs/"],
    "zoho":            ["jobs.zoho.com/portal/", ".zohorecruit.com/jobs/"],
    "linkedin":        ["linkedin.com/jobs"],
    "indeed":          ["indeed.com/viewjob", "indeed.com/jobs"],
    "glassdoor":       ["glassdoor.com/job-listing"],
    "builtin":         ["builtin.com/job"],
    "wellfound":       ["wellfound.com/jobs", "angel.co/jobs"],
    "dice":            ["dice.com/jobs"],
    "ziprecruiter":    ["ziprecruiter.com/jobs"],
}


# ── Public API ────────────────────────────────────────────────────────────────

class JobJarvis:
    """Paste-any-URL job extractor."""

    def __init__(self, timeout: int = _TIMEOUT):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _JARVIS_UA})

    # ── Main entry point ──────────────────────────────────────────────────────

    def ingest(self, url: str) -> dict[str, Any]:
        """
        Fetch *url* and return a normalised job dict.

        The returned dict contains:
          - All keys that map 1-to-1 to RawJob fields
          - ``strategy``  — how the data was extracted
          - ``error``     — non-empty string if extraction failed
          - ``platform_slug`` — detected ATS slug (may be empty)
        """
        url = url.strip()
        result = _empty_job(url)

        platform_slug = _detect_platform(url)
        result["platform_slug"] = platform_slug

        # ── Strategy 1: Platform-specific API ────────────────────────────────
        # Some platform APIs (e.g. Workday CXS search) return structured
        # metadata (title, location, ID) but NOT the full description.
        # Only return early if the API gave us a useful description.
        # Otherwise fall through so JSON-LD / HTML scrape can fill it in.
        if platform_slug:
            try:
                api_data = self._platform_api(url, platform_slug)
                if api_data:
                    result.update(api_data)
                    result["strategy"] = f"api:{platform_slug}"
                    if result.get("description"):
                        # Description present — we're done.
                        _enrich_inferred(result)
                        return result
                    # No description yet — keep metadata, continue to HTML fetch
                    logger.debug(
                        "Jarvis platform-API (%s) had no description — "
                        "falling through to page fetch",
                        platform_slug,
                    )
            except Exception as exc:
                logger.warning("Jarvis platform-API failed (%s): %s", platform_slug, exc)

        # ── Fetch the page ────────────────────────────────────────────────────
        try:
            html, final_url = self._fetch(url)
            result["original_url"] = final_url
            result["apply_url"] = final_url
        except requests.exceptions.Timeout:
            result["error"] = "The page took too long to respond (timeout)."
            return result
        except requests.exceptions.ConnectionError:
            result["error"] = "Could not connect to the URL — check it's publicly accessible."
            return result
        except requests.exceptions.HTTPError as exc:
            result["error"] = f"HTTP {exc.response.status_code} — page returned an error."
            return result
        except Exception as exc:
            result["error"] = f"Failed to fetch URL: {exc}"
            return result

        # ── Strategy 2: JSON-LD structured data ───────────────────────────────
        # Many career sites emit JobPosting JSON-LD with title/location but an empty
        # or useless description. We used to return immediately after JSON-LD, which
        # skipped HTML scrape — backfill then left those jobs blank while Greenhouse /
        # Lever (API-first with full body) looked fine.
        jsonld = _try_jsonld(html)
        if jsonld:
            # Merge: API metadata wins for non-empty fields; JSON-LD fills gaps.
            # "UNKNOWN" counts as empty — JSON-LD can always upgrade it.
            _EMPTY = ("", "UNKNOWN", None, [], {})
            for k, v in jsonld.items():
                cur = result.get(k)
                if k == "description":
                    # Always take description from JSON-LD when it has real content
                    if v:
                        result[k] = v
                elif v not in _EMPTY and cur in _EMPTY:
                    result[k] = v
            if result.get("strategy", "").startswith("api:"):
                result["strategy"] = f"{result['strategy']}+jsonld"
            else:
                result["strategy"] = "jsonld"
            _enrich_inferred(result)
            desc = (result.get("description") or "").strip()
            if desc:
                return result
            logger.debug(
                "Jarvis: JSON-LD matched but no description — falling through to HTML scrape (%s)",
                platform_slug or "unknown",
            )

        # ── Strategy 3: HTML scrape fallback ──────────────────────────────────
        scraped = _try_html_scrape(html, final_url)
        if scraped and (scraped.get("title") or scraped.get("description")):
            _EMPTY = ("", "UNKNOWN", None, [], {})
            for k, v in scraped.items():
                cur = result.get(k)
                if k == "description":
                    if v:
                        result[k] = v
                elif v not in _EMPTY and cur in _EMPTY:
                    result[k] = v
            if result.get("strategy", "").startswith("api:"):
                result["strategy"] = f"{result['strategy']}+html"
            elif jsonld:
                result["strategy"] = "jsonld+html"
            else:
                result["strategy"] = "html_scrape"
            _enrich_inferred(result)
            return result

        result["error"] = (
            "Could not extract job data from this page. "
            "The site may block bots, require login, or use a format we don't support yet."
        )
        return result

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _fetch(self, url: str) -> tuple[str, str]:
        resp = self._session.get(
            url,
            timeout=self.timeout,
            allow_redirects=True,
            headers={
                # Some sites enforce Accept-Language; set a safe default.
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        resp.raise_for_status()
        return resp.text, resp.url

    # ── Platform API strategies ───────────────────────────────────────────────

    def _platform_api(self, url: str, slug: str) -> Optional[dict]:
        dispatch = {
            "greenhouse": self._greenhouse,
            "lever": self._lever,
            "ashby": self._ashby,
            "workable": self._workable,
            "workday": self._workday,
            "bamboohr": self._bamboohr,
            "smartrecruiters": self._smartrecruiters,
            "recruitee": self._recruitee,
            "icims": self._icims,
            "jobvite": self._jobvite,
            "taleo": self._taleo,
            "oracle": self._oracle,
            "ultipro": self._ultipro,
            "dayforce": self._dayforce,
            "breezy": self._breezy,
            "teamtailor": self._teamtailor,
            "zoho": self._zoho,
        }
        fn = dispatch.get(slug)
        return fn(url) if fn else None

    # ── Workday ───────────────────────────────────────────────────────────────

    def _workday(self, url: str) -> Optional[dict]:
        """
        Extract a single Workday job via the CXS search API.

        Workday detail page URLs look like:
          https://{sub}.myworkdayjobs.com/{locale}/{jobboard}/details/{title}_{jobId}
          https://{sub}.myworkdayjobs.com/{jobboard}/job/{loc}/{title}_{jobId}

        We extract (subdomain, tenant, jobboard, jobId) then POST to the
        search API filtering by the job ID — same endpoint used by the
        bulk harvester, but limited to 1 result.
        """
        # Pattern A: /details/{title}_{jobId}  (new-style)
        # Pattern B: /job/{loc}/{title}_{jobId}  (old-style)
        m = re.search(
            r"([\w-]+)\.myworkdayjobs\.com"
            r"/(?:[a-z]{2}-[A-Z]{2}/)?"          # optional locale: en-US/
            r"([^/]+)"                             # jobboard
            r"/(?:details|job)/[^_]*"              # /details/ or /job/{loc}/
            r"_(R[\w-]+)",                         # _{jobId}  e.g. _R01162544
            url, re.I,
        )
        if not m:
            return None

        full_subdomain = m.group(1)
        jobboard = m.group(2)
        job_id = m.group(3)

        # tenant = subdomain minus .wd1/.wd5 suffix
        tenant = re.sub(r"\.wd\d+$", "", full_subdomain, flags=re.I)

        api_url = (
            f"https://{full_subdomain}.myworkdayjobs.com"
            f"/wday/cxs/{tenant}/{jobboard}/jobs"
        )
        payload = {
            "appliedFacets": {},
            "limit": 5,
            "offset": 0,
            "searchText": job_id,   # search by job ID — Workday returns exact match
        }
        try:
            resp = self._session.post(api_url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        postings = data.get("jobPostings") or []
        if not postings:
            return None

        # Find the posting that matches our job_id
        job = next(
            (j for j in postings if job_id.lower() in (j.get("externalPath") or "").lower()),
            postings[0],
        )

        ext_path = job.get("externalPath", "")
        job_url = f"https://{full_subdomain}.myworkdayjobs.com/{jobboard}{ext_path}" if ext_path else url

        loc = job.get("locationsText", "")
        bullet = job.get("bulletFields") or []
        ext_id = bullet[0] if bullet else job_id

        # Same body extraction as WorkdayHarvester._normalize_workday_job — search hits
        # often include full HTML here; Jarvis previously omitted it so backfill stayed empty.
        description = (
            (job.get("jobDescription") or {}).get("content", "")
            or (job.get("jobPostingDescription") or {}).get("content", "")
            or job.get("shortDescription", "")
            or ""
        )
        if isinstance(description, dict):
            description = description.get("content", "") or ""

        return {
            "title": job.get("title", ""),
            "company_name": full_subdomain.split(".")[0].replace("-", " ").title(),
            "location_raw": loc,
            "is_remote": "remote" in loc.lower(),
            "location_type": _infer_location_type(loc),
            "description": description,
            "external_id": ext_id,
            "original_url": job_url,
            "apply_url": job_url,
            "raw_payload": job,
        }

    # ── Greenhouse ────────────────────────────────────────────────────────────

    def _greenhouse(self, url: str) -> Optional[dict]:
        # https://boards.greenhouse.io/{company}/jobs/{id}
        m = re.search(
            r"boards(?:-api)?\.greenhouse\.io/([^/?#]+)/jobs/(\d+)",
            url, re.I,
        )
        if not m:
            return None
        company_slug, job_id = m.group(1), m.group(2)
        api_url = (
            f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs/{job_id}"
            "?questions=true"
        )
        try:
            resp = self._session.get(api_url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        loc = (data.get("location") or {}).get("name", "")
        depts = data.get("departments") or []
        dept = depts[0].get("name", "") if depts else ""

        company_name = company_slug.replace("-", " ").title()
        # Try to get a nicer name from metadata
        for meta in data.get("metadata") or []:
            if meta.get("name", "").lower() in ("company", "company name"):
                company_name = meta.get("value") or company_name
                break

        return {
            "title": data.get("title", ""),
            "company_name": company_name,
            "location_raw": loc,
            "is_remote": "remote" in loc.lower(),
            "location_type": _infer_location_type(loc),
            "description": data.get("content", ""),
            "department": dept,
            "external_id": str(data.get("id", "")),
            "posted_date_raw": data.get("updated_at", ""),
            "apply_url": data.get("absolute_url") or url,
            "original_url": data.get("absolute_url") or url,
            "raw_payload": data,
        }

    # ── Lever ────────────────────────────────────────────────────────────────

    def _lever(self, url: str) -> Optional[dict]:
        # https://jobs.lever.co/{company}/{uuid}
        m = re.search(
            r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})",
            url, re.I,
        )
        if not m:
            return None
        company_slug, job_id = m.group(1), m.group(2)
        api_url = f"https://api.lever.co/v0/postings/{company_slug}/{job_id}"
        try:
            resp = self._session.get(api_url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        cats = data.get("categories") or {}
        loc = cats.get("location", "")

        # Build description from lists + body
        html_parts: list[str] = []
        for section in data.get("lists") or []:
            heading = section.get("text", "")
            content = section.get("content", "")
            if heading:
                html_parts.append(f"<h4>{heading}</h4>")
            if content:
                html_parts.append(f"<ul><li>{'</li><li>'.join(content.split('<br>'))}</li></ul>")
        html_parts.append(data.get("descriptionBody") or data.get("description") or "")

        return {
            "title": data.get("text", ""),
            "company_name": data.get("company") or company_slug.replace("-", " ").title(),
            "location_raw": loc,
            "is_remote": "remote" in loc.lower(),
            "location_type": _infer_location_type(loc),
            "description": "".join(html_parts),
            "department": cats.get("department", ""),
            "team": cats.get("team", ""),
            "employment_type": _map_employment(cats.get("commitment", "")),
            "external_id": job_id,
            "apply_url": data.get("applyUrl") or url,
            "original_url": url,
            "raw_payload": data,
        }

    # ── Ashby ─────────────────────────────────────────────────────────────────

    def _ashby(self, url: str) -> Optional[dict]:
        # https://jobs.ashbyhq.com/{company}/{uuid}
        m = re.search(
            r"(?:jobs\.ashbyhq\.com|ashbyhq\.com/jobs)/([^/?#]+)/([0-9a-f-]{36})",
            url, re.I,
        )
        if not m:
            return None
        company_slug, job_id = m.group(1), m.group(2)

        # Fetch the full job board then find the job by id
        api_url = "https://api.ashbyhq.com/posting-api/job-board/ashby"
        payload = {"organizationHostedJobsPageName": company_slug}
        try:
            resp = self._session.post(api_url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            jobs = (resp.json() or {}).get("jobPostings") or []
            job = next((j for j in jobs if j.get("id") == job_id), None)
            if not job:
                return None
        except Exception:
            return None

        loc = job.get("location", "")
        return {
            "title": job.get("title", ""),
            "company_name": job.get("organizationName") or company_slug.replace("-", " ").title(),
            "location_raw": loc,
            "is_remote": bool(job.get("isRemote")),
            "location_type": "REMOTE" if job.get("isRemote") else _infer_location_type(loc),
            "description": job.get("descriptionHtml", ""),
            "department": job.get("departmentName", ""),
            "employment_type": _map_employment(job.get("employmentType", "")),
            "external_id": job_id,
            "apply_url": url,
            "original_url": url,
            "raw_payload": job,
        }

    # ── Workable ───────────────────────────────────────────────────────────────

    def _workable(self, url: str) -> Optional[dict]:
        # https://apply.workable.com/{company}/j/{id}
        m = re.search(
            r"(?:apply|jobs)\.workable\.com/([^/?#]+)/j/([^/?#]+)",
            url, re.I,
        )
        if not m:
            return None
        company_slug, job_shortcode = m.group(1), m.group(2)
        api_url = f"https://apply.workable.com/api/v1/accounts/{company_slug}/jobs/{job_shortcode}"
        try:
            resp = self._session.get(api_url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        loc_parts = [
            data.get("city", ""),
            data.get("state", ""),
            data.get("country", ""),
        ]
        loc = ", ".join(p for p in loc_parts if p)
        return {
            "title": data.get("title", ""),
            "company_name": data.get("full_title", company_slug).split(" — ")[0].strip(),
            "location_raw": loc,
            "city": data.get("city", ""),
            "state": data.get("state", ""),
            "country": data.get("country_code", ""),
            "is_remote": data.get("remote", False),
            "location_type": "REMOTE" if data.get("remote") else _infer_location_type(loc),
            "employment_type": _map_employment(data.get("employment_type", "")),
            "description": data.get("description", ""),
            "requirements": data.get("requirements", ""),
            "benefits": data.get("benefits", ""),
            "department": data.get("department", ""),
            "external_id": job_shortcode,
            "apply_url": url,
            "original_url": url,
            "raw_payload": data,
        }

    def _bamboohr(self, url: str) -> Optional[dict]:
        """
        BambooHR public job JSON — same /careers/{id}/detail endpoint as BambooHRHarvester.

        Raw jobs store URLs like https://{tenant}.bamboohr.com/careers/{id} (no HTML body).
        """
        m = re.search(
            r"https?://([^/?#\s]+)\.bamboohr\.com/careers/(\d+)",
            url,
            re.I,
        )
        if not m:
            return None
        host_slug, job_id = m.group(1), m.group(2)
        detail_url = f"https://{host_slug}.bamboohr.com/careers/{job_id}/detail"
        try:
            resp = self._session.get(
                detail_url,
                timeout=self.timeout,
                headers={
                    "User-Agent": _JARVIS_UA,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return None

        jo = (payload.get("result") or {}).get("jobOpening") or {}
        desc = (jo.get("description") or "").strip()
        title = (
            (jo.get("jobTitle") or jo.get("title") or jo.get("jobOpeningName") or "")
        ).strip()
        dloc = jo.get("location") or {}
        city = (dloc.get("city") or "").strip()
        state = (dloc.get("state") or "").strip()
        country = (dloc.get("addressCountry") or dloc.get("country") or "").strip()
        loc = ", ".join(p for p in (city, state, country) if p)

        return {
            "title": title,
            "company_name": host_slug.replace("-", " ").title(),
            "location_raw": loc or "",
            "city": city,
            "state": state,
            "country": country,
            "description": desc,
            "department": (jo.get("departmentLabel") or jo.get("department") or "")[:256],
            "external_id": str(job_id),
            "original_url": url.strip(),
            "apply_url": url.strip(),
            "raw_payload": payload,
        }

    def _smartrecruiters(self, url: str) -> Optional[dict]:
        """
        SmartRecruiters public posting detail API (same as SmartRecruitersHarvester).

        URLs: https://jobs.smartrecruiters.com/{companySlug}/{postingId}
        """
        m = re.search(
            r"https?://(?:jobs\.)?smartrecruiters\.com/([^/?#]+)/([^/?#]+)",
            url,
            re.I,
        )
        if not m:
            return None
        slug, job_id = m.group(1), m.group(2)
        detail_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"
        try:
            resp = self._session.get(detail_url, timeout=self.timeout)
            resp.raise_for_status()
            detail = resp.json()
        except Exception:
            return None

        if not isinstance(detail, dict) or "error" in detail:
            return None

        sections = (detail.get("jobAd") or {}).get("sections") or {}
        description = (sections.get("jobDescription") or {}).get("text") or ""
        requirements = (sections.get("qualifications") or {}).get("text") or ""
        benefits = (sections.get("additionalInformation") or {}).get("text") or ""

        loc = (detail.get("location") or {}) if isinstance(detail.get("location"), dict) else {}
        city = (loc.get("city") or "") if isinstance(loc, dict) else ""
        state = (loc.get("region") or "") if isinstance(loc, dict) else ""
        country = (loc.get("country") or "") if isinstance(loc, dict) else ""
        location_raw = ", ".join(x for x in (city, state, country) if x)

        ref = detail.get("ref") or url
        title = (detail.get("name") or "")[:512]

        return {
            "title": title,
            "company_name": slug.replace("-", " ").title(),
            "location_raw": location_raw,
            "city": city,
            "state": state,
            "country": country,
            "description": description,
            "requirements": requirements,
            "benefits": benefits,
            "external_id": job_id,
            "original_url": ref,
            "apply_url": ref,
            "raw_payload": detail,
        }

    def _recruitee(self, url: str) -> Optional[dict]:
        """
        Recruitee public offers API — match one offer by /o/{slug} path.

        List endpoint returns descriptions (same as RecruiteeHarvester).
        """
        m = re.search(
            r"https?://([\w-]+)\.recruitee\.com/o/([^/?#]+)",
            url,
            re.I,
        )
        if not m:
            return None
        tenant, opening_slug = m.group(1), m.group(2)
        api_url = f"https://{tenant}.recruitee.com/api/offers/"
        try:
            resp = self._session.get(api_url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        if not isinstance(data, dict):
            return None
        offers = data.get("offers") or []
        offer = next(
            (
                o
                for o in offers
                if (o.get("slug") or "") == opening_slug
                or str(o.get("id") or "") == opening_slug
                or (opening_slug in (o.get("careers_url") or ""))
            ),
            None,
        )
        if not offer:
            return None

        city = offer.get("city") or ""
        country = offer.get("country") or ""
        location_raw = ", ".join(x for x in (city, country) if x)

        return {
            "title": offer.get("title") or "",
            "company_name": tenant.replace("-", " ").title(),
            "location_raw": location_raw,
            "city": city,
            "country": country,
            "description": offer.get("description") or "",
            "requirements": offer.get("requirements") or "",
            "external_id": str(offer.get("id") or ""),
            "original_url": url.strip(),
            "apply_url": (offer.get("careers_url") or url).strip(),
            "raw_payload": offer,
        }

    # ── iCIMS ─────────────────────────────────────────────────────────────────

    def _icims(self, url: str) -> Optional[dict]:
        """
        iCIMS job detail pages are server-rendered HTML.
        URLs: https://{tenant}.icims.com/jobs/{id}/job
        Fetch the detail page and scrape the description container.
        """
        m = re.search(r"([\w-]+)\.icims\.com/jobs/(\d+)", url, re.I)
        if not m:
            return None
        tenant, job_id = m.group(1), m.group(2)
        detail_url = f"https://{tenant}.icims.com/jobs/{job_id}/job"
        try:
            resp = self._session.get(
                detail_url, timeout=self.timeout,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title = ""
        el = soup.select_one("h1.iCIMS_JobTitle") or soup.select_one("h1")
        if el:
            title = el.get_text(" ", strip=True)

        desc_parts = []
        for sel in (
            ".iCIMS_JobContent", ".iCIMS_InfoMsg_Job",
            "[class*='job-description']", "[class*='jobDescription']",
            "[itemprop='description']", "article", "main",
        ):
            el = soup.select_one(sel)
            if el and len(el.get_text(" ", strip=True)) >= 72:
                desc_parts.append(str(el))
                break

        location_raw = ""
        for sel in (".iCIMS_JobHeaderData", "[class*='location']"):
            el = soup.select_one(sel)
            if el:
                location_raw = el.get_text(" ", strip=True)[:200]
                break

        return {
            "title": title,
            "company_name": tenant.replace("-", " ").replace("careers", "").strip().title(),
            "location_raw": location_raw,
            "description": "\n".join(desc_parts),
            "external_id": job_id,
            "original_url": detail_url,
            "apply_url": detail_url,
            "raw_payload": {"source": "icims_scrape"},
        }

    # ── Jobvite ───────────────────────────────────────────────────────────────

    def _jobvite(self, url: str) -> Optional[dict]:
        """
        Jobvite detail pages: https://jobs.jobvite.com/{company}/job/{id}
        Server-rendered HTML with job description in the page body.
        """
        m = re.search(
            r"jobs\.jobvite\.com/([^/?#]+)/job/([^/?#]+)", url, re.I,
        )
        if not m:
            return None
        company_slug, job_id = m.group(1), m.group(2)
        detail_url = f"https://jobs.jobvite.com/{company_slug}/job/{job_id}"
        try:
            resp = self._session.get(
                detail_url, timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title = ""
        el = soup.select_one("h2.jv-header") or soup.select_one("h1") or soup.select_one("h2")
        if el:
            title = el.get_text(" ", strip=True)

        description = ""
        for sel in (".jv-job-detail-description", ".jv-job-detail", "[class*='job-description']", "article", "main"):
            el = soup.select_one(sel)
            if el and len(el.get_text(" ", strip=True)) >= 72:
                description = str(el)
                break

        location_raw = ""
        el = soup.select_one(".jv-job-detail-meta .location") or soup.select_one("[class*='location']")
        if el:
            location_raw = el.get_text(" ", strip=True)

        return {
            "title": title,
            "company_name": company_slug.replace("-", " ").title(),
            "location_raw": location_raw,
            "description": description,
            "external_id": job_id,
            "original_url": detail_url,
            "apply_url": detail_url,
            "raw_payload": {"source": "jobvite_scrape"},
        }

    # ── Taleo ─────────────────────────────────────────────────────────────────

    def _taleo(self, url: str) -> Optional[dict]:
        """
        Taleo detail pages: .../careersection/{section}/jobdetail.ftl?job={id}
        Server-rendered HTML (FTL template).
        """
        m = re.search(
            r"([\w-]+)\.taleo\.net/careersection/([^/]+)/jobdetail\.ftl\?.*?job=([^&]+)",
            url, re.I,
        )
        if not m:
            return None
        subdomain, section, job_ref = m.group(1), m.group(2), m.group(3)
        detail_url = (
            f"https://{subdomain}.taleo.net/careersection/{section}"
            f"/jobdetail.ftl?job={job_ref}&lang=en"
        )
        try:
            resp = self._session.get(
                detail_url, timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": _JARVIS_UA},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title = ""
        for sel in ("#requisitionDescriptionInterface\\.reqTitleLinkAction\\.row1", "h1", ".pageTitle"):
            el = soup.select_one(sel)
            if el:
                title = el.get_text(" ", strip=True)
                if title:
                    break

        description = ""
        for sel in (
            "#requisitionDescriptionInterface\\.ID1702\\.row1",
            "[id*='requisitionDescription']",
            ".contentlinepanel",
            "[class*='job-description']",
            "article", "main",
        ):
            el = soup.select_one(sel)
            if el and len(el.get_text(" ", strip=True)) >= 72:
                description = str(el)
                break

        location_raw = ""
        for sel in ("[id*='locationDescription']", "[class*='location']"):
            el = soup.select_one(sel)
            if el:
                location_raw = el.get_text(" ", strip=True)[:200]
                break

        return {
            "title": title,
            "company_name": subdomain.replace("-", " ").title(),
            "location_raw": location_raw,
            "description": description,
            "external_id": job_ref,
            "original_url": detail_url,
            "apply_url": detail_url,
            "raw_payload": {"source": "taleo_scrape"},
        }

    # ── Oracle HCM CE ─────────────────────────────────────────────────────────

    def _oracle(self, url: str) -> Optional[dict]:
        """
        Oracle HCM Candidate Experience — single-requisition REST API.
        URL: https://{sub}.oraclecloud.com/hcmUI/CandidateExperience/en/sites/{site}/job/{reqId}
        API: GET .../hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?...
        """
        m = re.search(
            r"([\w.-]+)\.oraclecloud\.com/hcmUI/CandidateExperience"
            r"/\w+/sites/([^/]+)/job/(\d+)",
            url, re.I,
        )
        if not m:
            return None
        subdomain, site_id, req_id = m.group(1), m.group(2), m.group(3)

        api_url = (
            f"https://{subdomain}.oraclecloud.com"
            f"/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        )
        params = {
            "onlyData": "true",
            "expand": "all",
            "finder": f"findReqDetails;Id={req_id},siteNumber={site_id}",
        }
        try:
            resp = self._session.get(
                api_url, params=params, timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        items = data.get("items") or []
        if not items:
            return None
        req = items[0]

        title = req.get("Title") or ""
        desc = req.get("ExternalDescriptionStr") or req.get("ShortDescriptionStr") or ""
        loc_raw = req.get("PrimaryLocation") or ""

        return {
            "title": title,
            "company_name": subdomain.split(".")[0].replace("-", " ").title(),
            "location_raw": loc_raw,
            "description": desc,
            "department": req.get("Organization") or "",
            "external_id": req_id,
            "original_url": url,
            "apply_url": url,
            "raw_payload": req,
        }

    # ── UltiPro / UKG ────────────────────────────────────────────────────────

    def _ultipro(self, url: str) -> Optional[dict]:
        """
        UltiPro OpportunityDetail — fetch single-job HTML or JSON.
        URL: .../recruiting.ultipro.com/{code}/JobBoard/{guid}/OpportunityDetail?opportunityId={id}
        """
        m = re.search(
            r"recruiting\.ultipro\.com/([^/]+)/JobBoard/([^/]+)"
            r"/OpportunityDetail\?opportunityId=([^&#]+)",
            url, re.I,
        )
        if not m:
            return None
        company_code, board_guid, opp_id = m.group(1), m.group(2), m.group(3)

        detail_api = (
            f"https://recruiting.ultipro.com/{company_code}/JobBoard/{board_guid}"
            f"/OpportunityDetail/GetOpportunityDetail?opportunityId={opp_id}"
        )
        try:
            resp = self._session.get(
                detail_api, timeout=self.timeout,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": _JARVIS_UA,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return self._ultipro_html_fallback(url)

        if not isinstance(data, dict):
            return self._ultipro_html_fallback(url)

        title = data.get("Title") or data.get("title") or ""
        desc = data.get("Description") or data.get("description") or ""
        loc = data.get("Location") or data.get("location") or ""

        if not desc:
            return self._ultipro_html_fallback(url)

        return {
            "title": title,
            "company_name": company_code.replace("-", " ").title(),
            "location_raw": loc if isinstance(loc, str) else "",
            "description": desc,
            "external_id": opp_id,
            "original_url": url,
            "apply_url": url,
            "raw_payload": data,
        }

    def _ultipro_html_fallback(self, url: str) -> Optional[dict]:
        """Fetch the OpportunityDetail page and scrape HTML."""
        try:
            resp = self._session.get(
                url, timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            resp.raise_for_status()
        except Exception:
            return None
        return None  # let generic HTML scrape handle it

    # ── Dayforce ──────────────────────────────────────────────────────────────

    def _dayforce(self, url: str) -> Optional[dict]:
        """
        Dayforce job detail via GEO API.
        URL: .../jobs.dayforcehcm.com/en-US/{slug}/CANDIDATEPORTAL/jobs/{id}
        API: GET .../api/geo/{slug}/jobposting/{id}
        """
        m = re.search(
            r"jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/([^/]+)/jobs/(\d+)",
            url, re.I,
        )
        if not m:
            return None
        slug, portal, job_id = m.group(1), m.group(2), m.group(3)

        detail_url = f"https://jobs.dayforcehcm.com/api/geo/{slug}/jobposting/{job_id}"
        try:
            resp = self._session.get(
                detail_url, timeout=self.timeout,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": url,
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        title = data.get("JobTitle") or data.get("title") or ""
        desc = data.get("Description") or data.get("description") or ""
        loc = data.get("JobLocation") or data.get("location") or ""

        return {
            "title": title,
            "company_name": slug.replace("-", " ").title(),
            "location_raw": loc,
            "description": desc,
            "external_id": job_id,
            "original_url": url,
            "apply_url": url,
            "raw_payload": data,
        }

    # ── Breezy HR ─────────────────────────────────────────────────────────────

    def _breezy(self, url: str) -> Optional[dict]:
        """
        Breezy detail pages: https://{sub}.breezy.hr/p/{slug}
        Server-rendered HTML.
        """
        m = re.search(r"([\w-]+)\.breezy\.hr/p/([^/?#]+)", url, re.I)
        if not m:
            return None
        tenant, position_slug = m.group(1), m.group(2)
        detail_url = f"https://{tenant}.breezy.hr/p/{position_slug}"
        try:
            resp = self._session.get(
                detail_url, timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title = ""
        el = soup.select_one("h1") or soup.select_one("h2")
        if el:
            title = el.get_text(" ", strip=True)

        description = ""
        for sel in (".description", "[class*='job-description']", "[class*='posting']", "article", "main"):
            el = soup.select_one(sel)
            if el and len(el.get_text(" ", strip=True)) >= 72:
                description = str(el)
                break

        location_raw = ""
        el = soup.select_one("[class*='location']")
        if el:
            location_raw = el.get_text(" ", strip=True)[:200]

        return {
            "title": title,
            "company_name": tenant.replace("-", " ").title(),
            "location_raw": location_raw,
            "description": description,
            "external_id": position_slug,
            "original_url": detail_url,
            "apply_url": detail_url,
            "raw_payload": {"source": "breezy_scrape"},
        }

    # ── Teamtailor ────────────────────────────────────────────────────────────

    def _teamtailor(self, url: str) -> Optional[dict]:
        """
        Teamtailor detail pages: https://{sub}.teamtailor.com/jobs/{id}-{slug}
        Server-rendered HTML with job description.
        """
        m = re.search(r"([\w-]+)\.teamtailor\.com/jobs/(\d+[^?#]*)", url, re.I)
        if not m:
            return None
        tenant = m.group(1)
        try:
            resp = self._session.get(
                url, timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title = ""
        el = soup.select_one("h1") or soup.select_one("[class*='title']")
        if el:
            title = el.get_text(" ", strip=True)

        description = ""
        for sel in (
            "[class*='job-description']", "[class*='jobDescription']",
            "[itemprop='description']", ".content", "article", "main",
        ):
            el = soup.select_one(sel)
            if el and len(el.get_text(" ", strip=True)) >= 72:
                description = str(el)
                break

        location_raw = ""
        el = soup.select_one("[class*='location']")
        if el:
            location_raw = el.get_text(" ", strip=True)[:200]

        return {
            "title": title,
            "company_name": tenant.replace("-", " ").title(),
            "location_raw": location_raw,
            "description": description,
            "original_url": url,
            "apply_url": url,
            "raw_payload": {"source": "teamtailor_scrape"},
        }

    # ── Zoho Recruit ──────────────────────────────────────────────────────────

    def _zoho(self, url: str) -> Optional[dict]:
        """
        Zoho job detail pages come in two shapes:
        - https://jobs.zoho.com/portal/{slug}/apply/{id}
        - https://{sub}.zohorecruit.com/jobs/Careers/{id}/{title}
        Fetch and scrape the server-rendered HTML.
        """
        try:
            resp = self._session.get(
                url, timeout=self.timeout,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title = ""
        el = soup.select_one("h1") or soup.select_one("[class*='title']")
        if el:
            title = el.get_text(" ", strip=True)

        description = ""
        for sel in (
            "[class*='job-description']", "[class*='jobDescription']",
            "[itemprop='description']", ".careers-jobdetail-desc",
            ".jobDetail", "article", "main",
        ):
            el = soup.select_one(sel)
            if el and len(el.get_text(" ", strip=True)) >= 72:
                description = str(el)
                break

        location_raw = ""
        el = soup.select_one("[class*='location']")
        if el:
            location_raw = el.get_text(" ", strip=True)[:200]

        return {
            "title": title,
            "description": description,
            "location_raw": location_raw,
            "original_url": url,
            "apply_url": url,
            "raw_payload": {"source": "zoho_scrape"},
        }


# ── JSON-LD extraction ────────────────────────────────────────────────────────

def _try_jsonld(html: str) -> Optional[dict]:
    """Find a @type:JobPosting block in the page's JSON-LD scripts."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        items: list[dict] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("@graph") or [raw]

        for item in items:
            if not isinstance(item, dict):
                continue
            type_val = item.get("@type", "")
            # @type can be a string or list
            types = type_val if isinstance(type_val, list) else [type_val]
            if any(t in ("JobPosting", "https://schema.org/JobPosting") for t in types):
                return _parse_jsonld(item)
    return None


def _parse_jsonld(d: dict) -> dict:
    title = d.get("title") or d.get("name") or ""

    # Company
    org = d.get("hiringOrganization") or {}
    company_name = org.get("name", "") if isinstance(org, dict) else str(org)

    # Location
    raw_loc = d.get("jobLocation") or {}
    if isinstance(raw_loc, list):
        raw_loc = raw_loc[0] if raw_loc else {}
    addr = raw_loc.get("address", {}) if isinstance(raw_loc, dict) else {}
    if isinstance(addr, str):
        location_raw = addr
        city = state = country = ""
    else:
        # addressLocality / addressRegion / addressCountry can each be a
        # plain string OR a schema.org object {"@type": "...", "name": "..."}
        city    = _str_val(addr.get("addressLocality", ""))
        state   = _str_val(addr.get("addressRegion", ""))
        country = _str_val(addr.get("addressCountry", ""))
        # Some publishers (e.g. Microsoft) put "WA,US" in addressRegion —
        # strip any trailing country code suffix after a comma
        if "," in state and not country:
            state, country = [p.strip() for p in state.split(",", 1)]
        elif "," in state:
            state = state.split(",")[0].strip()
        location_raw = ", ".join(p for p in [city, state, country] if p)

    # Remote
    loc_type_raw = str(d.get("jobLocationType", "") or "").lower()
    is_remote = (loc_type_raw == "telecommute") or ("remote" in location_raw.lower())
    location_type = _infer_location_type(location_raw, is_remote=is_remote)

    # Salary
    salary_raw = ""
    salary_min = salary_max = None
    salary_currency = "USD"
    salary_period = ""
    sal = d.get("baseSalary") or {}
    if isinstance(sal, dict):
        sv = sal.get("value") or {}
        if isinstance(sv, dict):
            salary_min    = _safe_float(sv.get("minValue"))
            salary_max    = _safe_float(sv.get("maxValue"))
            salary_period = str(sv.get("unitText", "")).upper()
        elif sv:
            salary_min = _safe_float(sv)
        salary_currency = sal.get("currency", "USD") or "USD"
        if salary_min and salary_max:
            salary_raw = f"{salary_currency} {salary_min:,.0f}–{salary_max:,.0f}/{salary_period or 'YEAR'}"
        elif salary_min:
            salary_raw = f"{salary_currency} {salary_min:,.0f}/{salary_period or 'YEAR'}"

    # Employment type
    emp_raw = d.get("employmentType") or ""
    if isinstance(emp_raw, list):
        emp_raw = emp_raw[0] if emp_raw else ""
    employment_type = _map_employment(str(emp_raw))

    # Department
    dept = d.get("occupationalCategory") or d.get("industry") or ""
    if isinstance(dept, list):
        dept = dept[0] if dept else ""

    # Identifier (external id)
    ident = d.get("identifier") or {}
    ext_id = ident.get("value", "") if isinstance(ident, dict) else str(ident)

    return {
        "title": title,
        "company_name": company_name,
        "location_raw": location_raw,
        "city": city,
        "state": state,
        "country": country,
        "is_remote": is_remote,
        "location_type": location_type,
        "employment_type": employment_type,
        "description": d.get("description", ""),
        "salary_raw": salary_raw,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "salary_period": salary_period,
        "posted_date_raw": d.get("datePosted", ""),
        "closing_date_raw": d.get("validThrough", ""),
        "department": str(dept),
        "external_id": str(ext_id),
        "raw_payload": d,
    }


# ── HTML scrape fallback ──────────────────────────────────────────────────────

def _try_html_scrape(html: str, page_url: str = "") -> Optional[dict]:
    """
    Best-effort extraction from any career page HTML.
    Works on iCIMS, Taleo, SuccessFactors, ADP, and custom career sites
    that embed job data in the DOM without JSON-LD.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Title ────────────────────────────────────────────────────────────────
    title = ""
    # Priority order: specific job-title elements → any prominent h1 → page <title>
    title_selectors = [
        # iCIMS
        "h1.iCIMS_JobTitle", "[class*='job-title']", "[id*='job-title']",
        # Taleo / Oracle
        "[class*='JobTitle']", "[id*='JobTitle']",
        # SuccessFactors
        "[class*='jobTitle']", "[id*='jobTitle']",
        # ADP
        "[data-automation='job-title']",
        # Generic
        "h1[itemprop='title']", "h1[class*='title']", ".posting-headline h2",
        ".job-header h1", "header h1", "h1",
    ]
    for sel in title_selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            t = el.get_text(" ", strip=True)
            if t and len(t) < 250:
                title = t
                break

    # <title> tag fallback — strip the company/site suffix
    if not title:
        tag = soup.find("title")
        if tag:
            raw = tag.get_text(" ", strip=True)
            parts = re.split(r"\s*[|\-–—]\s*", raw)
            title = parts[0].strip()
            # Discard if title looks like a generic page name
            if title.lower() in ("jobs", "careers", "job search", "open positions", ""):
                title = parts[-1].strip() if len(parts) > 1 else ""

    # og:title as last resort for title
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = og["content"].split("|")[0].split(" - ")[0].strip()

    # ── Company name ──────────────────────────────────────────────────────────
    company_name = ""
    # 1. og:site_name is the cleanest signal
    og_site = soup.find("meta", attrs={"property": "og:site_name"})
    if og_site and og_site.get("content"):
        company_name = og_site["content"].strip()

    # 2. Try <title> tag — last segment usually has company name
    if not company_name:
        tag = soup.find("title")
        if tag:
            raw = tag.get_text(" ", strip=True)
            parts = re.split(r"\s*[|\-–—]\s*", raw)
            if len(parts) >= 2:
                # Last part is often company or "Careers at Company"
                last = parts[-1].strip()
                last = re.sub(r"^(careers at |jobs at |careers - |jobs - )", "", last, flags=re.I).strip()
                if last and last.lower() not in ("careers", "jobs", "job search"):
                    company_name = last

    # 3. meta name="author" / application-name
    if not company_name:
        for attr_name in ("application-name", "author"):
            m = soup.find("meta", attrs={"name": attr_name})
            if m and m.get("content"):
                company_name = m["content"].strip()
                break

    # ── Location ──────────────────────────────────────────────────────────────
    location_raw = ""
    loc_selectors = [
        "[class*='location']", "[id*='location']",
        "[itemprop='jobLocation']", "[class*='Location']",
        "[data-automation='job-location']",
    ]
    for sel in loc_selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            t = el.get_text(" ", strip=True)
            if t and len(t) < 200:
                location_raw = t
                break

    # ── Description ───────────────────────────────────────────────────────────
    description = ""
    desc_selectors = [
        "[class*='job-description']", "[id*='job-description']",
        "[class*='jobDescription']", "[id*='jobDescription']",
        "[class*='job_description']",
        "[class*='description-content']",
        "[itemprop='description']",
        "[class*='job-detail']", "[id*='job-detail']",
        "[class*='requisition']",
        "[class*='posting-description']", "[id*='posting-description']",
        "[data-testid*='description']", "[data-automation*='description']",
        "article.job", ".job-content", "#job-content",
        "article", "main",
    ]
    _MIN_DESC_CHARS = 72
    for sel in desc_selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) >= _MIN_DESC_CHARS:
                description = str(el)
                break

    # Title is nice-to-have for display; description is what backfill cares about.
    if not title and not description:
        return None
    if not title:
        title = "Job posting"

    return {
        "title": title,
        "company_name": company_name,
        "location_raw": location_raw,
        "is_remote": "remote" in location_raw.lower(),
        "location_type": _infer_location_type(location_raw),
        "description": description,
        "raw_payload": {"scrape_url": page_url, "strategy": "html_scrape"},
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_job(url: str) -> dict[str, Any]:
    return {
        "original_url": url,
        "apply_url": url,
        "platform_slug": "",
        "strategy": "",
        "error": "",
        "title": "",
        "company_name": "",
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
        "salary_raw": "",
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "USD",
        "salary_period": "",
        "description": "",
        "requirements": "",
        "benefits": "",
        "posted_date_raw": "",
        "closing_date_raw": "",
        "external_id": "",
        "raw_payload": {},
    }


def _detect_platform(url: str) -> str:
    lower = url.lower()
    for slug, patterns in PLATFORM_PATTERNS.items():
        if any(p in lower for p in patterns):
            return slug
    return ""


def _infer_location_type(location: str, *, is_remote: bool = False) -> str:
    loc = location.lower()
    if is_remote or "remote" in loc:
        return "REMOTE"
    if "hybrid" in loc:
        return "HYBRID"
    if location:
        return "ONSITE"
    return "UNKNOWN"


def _map_employment(raw: str) -> str:
    r = raw.upper().replace("-", "_").replace(" ", "_")
    if "FULL" in r:
        return "FULL_TIME"
    if "PART" in r:
        return "PART_TIME"
    if any(k in r for k in ("CONTRACT", "CONTRACTOR", "FREELANCE")):
        return "CONTRACT"
    if "INTERN" in r:
        return "INTERNSHIP"
    if "TEMP" in r:
        return "TEMPORARY"
    return "UNKNOWN"


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _enrich_inferred(result: dict) -> None:
    """Fill in inferred fields that platform APIs may not supply."""
    # Inline experience-level detection (mirrors workday.py logic)
    if result.get("experience_level", "UNKNOWN") == "UNKNOWN":
        result["experience_level"] = _detect_experience_level(
            result.get("title", ""),
            result.get("description", ""),
        )
    # Normalise is_remote + location_type consistency
    if result.get("is_remote") and result.get("location_type") == "UNKNOWN":
        result["location_type"] = "REMOTE"


def _detect_experience_level(title: str, description: str) -> str:
    """Infer seniority from title/description keywords."""
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
    if any(k in combined for k in ("junior", "jr.", "jr ", "entry", "associate", "level 1")):
        return "ENTRY"
    return "MID"

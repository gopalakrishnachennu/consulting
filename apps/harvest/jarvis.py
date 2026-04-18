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
    "icims":           ["careers.icims.com", "icims.com/jobs"],
    "jobvite":         ["jobs.jobvite.com", "jobvite.com/company"],
    "taleo":           ["taleo.net/careersection"],
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
        if platform_slug:
            try:
                api_data = self._platform_api(url, platform_slug)
                if api_data:
                    result.update(api_data)
                    result["strategy"] = f"api:{platform_slug}"
                    _enrich_inferred(result)
                    return result
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
        jsonld = _try_jsonld(html)
        if jsonld:
            result.update(jsonld)
            result["strategy"] = "jsonld"
            _enrich_inferred(result)
            return result

        # ── Strategy 3: HTML scrape fallback ──────────────────────────────────
        scraped = _try_html_scrape(html, final_url)
        if scraped and scraped.get("title"):
            result.update(scraped)
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
        }
        fn = dispatch.get(slug)
        return fn(url) if fn else None

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
        api_url = f"https://apply.workable.com/api/v1/widget/accounts/{company_slug}/jobs/{job_shortcode}"
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
        city    = addr.get("addressLocality", "")
        state   = addr.get("addressRegion", "")
        country = addr.get("addressCountry", "")
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
    soup = BeautifulSoup(html, "html.parser")

    # Title
    title = ""
    for sel in [
        "h1.job-title", "h1[class*='title']", ".posting-headline h2",
        "h1[itemprop='title']", ".job-header h1", "h1",
    ]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                title = t
                break

    if not title:
        tag = soup.find("title")
        if tag:
            raw = tag.get_text(" ", strip=True)
            # Strip common suffixes: " | Company" / " - Careers"
            parts = re.split(r"\s*[|\-–—]\s*", raw)
            title = parts[0].strip()

    # Company name — open graph site_name is usually cleanest
    company_name = ""
    for prop in ("og:site_name", "og:title"):
        meta = soup.find("meta", attrs={"property": prop})
        if meta and meta.get("content"):
            company_name = meta["content"]
            if prop == "og:title":
                # "Job at Company" → keep only company part
                parts = re.split(r"\s*(at|@)\s*", company_name, maxsplit=1)
                company_name = parts[-1].strip() if len(parts) > 1 else ""
            if company_name:
                break

    # Description — look for large text block
    description = ""
    for sel in [
        "[class*='job-description']", "[id*='job-description']",
        "[class*='description']", "[id*='description']",
        "[class*='job-detail']", ".content", "article", "main",
    ]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) > 200:
                description = str(el)
                break

    if not title:
        return None

    return {
        "title": title,
        "company_name": company_name,
        "description": description,
        "raw_payload": {"scrape_url": page_url},
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

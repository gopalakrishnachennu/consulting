from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


_WS_RE = re.compile(r"\s+")

# Pages that look dead but are actually bot-blocks or login walls —
# treat these as INCONCLUSIVE (live-assumed) to avoid false positives.
_BOT_BLOCK_MARKERS = (
    "challenge.cloudflare.com",
    "cf-please-wait",
    "enable javascript and cookies",
    "checking your browser",
    "ddos protection by cloudflare",
    "access denied",
    "this site is protected by recaptcha",
    "unusual traffic",
    "please verify you are a human",
    "security check to access",
    "one more step",
    "prove you are not a robot",
)
_LOGIN_WALL_MARKERS = (
    "sign in to view",
    "log in to view",
    "please log in",
    "please sign in",
    "login required",
    "you must be logged in",
    "create an account to view",
    "register to view",
    "sign up to apply",
)

# Generic signals for job pages that render an error page with HTTP 200.
_DEAD_MARKERS_GENERIC = (
    "page you are looking for doesnt exist",
    "page you are looking for does not exist",
    "job not found",
    "this job is no longer available",
    "position is no longer available",
    "posting is no longer available",
    "we couldnt find the job",
    "we couldn't find the job",
    "requisition is no longer available",
    "position has been filled",
    "position is filled",
    "no longer accepting applications",
    "job posting has expired",
    "this posting has expired",
    "position has been closed",
    "this position has been closed",
    "this job has been closed",
    "this requisition is no longer active",
    "requisition has been removed",
    "opportunity has expired",
)

_DEAD_MARKERS_BY_PLATFORM = {
    "workday": (
        "the page you are looking for doesnt exist",
        "the page you are looking for does not exist",
        "the job you are trying to view is no longer available",
        "we are unable to find the job you are looking for",
    ),
    "icims": (
        "job description no longer available",
        "this opportunity is no longer available",
        "this job is no longer posted",
        "this position is no longer posted",
    ),
    "jobvite": (
        "job is no longer available",
        "the job you are looking for is no longer available",
    ),
    "taleo": (
        "this requisition is no longer available",
        "job opening is no longer available",
    ),
    "dayforce": (
        "job posting is no longer available",
        "this job opportunity is no longer available",
    ),
    "greenhouse": (
        "this job has been filled",
        "this role is no longer open",
    ),
    "lever": (
        "this posting is no longer available",
        "the position has been filled",
    ),
    "workable": (
        "this job has been archived",
        "the job you tried to access is not available",
    ),
    "recruitee": (
        "this offer is no longer available",
        "we could not find this offer",
    ),
    "teamtailor": (
        "this job is no longer accepting applications",
    ),
    "zoho": (
        "job opening is no longer available",
        "this posting is no longer accepting applications",
    ),
    "ultipro": (
        "this opportunity is no longer available",
        "job opportunity not found",
    ),
}

_LIVE_MARKERS_GENERIC = (
    "job description",
    "responsibilities",
    "qualifications",
    "requirements",
    "about the role",
    "about this role",
    "what you'll do",
    "what you will do",
    "apply now",
    "apply for this",
)

_LIVE_MARKERS_BY_PLATFORM = {
    "workday": (
        "job profile summary",
        "posted",
        "locations",
        "apply now",
    ),
    "icims": (
        "apply for this job",
        "job summary",
        "job description",
    ),
    "jobvite": (
        "share this job",
        "job description",
    ),
    "taleo": (
        "job field",
        "all jobs",
    ),
    "workable": (
        "about workable",
        "requirements",
    ),
    "recruitee": (
        "apply to this job",
        "department",
    ),
    "dayforce": (
        "job location",
        "posted date",
    ),
    "breezy": (
        "apply for this position",
    ),
    "teamtailor": (
        "connect",
        "departments",
    ),
    "zoho": (
        "job description",
        "job opening",
    ),
    "ultipro": (
        "opportunitydetail",
        "briefdescription",
    ),
}

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; Harvest-LinkHealth/1.0; +https://chennu.co)"
    )
}


@dataclass(frozen=True)
class LinkHealthResult:
    is_live: bool
    status_code: int
    reason: str
    final_url: str


_CONFIRMED_LIVE_REASONS = {
    "workday_cxs_live",
    "workday_search_match",
    "oracle_hcm_live",
    "greenhouse_api_live",
    "lever_api_live",
    "ashby_api_live",
    "smartrecruiters_api_live",
    "bamboohr_api_live",
    "workable_api_live",
    "recruitee_api_live",
    "dayforce_api_live",
    "icims_detail_live",
    "jobvite_detail_live",
    "taleo_detail_live",
    "breezy_detail_live",
    "teamtailor_detail_live",
    "zoho_detail_live",
    "ultipro_detail_live",
    "detail_live_markers",
    "detail_long_content",
}

_INCONCLUSIVE_LIVE_REASONS = {
    "ok",
    "bot_block_assumed_live",
    "login_wall_assumed_live",
    "head_ok_get_failed",
    "request_error_unknown",
    "workday_cxs_http_error",
    "workday_cxs_non_json",
    "workday_cxs_empty",
    "workday_cxs_error",
}


def _norm_text(raw: str) -> str:
    txt = html.unescape(raw or "").lower()
    txt = txt.replace("’", "'")
    txt = txt.replace("doesn't", "doesnt")
    txt = _WS_RE.sub(" ", txt).strip()
    return txt


def _contains_dead_marker(text: str, platform_slug: str) -> bool:
    if not text:
        return False
    markers = list(_DEAD_MARKERS_GENERIC)
    markers.extend(_DEAD_MARKERS_BY_PLATFORM.get((platform_slug or "").lower(), ()))
    return any(m in text for m in markers)


def _contains_live_marker(text: str, platform_slug: str) -> bool:
    if not text:
        return False
    markers = list(_LIVE_MARKERS_GENERIC)
    markers.extend(_LIVE_MARKERS_BY_PLATFORM.get((platform_slug or "").lower(), ()))
    return any(m in text for m in markers)


def _looks_like_detail_path(path: str, platform_slug: str) -> bool:
    p = (path or "").lower()
    if not p:
        return False
    slug = (platform_slug or "").lower()
    if slug == "workday":
        return "/job/" in p or "/details/" in p
    if slug == "icims":
        return "/jobs/" in p and "/search" not in p
    if slug == "jobvite":
        return "/job/" in p
    if slug == "taleo":
        return "jobdetail.ftl" in p
    if slug == "workable":
        return "/j/" in p
    if slug == "recruitee":
        return "/o/" in p
    if slug == "dayforce":
        return "/jobs/" in p
    if slug == "breezy":
        return "/p/" in p
    if slug == "teamtailor":
        return "/jobs/" in p
    return any(seg in p for seg in ("/job/", "/jobs/", "/details/", "/positions/"))


def link_health_state(result: LinkHealthResult) -> str:
    if is_definitive_inactive(result):
        return "DEAD"
    reason = (result.reason or "").strip().lower()
    if reason in _CONFIRMED_LIVE_REASONS:
        return "LIVE"
    if reason in _INCONCLUSIVE_LIVE_REASONS or reason.startswith("transient_http_"):
        return "INCONCLUSIVE"
    if result.is_live:
        return "INCONCLUSIVE"
    return "INCONCLUSIVE"


def build_link_health_payload(result: LinkHealthResult, *, checked_at_iso: str) -> dict:
    state = link_health_state(result)
    return {
        "state": state,
        "is_live": bool(result.is_live),
        "reason": (result.reason or "")[:120],
        "status_code": int(result.status_code or 0),
        "checked_at": checked_at_iso,
        "final_url": result.final_url or "",
        "decisive": state == "DEAD",
    }


def is_definitive_inactive(result: LinkHealthResult) -> bool:
    """
    Decision policy for flipping DB rows inactive.

    Fires on:
    - Hard HTTP errors: 404/410/451
    - Soft-404 markers detected in HTML
    - Redirect-to-search with no live signals
    - Platform API: job not found in API response (Workday, Oracle, Greenhouse,
      Lever, Ashby, SmartRecruiters, BambooHR, iCIMS)
    """
    if result.is_live:
        return False
    reason = (result.reason or "").lower()
    code = int(result.status_code or 0)
    if reason.startswith("http_"):
        return code in {404, 410, 451}
    if reason in {
        "soft_404_marker",
        "redirected_to_search_soft404",
        "redirected_to_non_detail_no_live_signals",
        # Workday
        "workday_cxs_not_found",
        "workday_search_no_match",
        # Oracle HCM
        "oracle_hcm_not_found",
        "oracle_hcm_no_results",
        # Greenhouse
        "greenhouse_api_not_found",
        # Lever
        "lever_api_not_found",
        # Ashby
        "ashby_api_not_found",
        # SmartRecruiters
        "smartrecruiters_api_not_found",
        # BambooHR
        "bamboohr_api_not_found",
        # Workable
        "workable_api_not_found",
        # Recruitee
        "recruitee_api_not_found",
        # Dayforce
        "dayforce_api_not_found",
        # iCIMS
        "icims_api_not_found",
    }:
        return True
    return False


def _workday_cxs_liveness(url: str) -> LinkHealthResult | None:
    """
    Ask Workday CXS JSON endpoint directly for this detail URL.
    Returns:
      - LinkHealthResult(..., is_live=True/False, reason=workday_cxs_*)
      - None when URL shape is not Workday-detail compatible.
    """
    m = re.match(
        r"https?://([\w-]+(?:\.wd\d+)?)\.myworkdayjobs\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([^/?#]+)(/(?:details|job)/[^?#]+)",
        url,
        re.I,
    )
    if not m:
        return None

    full_subdomain = m.group(1)
    jobboard = m.group(2)
    ext_path = m.group(3).split("?")[0]
    tenant = re.sub(r"\.wd\d+$", "", full_subdomain, flags=re.I)
    cxs_url = f"https://{full_subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{jobboard}{ext_path}"

    # common req id patterns at the end of slug, e.g. _JR-023060 or _R2115899
    req_id = ""
    m_req = re.search(r"_([A-Za-z]+-?\d{3,})$", ext_path)
    if m_req:
        req_id = m_req.group(1)

    try:
        resp = requests.get(
            cxs_url,
            headers={"Accept": "application/json", **_UA},
            timeout=10,
        )
        status = int(resp.status_code or 0)
        if status >= 400:
            # Some tenants block detail CXS for bots (403). Fallback to searchable CXS jobs endpoint.
            if status in {401, 403}:
                search_url = f"https://{full_subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{jobboard}/jobs"
                if req_id:
                    try:
                        q = requests.post(
                            search_url,
                            json={"limit": 20, "offset": 0, "searchText": req_id, "appliedFacets": {}},
                            headers={"Accept": "application/json", **_UA},
                            timeout=10,
                        )
                        q_status = int(q.status_code or 0)
                        if q_status < 400:
                            data_q = q.json() if q.content else {}
                            total = int((data_q or {}).get("total") or 0)
                            if total > 0:
                                return LinkHealthResult(True, q_status, "workday_search_match", search_url)
                            return LinkHealthResult(False, q_status, "workday_search_no_match", search_url)
                    except Exception:
                        pass
            return LinkHealthResult(False, status, "workday_cxs_http_error", cxs_url)
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            return LinkHealthResult(False, status, "workday_cxs_non_json", cxs_url)

        info = data.get("jobPostingInfo") or data
        # canonical live signals
        for key in ("title", "jobDescription", "jobPostingDescription", "externalJobDescription", "bulletFields"):
            val = info.get(key)
            if isinstance(val, (str, list, dict)) and str(val).strip():
                return LinkHealthResult(True, status, "workday_cxs_live", cxs_url)

        raw_text = _norm_text(str(data))
        if any(k in raw_text for k in ("not found", "doesnt exist", "does not exist", "no longer available")):
            return LinkHealthResult(False, status, "workday_cxs_not_found", cxs_url)
        return LinkHealthResult(False, status, "workday_cxs_empty", cxs_url)
    except Exception:
        return LinkHealthResult(False, 0, "workday_cxs_error", cxs_url)


def _oracle_hcm_liveness(url: str) -> "LinkHealthResult | None":
    """
    Oracle HCM CX pages are SPAs — the 'This job is no longer available' message
    is rendered by JavaScript and invisible to a plain GET request.

    Instead, query the Oracle HCM REST API directly for the requisition.
    Returns None if URL doesn't match Oracle HCM pattern.
    """
    m = re.search(
        r"([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/(?:requisitions?(?:/preview)?|jobs?)/(\d+)",
        url,
        re.I,
    )
    if not m:
        return None

    host, sites_id, req_num = m.group(1), m.group(2), m.group(3)
    # Query Oracle HCM detail endpoint by requisition ID directly.
    # The detail endpoint is more reliable than the list endpoint for individual lookups.
    detail_url = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f"?onlyData=true&expand=all"
        f"&finder=ById;Id={req_num},siteNumber={sites_id}"
    )

    try:
        resp = requests.get(
            detail_url,
            headers={"Accept": "application/json", **_UA},
            timeout=15,
        )
        status = int(resp.status_code or 0)
        if status == 404:
            return LinkHealthResult(False, status, "oracle_hcm_not_found", detail_url)
        if status >= 400:
            # API not accessible — fall through to HTML check
            return None
        data = resp.json() if resp.content else {}
        items = data.get("items") or []
        if items and isinstance(items[0], dict):
            # Check if the posting is still active (ExternalPostedEndDate not set, or in future)
            return LinkHealthResult(True, status, "oracle_hcm_live", detail_url)
        # Empty items = requisition not found or not publicly visible = closed
        return LinkHealthResult(False, status, "oracle_hcm_not_found", detail_url)
    except Exception:
        return None


def _greenhouse_liveness(url: str) -> "LinkHealthResult | None":
    """Greenhouse boards-api: 404 = definitively closed."""
    m = re.search(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", url, re.I)
    if not m:
        return None
    board_token, job_id = m.group(1), m.group(2)
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}"
    try:
        resp = requests.get(api_url, headers={"Accept": "application/json", **_UA}, timeout=10)
        status = int(resp.status_code or 0)
        if status == 404:
            return LinkHealthResult(False, status, "greenhouse_api_not_found", api_url)
        if status >= 400:
            return None  # inconclusive (rate limit, server error)
        d = resp.json() if resp.content else {}
        return LinkHealthResult(True, status, "greenhouse_api_live", api_url) if d.get("id") else None
    except Exception:
        return None


def _lever_liveness(url: str) -> "LinkHealthResult | None":
    """Lever public postings API: 404 = closed."""
    m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", url, re.I)
    if not m:
        return None
    company, posting_id = m.group(1), m.group(2)
    api_url = f"https://api.lever.co/v0/postings/{company}/{posting_id}"
    try:
        resp = requests.get(api_url, headers={"Accept": "application/json", **_UA}, timeout=10)
        status = int(resp.status_code or 0)
        if status == 404:
            return LinkHealthResult(False, status, "lever_api_not_found", api_url)
        if status >= 400:
            return None
        d = resp.json() if resp.content else {}
        return LinkHealthResult(True, status, "lever_api_live", api_url) if d.get("id") else None
    except Exception:
        return None


def _ashby_liveness(url: str) -> "LinkHealthResult | None":
    """Ashby board API: job missing from board = closed."""
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url, re.I)
    if not m:
        return None
    company, job_id = m.group(1), m.group(2)
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
    try:
        resp = requests.get(api_url, headers={"Accept": "application/json", **_UA}, timeout=12)
        status = int(resp.status_code or 0)
        if status >= 400:
            return None
        jobs = (resp.json() if resp.content else {}).get("jobs") or []
        match = next((j for j in jobs if (j.get("id") or "").lower() == job_id.lower()), None)
        if match:
            return LinkHealthResult(True, status, "ashby_api_live", api_url)
        if jobs:
            # Board returned results but this job wasn't in it → gone
            return LinkHealthResult(False, status, "ashby_api_not_found", api_url)
        return None  # empty board = inconclusive
    except Exception:
        return None


def _smartrecruiters_liveness(url: str) -> "LinkHealthResult | None":
    """SmartRecruiters public API: 404 = closed."""
    m = re.search(r"(?:jobs\.smartrecruiters\.com|smartrecruiters\.com)/([^/]+)/(\d+)", url, re.I)
    if not m:
        # Handle legacy records where original_url was accidentally set to the API URL
        # e.g. api.smartrecruiters.com/v1/companies/{company}/postings/{id}
        m = re.search(r"api\.smartrecruiters\.com/v1/companies/([^/]+)/postings/(\d+)", url, re.I)
        if not m:
            return None
    company, job_id = m.group(1), m.group(2)
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}"
    try:
        resp = requests.get(api_url, headers={"Accept": "application/json", **_UA}, timeout=12)
        status = int(resp.status_code or 0)
        if status == 404:
            return LinkHealthResult(False, status, "smartrecruiters_api_not_found", api_url)
        if status >= 400:
            return None
        d = resp.json() if resp.content else {}
        return LinkHealthResult(True, status, "smartrecruiters_api_live", api_url) if d.get("id") else None
    except Exception:
        return None


def _bamboohr_liveness(url: str) -> "LinkHealthResult | None":
    """BambooHR careers page: 404 on detail URL = closed."""
    m = re.search(r"([\w-]+)\.bamboohr\.com/(?:careers|jobs)/(\d+)", url, re.I)
    if not m:
        return None
    subdomain, job_id = m.group(1), m.group(2)
    api_url = f"https://{subdomain}.bamboohr.com/careers/json/jobs/{job_id}"
    try:
        resp = requests.get(api_url, headers={"Accept": "application/json", **_UA}, timeout=10)
        status = int(resp.status_code or 0)
        if status == 404:
            return LinkHealthResult(False, status, "bamboohr_api_not_found", api_url)
        if status >= 400:
            return None
        d = resp.json() if resp.content else {}
        return LinkHealthResult(True, status, "bamboohr_api_live", api_url) if d.get("id") or d.get("title") else None
    except Exception:
        return None


def _fetch_html_detail_liveness(
    url: str,
    *,
    platform_slug: str,
    title_selectors: tuple[str, ...],
    body_selectors: tuple[str, ...],
) -> "LinkHealthResult | None":
    try:
        resp = requests.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml", **_UA},
            timeout=12,
            allow_redirects=True,
        )
        status = int(resp.status_code or 0)
        final_url = str(getattr(resp, "url", "") or url)
        if status in {404, 410, 451}:
            return LinkHealthResult(False, status, f"http_{status}", final_url)
        if status >= 400:
            return None

        text = _norm_text(resp.text or "")
        if any(m in text for m in _BOT_BLOCK_MARKERS):
            return LinkHealthResult(True, status, "bot_block_assumed_live", final_url)
        if any(m in text for m in _LOGIN_WALL_MARKERS):
            return LinkHealthResult(True, status, "login_wall_assumed_live", final_url)
        if _contains_dead_marker(text, platform_slug):
            return LinkHealthResult(False, status, "soft_404_marker", final_url)

        soup = BeautifulSoup(resp.text or "", "html.parser")
        title_text = ""
        for selector in title_selectors:
            el = soup.select_one(selector)
            if el:
                title_text = el.get_text(" ", strip=True)
                if title_text:
                    break

        body_text = ""
        for selector in body_selectors:
            el = soup.select_one(selector)
            if el:
                body_text = el.get_text(" ", strip=True)
                if len(body_text) >= 120:
                    break
                body_text = ""

        if title_text and (body_text or _contains_live_marker(text, platform_slug)):
            return LinkHealthResult(True, status, f"{platform_slug}_detail_live", final_url)
        if body_text:
            return LinkHealthResult(True, status, f"{platform_slug}_detail_live", final_url)
        return None
    except Exception:
        return None


def _workable_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(r"(?:apply|jobs)\.workable\.com/([^/?#]+)/j/([^/?#]+)", url, re.I)
    if not m:
        return None
    company_slug, job_shortcode = m.group(1), m.group(2)
    api_url = f"https://apply.workable.com/api/v1/accounts/{company_slug}/jobs/{job_shortcode}"
    try:
        resp = requests.get(api_url, headers={"Accept": "application/json", **_UA}, timeout=12)
        status = int(resp.status_code or 0)
        if status == 404:
            return LinkHealthResult(False, status, "workable_api_not_found", api_url)
        if status >= 400:
            return None
        data = resp.json() if resp.content else {}
        if any((data or {}).get(k) for k in ("title", "description", "requirements", "benefits")):
            return LinkHealthResult(True, status, "workable_api_live", api_url)
        return None
    except Exception:
        return None


def _recruitee_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(r"https?://([\w-]+)\.recruitee\.com/o/([^/?#]+)", url, re.I)
    if not m:
        return None
    tenant, opening_slug = m.group(1), m.group(2)
    api_url = f"https://{tenant}.recruitee.com/api/offers/"
    try:
        resp = requests.get(api_url, headers={"Accept": "application/json", **_UA}, timeout=12)
        status = int(resp.status_code or 0)
        if status >= 400:
            return None
        data = resp.json() if resp.content else {}
        offers = (data or {}).get("offers") or []
        offer = next(
            (
                o for o in offers
                if (o.get("slug") or "") == opening_slug
                or str(o.get("id") or "") == opening_slug
                or opening_slug in (o.get("careers_url") or "")
            ),
            None,
        )
        if offer:
            return LinkHealthResult(True, status, "recruitee_api_live", api_url)
        if offers:
            return LinkHealthResult(False, status, "recruitee_api_not_found", api_url)
        return None
    except Exception:
        return None


def _icims_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(r"([\w-]+)\.icims\.com/jobs/\d+", url, re.I)
    if not m:
        return None
    return _fetch_html_detail_liveness(
        url,
        platform_slug="icims",
        title_selectors=("h1.iCIMS_JobTitle", "h1"),
        body_selectors=(
            ".iCIMS_JobContent",
            ".iCIMS_InfoMsg_Job",
            "[class*='job-description']",
            "[class*='jobDescription']",
            "[itemprop='description']",
            "article",
            "main",
        ),
    )


def _jobvite_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(r"jobs\.jobvite\.com/([^/?#]+)/job/([^/?#]+)", url, re.I)
    if not m:
        return None
    detail_url = f"https://jobs.jobvite.com/{m.group(1)}/job/{m.group(2)}"
    return _fetch_html_detail_liveness(
        detail_url,
        platform_slug="jobvite",
        title_selectors=("h2.jv-header", "h1", "h2"),
        body_selectors=(
            ".jv-job-detail-description",
            ".jv-job-detail",
            "[class*='job-description']",
            "article",
            "main",
        ),
    )


def _taleo_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(
        r"([\w-]+)\.taleo\.net/careersection/([^/]+)/jobdetail\.ftl\?.*?job=([^&]+)",
        url,
        re.I,
    )
    if not m:
        return None
    detail_url = (
        f"https://{m.group(1)}.taleo.net/careersection/{m.group(2)}"
        f"/jobdetail.ftl?job={m.group(3)}&lang=en"
    )
    return _fetch_html_detail_liveness(
        detail_url,
        platform_slug="taleo",
        title_selectors=(
            "#requisitionDescriptionInterface\\.reqTitleLinkAction\\.row1",
            "h1",
            ".pageTitle",
        ),
        body_selectors=(
            "#requisitionDescriptionInterface\\.ID1702\\.row1",
            "[id*='requisitionDescription']",
            ".contentlinepanel",
            "[class*='job-description']",
            "article",
            "main",
        ),
    )


def _dayforce_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(
        r"jobs\.dayforcehcm\.com/(?:([a-z]{2}-[A-Z]{2})/)?([^/]+)/([^/]+)/jobs/([^/?#]+)",
        url,
        re.I,
    )
    if not m:
        return None
    slug, job_id = m.group(2), m.group(4)
    api_url = f"https://jobs.dayforcehcm.com/api/geo/{slug}/jobposting/{job_id}"
    try:
        resp = requests.get(
            api_url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": url,
                **_UA,
            },
            timeout=12,
        )
        status = int(resp.status_code or 0)
        if status == 404:
            return LinkHealthResult(False, status, "dayforce_api_not_found", api_url)
        if status >= 400:
            return None
        data = resp.json() if resp.content else {}
        payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        if isinstance(payload, dict) and any(payload.get(k) for k in ("JobTitle", "title", "Description", "description")):
            return LinkHealthResult(True, status, "dayforce_api_live", api_url)
        return None
    except Exception:
        return None


def _breezy_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(r"([\w-]+)\.breezy\.hr/p/([^/?#]+)", url, re.I)
    if not m:
        return None
    detail_url = f"https://{m.group(1)}.breezy.hr/p/{m.group(2)}"
    return _fetch_html_detail_liveness(
        detail_url,
        platform_slug="breezy",
        title_selectors=("h1", "h2"),
        body_selectors=(
            ".description",
            "[class*='job-description']",
            "[class*='posting']",
            "article",
            "main",
        ),
    )


def _teamtailor_liveness(url: str) -> "LinkHealthResult | None":
    m = re.search(r"([\w-]+)\.teamtailor\.com/jobs/(\d+[^?#]*)", url, re.I)
    if not m:
        return None
    return _fetch_html_detail_liveness(
        url,
        platform_slug="teamtailor",
        title_selectors=("h1", "[class*='title']"),
        body_selectors=(
            "[class*='job-description']",
            "[class*='jobDescription']",
            "[itemprop='description']",
            ".content",
            "article",
            "main",
        ),
    )


def _zoho_liveness(url: str) -> "LinkHealthResult | None":
    if not re.search(r"(jobs\.zoho\.com/portal/|zohorecruit\.com/jobs/careers/)", url, re.I):
        return None
    return _fetch_html_detail_liveness(
        url,
        platform_slug="zoho",
        title_selectors=("h1", "[class*='title']"),
        body_selectors=(
            "[class*='job-description']",
            "[class*='jobDescription']",
            "[itemprop='description']",
            ".careers-jobdetail-desc",
            ".jobDetail",
            "article",
            "main",
        ),
    )


def _ultipro_liveness(url: str) -> "LinkHealthResult | None":
    if not re.search(
        r"(?:recruiting\.ultipro\.com|recruiting\.ukg\.net)/[^/]+/JobBoard/[^/]+/OpportunityDetail\?opportunityId=",
        url,
        re.I,
    ):
        return None
    try:
        resp = requests.get(
            url,
            headers={"Accept": "text/html,application/xhtml+xml", **_UA},
            timeout=12,
            allow_redirects=True,
        )
        status = int(resp.status_code or 0)
        final_url = str(getattr(resp, "url", "") or url)
        if status in {404, 410, 451}:
            return LinkHealthResult(False, status, f"http_{status}", final_url)
        if status >= 400:
            return None
        text = _norm_text(resp.text or "")
        if any(m in text for m in _BOT_BLOCK_MARKERS):
            return LinkHealthResult(True, status, "bot_block_assumed_live", final_url)
        if any(m in text for m in _LOGIN_WALL_MARKERS):
            return LinkHealthResult(True, status, "login_wall_assumed_live", final_url)
        if _contains_dead_marker(text, "ultipro"):
            return LinkHealthResult(False, status, "soft_404_marker", final_url)
        if "candidateopportunitydetail(" in text or "briefdescription" in text or "opportunitydetail" in text:
            return LinkHealthResult(True, status, "ultipro_detail_live", final_url)
        return None
    except Exception:
        return None


# Map URL hostname/pattern → liveness function for quick lookup
_PLATFORM_LIVENESS_REGISTRY: list[tuple[str, object]] = [
    ("myworkdayjobs.com",     _workday_cxs_liveness),
    ("oraclecloud.com",       _oracle_hcm_liveness),
    ("greenhouse.io",         _greenhouse_liveness),
    ("lever.co",              _lever_liveness),
    ("ashbyhq.com",           _ashby_liveness),
    ("smartrecruiters.com",   _smartrecruiters_liveness),
    ("bamboohr.com",          _bamboohr_liveness),
    ("workable.com",          _workable_liveness),
    ("recruitee.com",         _recruitee_liveness),
    ("icims.com",             _icims_liveness),
    ("jobvite.com",           _jobvite_liveness),
    ("taleo.net",             _taleo_liveness),
    ("dayforcehcm.com",       _dayforce_liveness),
    ("breezy.hr",             _breezy_liveness),
    ("teamtailor.com",        _teamtailor_liveness),
    ("jobs.zoho.com",         _zoho_liveness),
    ("zohorecruit.com",       _zoho_liveness),
    ("recruiting.ultipro.com", _ultipro_liveness),
    ("recruiting.ukg.net",    _ultipro_liveness),
]

_INCONCLUSIVE_API_REASONS = {
    "workday_cxs_error",
    "workday_cxs_http_error",
    "workday_cxs_non_json",
    "workday_cxs_empty",
}


def check_job_posting_live(
    url: str,
    *,
    platform_slug: str = "",
    timeout_head: int = 10,
    timeout_get: int = 12,
    max_read_bytes: int = 32768,
) -> LinkHealthResult:
    url = (url or "").strip()
    if not url:
        return LinkHealthResult(False, 0, "missing_url", "")
    if not urlparse(url).scheme:
        url = "https://" + url

    # Platform-specific API checks first — these are definitive and avoid false positives
    # from SPA soft-404 pages that serve HTTP 200 with a "job gone" message in JS.
    url_lower = url.lower()
    for hostname_fragment, liveness_fn in _PLATFORM_LIVENESS_REGISTRY:
        if hostname_fragment in url_lower:
            result = liveness_fn(url)
            if result is not None:
                # Some API probes can fail for tenant/firewall reasons while the
                # actual detail page is still available. Fall back to HTML checks
                # for these inconclusive states instead of short-circuiting.
                if (result.reason or "") in _INCONCLUSIVE_API_REASONS:
                    break
                return result
            break  # matched platform but API was inconclusive — fall through to HTML check

    # HEAD first: fast path
    try:
        r_head = requests.head(
            url,
            timeout=timeout_head,
            allow_redirects=True,
            headers=_UA,
        )
        status = int(r_head.status_code or 0)
        final_url = str(getattr(r_head, "url", "") or url)
    except Exception:
        r_head = None
        status = 0
        final_url = url

    # Hard failures
    if status in {404, 410, 451}:
        return LinkHealthResult(False, status, f"http_{status}", final_url)
    # Rate-limited / temporary server failures are inconclusive. Do not hard-fail yet.

    # GET + body sniff for soft-404 detection (needed for Workday/iCIMS, etc.)
    try:
        r_get = requests.get(
            url,
            timeout=timeout_get,
            allow_redirects=True,
            headers=_UA,
            stream=True,
        )
        status_get = int(r_get.status_code or 0)
        final_url = str(getattr(r_get, "url", "") or final_url or url)
        if status_get in {404, 410, 451}:
            r_get.close()
            return LinkHealthResult(False, status_get, f"http_{status_get}", final_url)
        if status_get in {401, 403, 429, 500, 502, 503, 504}:
            # Auth walls, bot-blocks, throttling, upstream issues: treat as unknown-live
            # to prevent accidental mass deactivation of valid postings.
            r_get.close()
            return LinkHealthResult(True, status_get, f"transient_http_{status_get}", final_url)

        body_bytes = r_get.raw.read(max_read_bytes, decode_content=True) or b""
        r_get.close()
        text = _norm_text(body_bytes.decode("utf-8", errors="ignore"))

        # If the resulting URL already points to search/home routes, it's likely no longer a detail posting.
        path_l = urlparse(final_url).path.lower()
        detail_path = _looks_like_detail_path(path_l, platform_slug)
        dead_marker = _contains_dead_marker(text, platform_slug)
        live_marker = _contains_live_marker(text, platform_slug)

        # Bot-block / login-wall: treat as live-assumed (inconclusive) to avoid
        # false positives where a valid job is unreachable only to the crawler.
        if any(m in text for m in _BOT_BLOCK_MARKERS):
            return LinkHealthResult(True, status_get, "bot_block_assumed_live", final_url)
        if any(m in text for m in _LOGIN_WALL_MARKERS):
            return LinkHealthResult(True, status_get, "login_wall_assumed_live", final_url)

        if any(seg in path_l for seg in ("/jobs/search", "/search", "/job-search")) and not any(
            seg in path_l for seg in ("/job/", "/details/")
        ):
            if dead_marker:
                return LinkHealthResult(False, status_get, "redirected_to_search_soft404", final_url)
            if not live_marker:
                return LinkHealthResult(False, status_get, "redirected_to_non_detail_no_live_signals", final_url)

        if dead_marker:
            return LinkHealthResult(False, status_get, "soft_404_marker", final_url)

        if detail_path and live_marker:
            return LinkHealthResult(True, status_get, "detail_live_markers", final_url)

        if detail_path and len(text) > 800:
            return LinkHealthResult(True, status_get, "detail_long_content", final_url)

        return LinkHealthResult(True, status_get, "ok", final_url)
    except Exception:
        # If GET fails after a successful HEAD<400, keep live as unknown to reduce false negatives.
        if 0 < status < 400:
            return LinkHealthResult(True, status, "head_ok_get_failed", final_url)
        # Network errors with no hard signal are treated as unknown-live.
        return LinkHealthResult(True, status or 0, "request_error_unknown", final_url)

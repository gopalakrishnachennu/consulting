"""
TaleoHarvester — Python port of OpenPostings Taleo scraper.

Taleo has TWO data paths (tries REST first, falls back to AJAX):

Path 1 — REST API (preferred):
  POST {origin}/careersection/rest/jobboard/searchjobs?lang=en&portal={id}
  - portal ID and CSRF token extracted from the job search HTML page
  - paginated via pageNo parameter

Path 2 — AJAX fallback:
  POST {origin}/careersection/{section}/jobsearch.ajax
  - response is "!|!" delimited text with job data at fixed token offsets

tenant_id stored as "{subdomain}|{career_section}"
  e.g. "aa224|ex", "chn|chn_ex_staff", "uhg|10000"

Ported 1-to-1 from OpenPostings (MIT) JavaScript implementation.
"""
import json
import re
import time
from typing import Any
from urllib.parse import quote

from .base import BaseHarvester, MIN_DELAY_SCRAPE, DEFAULT_TIMEOUT, BOT_USER_AGENT

MAX_PAGES = 25
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml", "User-Agent": BOT_USER_AGENT}
AJAX_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded",
    "x-requested-with": "XMLHttpRequest",
    "tz": "GMT-07:00",
    "tzname": "America/Los_Angeles",
    "User-Agent": BOT_USER_AGENT,
}
REST_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json",
    "x-requested-with": "XMLHttpRequest",
    "tz": "GMT-07:00",
    "tzname": "America/Los_Angeles",
    "User-Agent": BOT_USER_AGENT,
}


class TaleoHarvester(BaseHarvester):
    platform_slug = "taleo"
    is_scraper = True

    def fetch_jobs(self, company, tenant_id: str, since_hours: int = 24) -> list[dict[str, Any]]:
        """
        tenant_id = "{subdomain}|{career_section}"  e.g. "aa224|ex"
        Falls back to guessing section "ex" if no "|" found (old records).
        """
        if not tenant_id:
            return []

        if "|" in tenant_id:
            subdomain, career_section = tenant_id.split("|", 1)
        else:
            subdomain = tenant_id
            career_section = "ex"  # most common default

        lang = "en"
        base_origin = f"https://{subdomain}.taleo.net"
        base_section_url = f"{base_origin}/careersection/{career_section}"
        search_url = f"{base_section_url}/jobsearch.ftl?lang={lang}"

        # Load the search page to extract portal ID + CSRF token
        page_html = self._fetch_html(search_url)
        if not page_html:
            return []

        portal, token_name, token_value = self._extract_rest_config(page_html)
        postings: list[dict] = []
        seen_urls: set[str] = set()

        # ── Path 1: REST API ──────────────────────────────────────────────────
        if portal:
            for page_no in range(1, MAX_PAGES + 1):
                try:
                    data = self._fetch_rest(
                        base_origin, portal, token_name, token_value, lang, page_no
                    )
                    requisitions = data.get("requisitionList") or []
                    if not requisitions:
                        break

                    for p in self._parse_rest(company.name, base_section_url, lang, requisitions):
                        if p["original_url"] not in seen_urls:
                            seen_urls.add(p["original_url"])
                            postings.append(p)

                    paging = data.get("pagingData") or {}
                    total = int(paging.get("totalCount") or 0)
                    page_size = int(paging.get("pageSize") or len(requisitions)) or len(requisitions)
                    if len(requisitions) < page_size or (total and page_no * page_size >= total):
                        break
                    time.sleep(MIN_DELAY_SCRAPE)
                except Exception:
                    break

        if postings:
            return postings

        # ── Path 2: AJAX fallback ─────────────────────────────────────────────
        try:
            ajax_text = self._fetch_ajax(base_section_url, lang, token_value)
            for p in self._parse_ajax(company.name, base_section_url, lang, ajax_text):
                if p["original_url"] not in seen_urls:
                    seen_urls.add(p["original_url"])
                    postings.append(p)
        except Exception:
            pass

        return postings

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _fetch_html(self, url: str) -> str:
        self._enforce_rate_limit()
        try:
            resp = self._session.get(url, timeout=DEFAULT_TIMEOUT, headers=HTML_HEADERS)
            self._last_request_at = time.monotonic()
            if resp.ok:
                return resp.text
        except Exception:
            pass
        return ""

    def _fetch_rest(
        self, origin: str, portal: str, token_name: str, token_value: str, lang: str, page_no: int
    ) -> dict:
        url = f"{origin}/careersection/rest/jobboard/searchjobs?lang={lang}&portal={portal}"
        payload = {
            "multilineEnabled": True,
            "sortingSelection": {"sortBySelectionParam": "1", "ascendingSortingOrder": "false"},
            "fieldData": {
                "fields": {"LOCATION": "", "CATEGORY": "", "KEYWORD": ""},
                "valid": True,
            },
            "filterSelectionParam": {"searchFilterSelections": [
                {"id": "JOB_FIELD", "selectedValues": []},
                {"id": "LOCATION", "selectedValues": []},
                {"id": "ORGANIZATION", "selectedValues": []},
                {"id": "JOB_LEVEL", "selectedValues": []},
            ]},
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
                {"id": "ORGANIZATION", "selectedValues": []},
                {"id": "LOCATION", "selectedValues": []},
                {"id": "JOB_FIELD", "selectedValues": []},
                {"id": "JOB_NUMBER", "selectedValues": []},
                {"id": "URGENT_JOB", "selectedValues": []},
                {"id": "JOB_SHIFT", "selectedValues": []},
            ]},
            "pageNo": page_no,
        }
        headers = dict(REST_HEADERS)
        if token_name and token_value:
            headers[token_name] = token_value

        self._enforce_rate_limit()
        resp = self._session.post(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp.json()

    def _fetch_ajax(self, base_section_url: str, lang: str, csrf_token: str = "") -> str:
        url = f"{base_section_url}/jobsearch.ajax"
        payload = {
            "ftlpageid": "reqListBasicPage",
            "ftlinterfaceid": "requisitionListInterface",
            "ftlcompid": "validateTimeZoneId",
            "jsfCmdId": "validateTimeZoneId",
            "ftlcompclass": "InitTimeZoneAction",
            "ftlcallback": "requisition_restoreDatesValues",
            "ftlajaxid": "ftlx1",
            "tz": "GMT-07:00",
            "tzname": "America/Los_Angeles",
            "lang": lang,
            "isExternal": "true",
            "rlPager.currentPage": "1",
            "listRequisition.size": "25",
            "dropListSize": "25",
        }
        if csrf_token:
            payload["csrftoken"] = csrf_token

        self._enforce_rate_limit()
        resp = self._session.post(url, data=payload, headers=AJAX_HEADERS, timeout=DEFAULT_TIMEOUT)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp.text

    # ── Token extraction ──────────────────────────────────────────────────────

    def _extract_rest_config(self, html: str) -> tuple[str, str, str]:
        portal_m = re.search(r"portal=([0-9]{6,})", html, re.I)
        portal = portal_m.group(1) if portal_m else ""

        token_name = ""
        for pat in [
            r"sessionCSRFTokenName\s*:\s*'([^']+)'",
            r'sessionCSRFTokenName\s*:\s*"([^"]+)"',
            r'"sessionCSRFTokenName"\s*:\s*"([^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                token_name = m.group(1).strip()
                break

        token_value = ""
        for pat in [
            r"sessionCSRFToken\s*:\s*'([^']+)'",
            r'sessionCSRFToken\s*:\s*"([^"]+)"',
            r'"sessionCSRFToken"\s*:\s*"([^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                token_value = m.group(1).strip()
                break

        return portal, token_name, token_value

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_rest(
        self, company_name: str, base_section_url: str, lang: str, requisitions: list
    ) -> list[dict]:
        postings = []
        for req in requisitions:
            job_id = str(req.get("jobId") or req.get("contestNo") or "").strip()
            if not job_id:
                continue
            columns = req.get("column") or []
            title = str(columns[0] if columns else "").strip() or "Untitled Position"
            location = self._location_label(columns[2] if len(columns) > 2 else "")
            posting_date = str(columns[4] if len(columns) > 4 else "").strip()
            contest_no = str(req.get("contestNo") or "").strip()
            detail_ref = contest_no or job_id
            job_url = f"{base_section_url}/jobdetail.ftl?job={quote(detail_ref)}&lang={lang}"
            postings.append({
                "original_url": job_url,
                "title": title,
                "company_name": company_name,
                "location": location,
                "posted_date_raw": posting_date,
                "raw_payload": req,
            })
        return postings

    def _parse_ajax(
        self, company_name: str, base_section_url: str, lang: str, ajax_text: str
    ) -> list[dict]:
        if "!|!" not in ajax_text:
            return []

        tokens = ajax_text.split("!|!")
        postings = []
        seen_keys: set[str] = set()
        apply_prefix = "Apply for this position ("

        for i, token in enumerate(tokens):
            token = token.strip()
            if not token.startswith(apply_prefix):
                continue

            title_from_apply = token[len(apply_prefix):]
            if title_from_apply.endswith(")"):
                title_from_apply = title_from_apply[:-1].strip()

            posted_date = tokens[i - 2].strip() if i >= 2 else ""
            location_raw = tokens[i - 8].strip() if i >= 8 else ""
            job_number = tokens[i - 9].strip() if i >= 9 else ""
            job_id = tokens[i - 14].strip() if i >= 14 else ""
            fallback_title = tokens[i - 13].strip() if i >= 13 else ""

            if not re.match(r"^\d+$", job_id):
                for step in range(1, 21):
                    candidate = tokens[i - step].strip() if i >= step else ""
                    if re.match(r"^\d+$", candidate):
                        job_id = candidate
                        break

            title = title_from_apply or fallback_title or "Untitled Position"
            detail_ref = job_number or job_id
            location = self._location_label(location_raw)
            dedup_key = f"{detail_ref}|{title}|{location}".lower()
            if not detail_ref or dedup_key in seen_keys:
                continue

            seen_keys.add(dedup_key)
            postings.append({
                "original_url": f"{base_section_url}/jobdetail.ftl?job={quote(detail_ref)}&lang={lang}",
                "title": title,
                "company_name": company_name,
                "location": location,
                "posted_date_raw": posted_date,
                "raw_payload": {},
            })

        return postings

    def _location_label(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return " / ".join(str(x).strip() for x in parsed if x)
            except Exception:
                pass
        return text

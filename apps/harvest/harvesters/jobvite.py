"""
JobviteHarvester — Python port of OpenPostings Jobvite scraper.

Jobvite job boards live at: jobs.jobvite.com/{company}/jobs
Jobs are grouped by department in <table class="jv-job-list"> blocks,
each preceded by an <h3> department heading.
Each row has:
  <td class="jv-job-list-name">  → title + job link
  <td class="jv-job-list-location"> → location

Single-page — no pagination needed.

Ported 1-to-1 from OpenPostings (MIT) JavaScript implementation.
"""
import re
import time
from typing import Any
from urllib.parse import urljoin

from .base import BaseHarvester, DEFAULT_TIMEOUT, BOT_USER_AGENT


class JobviteHarvester(BaseHarvester):
    platform_slug = "jobvite"
    is_scraper = True

    BASE_ORIGIN = "https://jobs.jobvite.com"

    def fetch_jobs(self, company, tenant_id: str, since_hours: int = 24) -> list[dict[str, Any]]:
        """
        tenant_id = company slug e.g. "loandepot", "varonis", "leovegas"
        Also handles /careers/{slug} variant automatically.
        """
        if not tenant_id:
            return []

        # Strip "careers/" prefix if stored that way
        slug = tenant_id.lstrip("/")
        if slug.startswith("careers/"):
            slug = slug[len("careers/"):]

        jobs_url = f"{self.BASE_ORIGIN}/{slug}/jobs"
        html = self._fetch_html(jobs_url)
        if not html:
            # Try alternate careers path
            html = self._fetch_html(f"{self.BASE_ORIGIN}/careers/{slug}/jobs")
        if not html:
            return []

        return self._parse_postings(company.name, slug, html)

    # ── HTML helpers ──────────────────────────────────────────────────────────

    def _fetch_html(self, url: str) -> str:
        self._enforce_rate_limit()
        try:
            resp = self._session.get(
                url, timeout=DEFAULT_TIMEOUT,
                headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": BOT_USER_AGENT},
            )
            self._last_request_at = time.monotonic()
            if resp.ok:
                return resp.text
        except Exception:
            pass
        return ""

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_postings(self, company_name: str, slug: str, html: str) -> list[dict]:
        postings: list[dict] = []
        seen: set[str] = set()

        # Department-grouped tables: <h3>Department</h3> <table class="jv-job-list">...</table>
        table_pat = re.compile(
            r"<h3[^>]*>([\s\S]*?)</h3>\s*"
            r'<table[^>]*class=["\'][^"\']*\bjv-job-list\b[^"\']*["\'][^>]*>([\s\S]*?)</table>',
            re.I,
        )
        row_pat = re.compile(
            r"<tr[^>]*>[\s\S]*?"
            r'<td[^>]*class=["\'][^"\']*\bjv-job-list-name\b[^"\']*["\'][^>]*>[\s\S]*?'
            r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>[\s\S]*?</td>[\s\S]*?'
            r'<td[^>]*class=["\'][^"\']*\bjv-job-list-location\b[^"\']*["\'][^>]*>([\s\S]*?)</td>'
            r'[\s\S]*?</tr>',
            re.I,
        )

        def push_rows(rows_html: str, department: str = "") -> None:
            for m in row_pat.finditer(rows_html):
                href = m.group(1).strip()
                abs_url = urljoin(self.BASE_ORIGIN + "/", href) if href else ""
                if not abs_url or abs_url in seen:
                    continue
                postings.append({
                    "original_url": abs_url,
                    "title": self._clean(m.group(2)) or "Untitled Position",
                    "company_name": company_name,
                    "location": self._clean(m.group(3)) or "",
                    "department": self._clean(department) or "",
                    "posted_date_raw": "",
                    "raw_payload": {},
                })
                seen.add(abs_url)

        matched = False
        for table_m in table_pat.finditer(html):
            push_rows(table_m.group(2), table_m.group(1))
            matched = True

        # Fallback: try without department grouping (some Jobvite pages differ)
        if not matched:
            push_rows(html)

        return postings

    def _clean(self, value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value or "")
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s*,\s*", ", ", text)
        return text.strip()

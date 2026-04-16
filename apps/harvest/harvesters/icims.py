"""
iCIMSHarvester — Python port of OpenPostings iCIMS scraper.

iCIMS job boards use an iframe pattern:
  1. Fetch {tenant}.icims.com/jobs/search?ss=1
  2. Extract iframe src from the wrapper page
  3. Parse <li class="iCIMS_JobCardItem"> elements inside the iframe
  4. Follow <link rel="next"> for pagination (up to 25 pages)

Ported 1-to-1 from OpenPostings (MIT) JavaScript implementation.
"""
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse, urlencode, parse_qs

from .base import BaseHarvester, MIN_DELAY_SCRAPE, DEFAULT_TIMEOUT, BOT_USER_AGENT

MAX_PAGES = 25


class IcimsHarvester(BaseHarvester):
    platform_slug = "icims"
    is_scraper = True

    # ── Public interface ──────────────────────────────────────────────────────

    def fetch_jobs(self, company, tenant_id: str, since_hours: int = 24) -> list[dict[str, Any]]:
        """
        tenant_id = full subdomain e.g. "careers-audacy", "careers-samaritanvillage"
        """
        if not tenant_id:
            return []

        base_origin = f"https://{tenant_id}.icims.com"
        search_url = f"{base_origin}/jobs/search?ss=1"

        wrapper_html = self._fetch_html(search_url)
        if not wrapper_html:
            return []

        page_url = self._extract_iframe_url(wrapper_html, search_url)
        page_url = self._ensure_iframe_url(page_url)

        collected: list[dict] = []
        seen_urls: set[str] = set()
        seen_pages: set[str] = set()

        for _ in range(MAX_PAGES):
            if not page_url or page_url in seen_pages:
                break
            seen_pages.add(page_url)

            page_html = self._fetch_html(page_url)
            if not page_html:
                break

            for posting in self._parse_postings(company.name, base_origin, page_html):
                url = posting.get("original_url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    collected.append(posting)

            next_url = self._extract_next_page(page_html, page_url)
            if not next_url:
                break
            page_url = next_url
            time.sleep(MIN_DELAY_SCRAPE)

        return collected

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

    def _ensure_iframe_url(self, url: str) -> str:
        if not url:
            return url
        parsed = urlparse(url)
        params = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        params["in_iframe"] = "1"
        return parsed._replace(query=urlencode(params)).geturl()

    def _extract_iframe_url(self, html: str, base_url: str) -> str:
        for pattern in [
            r"icimsFrame\.src\s*=\s*'([^']+)'",
            r'icimsFrame\.src\s*=\s*"([^"]+)"',
            r'<iframe[^>]*id=["\']icims_content_iframe["\'][^>]*src=["\']([^"\']+)["\']',
        ]:
            m = re.search(pattern, html, re.I)
            if m:
                candidate = m.group(1).strip()
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                elif not re.match(r"https?://", candidate, re.I):
                    candidate = urljoin(base_url, candidate)
                return self._ensure_iframe_url(candidate)
        return self._ensure_iframe_url(base_url)

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_postings(self, company_name: str, origin: str, html: str) -> list[dict]:
        postings: list[dict] = []
        seen: set[str] = set()

        card_pat = re.compile(
            r'<li[^>]*class=["\'][^"\']*iCIMS_JobCardItem[^"\']*["\'][^>]*>([\s\S]*?)</li>', re.I
        )
        for card_m in card_pat.finditer(html):
            card_html = card_m.group(1)
            link_m = re.search(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', card_html, re.I)
            if not link_m:
                continue
            href = link_m.group(1).strip()
            if not re.search(r"/jobs/\d+", href, re.I):
                continue
            abs_url = urljoin(origin + "/", href)
            if abs_url in seen or "/jobs/intro" in abs_url.lower():
                continue

            title_m = re.search(r"<h[1-6][^>]*>([\s\S]*?)</h[1-6]>", link_m.group(2), re.I)
            postings.append({
                "original_url": abs_url,
                "title": self._clean(title_m.group(1) if title_m else link_m.group(2)) or "Untitled",
                "company_name": company_name,
                "location": self._extract_location(card_html),
                "posted_date_raw": self._extract_date(card_html),
                "raw_payload": {},
            })
            seen.add(abs_url)

        if postings:
            return postings

        # Fallback: any /jobs/\d+ links in the page
        for m in re.finditer(
            r'<a[^>]*href=["\']([^"\']*\/jobs\/\d+[^"\']*)["\'][^>]*>([\s\S]*?)</a>', html, re.I
        ):
            href = m.group(1).strip()
            abs_url = urljoin(origin + "/", href)
            if abs_url in seen or "/jobs/intro" in abs_url.lower():
                continue
            title_m = re.search(r"<h[1-6][^>]*>([\s\S]*?)</h[1-6]>", m.group(2), re.I)
            ctx = html[max(0, m.start() - 800): m.end() + 2200]
            postings.append({
                "original_url": abs_url,
                "title": self._clean(title_m.group(1) if title_m else m.group(2)) or "Untitled",
                "company_name": company_name,
                "location": self._extract_location(ctx),
                "posted_date_raw": self._extract_date(ctx),
                "raw_payload": {},
            })
            seen.add(abs_url)

        return postings

    def _extract_location(self, html: str) -> str:
        for pat in [
            r'field-label">Location\s*</span>\s*</dt>\s*<dd[^>]*class=["\'][^"\']*iCIMS_JobHeaderData[^"\']*["\'][^>]*>\s*<span[^>]*>([\s\S]*?)</span>',
            r'glyphicons-map-marker[^>]*>[\s\S]*?</dt>\s*<dd[^>]*class=["\'][^"\']*iCIMS_JobHeaderData[^"\']*["\'][^>]*>\s*<span[^>]*>([\s\S]*?)</span>',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                loc = self._clean(m.group(1))
                if loc:
                    return loc
        return ""

    def _extract_date(self, html: str) -> str:
        m = re.search(
            r'field-label">Date Posted\s*</span>\s*<span[^>]*?(?:title=["\']([^"\']+)["\'])?[^>]*>\s*([^<]*)',
            html, re.I,
        )
        if m:
            return (m.group(1) or m.group(2) or "").strip()
        return ""

    def _extract_next_page(self, html: str, current_url: str) -> str:
        for pat in [
            r'<link[^>]*rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']',
            r'<link[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                candidate = m.group(1).strip()
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                elif not re.match(r"https?://", candidate, re.I):
                    candidate = urljoin(current_url, candidate)
                candidate = self._ensure_iframe_url(candidate)
                if candidate != current_url:
                    return candidate
        return ""

    def _clean(self, value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value or "")
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s*,\s*", ", ", text)
        return text.strip()

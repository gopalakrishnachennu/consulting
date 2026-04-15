"""
HTMLScrapeHarvester — Ethical HTML Fallback Scraper

Used ONLY for platforms without a public JSON API (ApplicantPro, ApplyToJob,
Taleo, iCIMS, Recruitee, Jobvite, UltiPro, Zoho Recruit, etc.).

Strict scraping policies enforced:
  1. robots.txt MUST allow access (checked before every fetch)
  2. Honest User-Agent — GoCareers-Bot, never spoofs a browser
  3. Minimum 4-second delay between requests (stricter than API callers)
  4. Hard timeout — 15 seconds per request
  5. Retry + backoff — up to 3 attempts (BaseHarvester)
  6. Result cap — maximum 50 jobs per company per run
  7. Only follows links that look like job listings (safe selectors)
"""
import logging
from typing import Any
from urllib.parse import urljoin

from .base import BaseHarvester

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    logger.warning(
        "[HARVEST] beautifulsoup4 not installed — HTML scraping disabled. "
        "Run: pip install beautifulsoup4"
    )

# CSS selectors for job listing links (conservative — avoids nav/footer noise)
JOB_SELECTORS = [
    "a[href*='/jobs/']",
    "a[href*='/careers/']",
    "a[href*='/position/']",
    "a[href*='/opening/']",
    "a[href*='/apply/']",
    ".job-title a",
    ".position-title a",
    ".job-listing a",
    "[class*='job'] a",
    "[class*='career'] a",
]

MAX_JOBS_PER_COMPANY = 50


class HTMLScrapeHarvester(BaseHarvester):
    """
    Generic HTML scraper for ATS platforms without public APIs.

    is_scraper=True activates strict mode in BaseHarvester:
      - robots.txt checked on every request
      - 4-second minimum delay between requests
    """

    platform_slug = "html_scrape"
    is_scraper = True       # ← activates strict compliance in BaseHarvester

    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        if not BS4_AVAILABLE:
            return []

        url = (
            tenant_id
            or getattr(company, "career_site_url", "")
            or getattr(company, "website", "")
        )
        if not url:
            logger.debug(
                "[HARVEST] HTMLScraper: no URL for company %s — skipping", company.name
            )
            return []

        # Fetch with robots.txt check + rate limiting (check_robots=True handled
        # automatically because is_scraper=True in BaseHarvester)
        data = self._get(url, check_robots=True)

        if isinstance(data, dict) and "error" in data:
            error = data["error"]
            if "robots.txt" in error:
                logger.info(
                    "[HARVEST] HTMLScraper: robots.txt blocked %s for %s",
                    url, company.name,
                )
            return []

        # At this point data is NOT json — it's a string; we used _get which
        # calls resp.json(). We need raw HTML. Override with _get_html:
        html_text = self._get_html(url)
        if not html_text:
            return []

        try:
            soup = BeautifulSoup(html_text, "html.parser")
        except Exception as exc:
            logger.warning("[HARVEST] HTMLScraper: parse error for %s: %s", url, exc)
            return []

        seen: set[str] = set()
        results: list[dict[str, Any]] = []

        for selector in JOB_SELECTORS:
            for el in soup.select(selector):
                href = el.get("href", "")
                text = el.get_text(strip=True)
                if not href or not text or len(text) < 5 or len(text) > 300:
                    continue
                full_url = urljoin(url, href)
                if full_url in seen:
                    continue
                seen.add(full_url)
                results.append({
                    "external_id": "",
                    "original_url": full_url,
                    "title": text[:300],
                    "company_name": company.name,
                    "location": "",
                    "raw_payload": {"source_url": url, "scraped_html": True},
                })
                if len(results) >= MAX_JOBS_PER_COMPANY:
                    break
            if len(results) >= MAX_JOBS_PER_COMPANY:
                break

        logger.info(
            "[HARVEST] HTMLScraper: %d jobs found for %s at %s",
            len(results), company.name, url,
        )
        return results

    def _get_html(self, url: str) -> str | None:
        """Fetch raw HTML (not JSON). Respects all compliance rules."""
        import time
        from .base import (
            _check_robots_allowed, MIN_DELAY_SCRAPE, DEFAULT_TIMEOUT,
            MAX_RETRIES, BACKOFF_FACTOR, BOT_USER_AGENT,
        )
        import requests

        if not _check_robots_allowed(url):
            logger.warning("[HARVEST] HTMLScraper: robots.txt blocked %s", url)
            return None

        # Rate limit
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_DELAY_SCRAPE:
            time.sleep(MIN_DELAY_SCRAPE - elapsed)

        headers = {
            "User-Agent": BOT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
                self._last_request_at = time.monotonic()

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", BACKOFF_FACTOR ** attempt))
                    time.sleep(min(wait, 120))
                    continue
                if resp.status_code >= 500:
                    time.sleep(BACKOFF_FACTOR ** attempt)
                    continue
                if resp.status_code >= 400:
                    logger.warning("[HARVEST] HTMLScraper: HTTP %s for %s", resp.status_code, url)
                    return None

                return resp.text

            except requests.exceptions.Timeout:
                time.sleep(BACKOFF_FACTOR ** attempt)
            except Exception as exc:
                logger.warning("[HARVEST] HTMLScraper: error fetching %s: %s", url, exc)
                return None

        return None

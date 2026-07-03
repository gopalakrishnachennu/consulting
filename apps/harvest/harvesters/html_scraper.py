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
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from .base import BaseHarvester
from ..career_url import build_career_url
from ..obscura import fetch_html_with_obscura, record_obscura_failure

if TYPE_CHECKING:
    from ..models import PlatformEngineConfig

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
        self, company, tenant_id: str, since_hours: int = 24, fetch_all: bool = False
    ) -> list[dict[str, Any]]:
        if not BS4_AVAILABLE:
            return []

        # tenant_id is usually a short code (e.g. "bookofthemonth"), not a URL.
        # Build the public career URL from platform slug + tenant first.
        platform_slug = ""
        try:
            platform_obj = getattr(company, "platform_label", None)
            if platform_obj and getattr(platform_obj, "platform", None):
                platform_slug = platform_obj.platform.slug or ""
        except Exception:
            platform_slug = ""
        runtime_cfg = self._runtime_config_for_company(company)
        url = (
            (build_career_url(platform_slug, tenant_id) if platform_slug and tenant_id else "")
            or getattr(company, "career_site_url", "")
            or getattr(company, "website", "")
        )
        if not url:
            logger.debug(
                "[HARVEST] HTMLScraper: no URL for company %s — skipping", company.name
            )
            return []

        # Generic fallback pages are HTML (not JSON), so fetch directly using
        # _get_html() which still enforces robots + rate-limit.
        html_text, render_backend = self._get_html(url, runtime_cfg=runtime_cfg)
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
                    "raw_payload": {
                        "source_url": url,
                        "scraped_html": True,
                        "render_backend": render_backend,
                    },
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

    @staticmethod
    def _runtime_config_for_company(company) -> "PlatformEngineConfig | None":
        from ..models import PlatformEngineConfig
        try:
            platform_obj = getattr(getattr(company, "platform_label", None), "platform", None)
            if not platform_obj:
                return None
            return getattr(platform_obj, "config", None)
        except PlatformEngineConfig.DoesNotExist:
            return None
        except Exception:
            return None

    def _respect_html_fetch_policy(self, url: str) -> bool:
        from .base import _check_robots_allowed, MIN_DELAY_SCRAPE
        if not _check_robots_allowed(url):
            logger.warning("[HARVEST] HTMLScraper: robots.txt blocked %s", url)
            return False

        # Rate limit
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_DELAY_SCRAPE:
            time.sleep(MIN_DELAY_SCRAPE - elapsed)
        return True

    def _get_html(
        self,
        url: str,
        *,
        runtime_cfg: "PlatformEngineConfig | None" = None,
    ) -> tuple[str | None, str]:
        """Fetch raw HTML (not JSON). Respects all compliance rules."""
        from ..models import PlatformEngineConfig
        if not self._respect_html_fetch_policy(url):
            return None, "blocked"

        backend = (
            getattr(runtime_cfg, "html_render_backend", PlatformEngineConfig.HtmlRenderBackend.REQUESTS)
            if runtime_cfg and getattr(runtime_cfg, "is_active", True)
            else PlatformEngineConfig.HtmlRenderBackend.REQUESTS
        )
        if backend in {
            PlatformEngineConfig.HtmlRenderBackend.OBSCURA_AUTO,
            PlatformEngineConfig.HtmlRenderBackend.OBSCURA_ONLY,
        }:
            try:
                rendered = fetch_html_with_obscura(url)
                self._last_request_at = time.monotonic()
                return rendered, "obscura"
            except Exception as exc:
                if backend == PlatformEngineConfig.HtmlRenderBackend.OBSCURA_ONLY:
                    record_obscura_failure(
                        url=url,
                        backend_mode=backend,
                        exc=exc,
                        fallback_used=False,
                    )
                    logger.warning("[HARVEST] HTMLScraper: Obscura-only fetch failed for %s: %s", url, exc)
                    return None, "obscura_failed"
                record_obscura_failure(
                    url=url,
                    backend_mode=backend,
                    exc=exc,
                    fallback_used=True,
                )
                logger.info("[HARVEST] HTMLScraper: Obscura fallback to requests for %s: %s", url, exc)

        return self._get_html_requests(url), "requests"

    def _get_html_requests(self, url: str) -> str | None:
        """Fetch raw HTML using requests after compliance checks have passed."""
        from .base import DEFAULT_TIMEOUT, MAX_RETRIES, BACKOFF_FACTOR, BOT_USER_AGENT
        import requests

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

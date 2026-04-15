import random
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

from .base import BaseHarvester

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

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
    "h2 > a",
    "h3 > a",
]


class HTMLScrapeHarvester(BaseHarvester):
    """Generic HTML scraper fallback for platforms without public APIs."""

    platform_slug = "html_scrape"

    def fetch_jobs(self, company, tenant_id: str, since_hours: int = 24) -> list[dict[str, Any]]:
        url = tenant_id or getattr(company, "career_site_url", "") or getattr(company, "website", "")
        if not url or not BS4_AVAILABLE:
            return []

        # Rate limiting
        time.sleep(random.uniform(1.5, 4.0))

        try:
            resp = requests.get(url, timeout=15, headers=SCRAPE_HEADERS)
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:
            return []

        seen: set[str] = set()
        results: list[dict[str, Any]] = []

        for selector in JOB_SELECTORS:
            for el in soup.select(selector):
                href = el.get("href", "")
                text = el.get_text(strip=True)
                if not href or not text or len(text) < 5:
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
                if len(results) >= 50:
                    break
            if len(results) >= 50:
                break

        return results

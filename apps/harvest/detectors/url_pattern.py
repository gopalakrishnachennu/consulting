from typing import Optional
from . import URL_PATTERNS

BOT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GoCareers-Bot/1.0; +https://gocareers.io)",
}


class URLPatternDetector:
    """Step 1: Direct URL substring match against known platform patterns."""

    def detect(self, company) -> tuple[Optional[str], str, str]:
        urls = []
        if getattr(company, "career_site_url", ""):
            urls.append(company.career_site_url.lower())
        if getattr(company, "website", ""):
            urls.append(company.website.lower())

        for url in urls:
            for slug, patterns in URL_PATTERNS.items():
                for pattern in patterns:
                    if pattern in url:
                        return slug, "HIGH", "URL_PATTERN"

        return None, "UNKNOWN", "UNDETECTED"

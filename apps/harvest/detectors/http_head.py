from typing import Optional

import requests

from . import URL_PATTERNS

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GoCareers-Bot/1.0; +https://gocareers.io)",
}


class HTTPHeadDetector:
    """Step 2: Follow HTTP redirects and check final/intermediate URLs."""

    def detect(self, company) -> tuple[Optional[str], str, str]:
        url = getattr(company, "career_site_url", "") or getattr(company, "website", "")
        if not url:
            return None, "UNKNOWN", "UNDETECTED"

        try:
            resp = requests.head(url, allow_redirects=True, timeout=8, headers=HEADERS)

            # Check final URL
            final = resp.url.lower()
            for slug, patterns in URL_PATTERNS.items():
                for pat in patterns:
                    if pat in final:
                        return slug, "MEDIUM", "HTTP_HEAD"

            # Check redirect chain Location headers
            for r in resp.history:
                loc = r.headers.get("Location", "").lower()
                for slug, patterns in URL_PATTERNS.items():
                    for pat in patterns:
                        if pat in loc:
                            return slug, "MEDIUM", "HTTP_HEAD"

        except Exception:
            pass

        return None, "UNKNOWN", "UNDETECTED"

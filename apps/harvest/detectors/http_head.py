"""
HTTPHeadDetector — Step 2 of platform detection pipeline

Sends an HTTP HEAD request and follows redirects to discover ATS platform
from the final URL or Location headers.

Compliance:
  - Honest User-Agent (GoCareers-Bot, not a browser)
  - 8-second timeout
  - Robots.txt NOT checked here — HEAD requests are low-impact and robots.txt
    typically governs GET/POST, not HEAD
"""
import logging
from typing import Optional

import requests

from . import URL_PATTERNS
from ..harvesters.base import BOT_USER_AGENT

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": BOT_USER_AGENT,
}


class HTTPHeadDetector:
    """Step 2: Follow HTTP redirects and check final/intermediate URLs."""

    def detect(self, company) -> tuple[Optional[str], str, str]:
        url = (
            getattr(company, "career_site_url", "")
            or getattr(company, "website", "")
        )
        if not url:
            return None, "UNKNOWN", "UNDETECTED"

        try:
            resp = requests.head(
                url, allow_redirects=True, timeout=8, headers=HEADERS
            )
            logger.debug(
                "[DETECT] HEAD %s → %s (final: %s)", url, resp.status_code, resp.url
            )

            # Check final URL
            final = resp.url.lower()
            for slug, patterns in URL_PATTERNS.items():
                for pat in patterns:
                    if pat in final:
                        return slug, "MEDIUM", "HTTP_HEAD"

            # Check every redirect Location header
            for r in resp.history:
                loc = r.headers.get("Location", "").lower()
                for slug, patterns in URL_PATTERNS.items():
                    for pat in patterns:
                        if pat in loc:
                            return slug, "MEDIUM", "HTTP_HEAD"

        except requests.exceptions.Timeout:
            logger.debug("[DETECT] HEAD timeout for %s", url)
        except requests.exceptions.ConnectionError:
            logger.debug("[DETECT] HEAD connection error for %s", url)
        except Exception as exc:
            logger.debug("[DETECT] HEAD error for %s: %s", url, exc)

        return None, "UNKNOWN", "UNDETECTED"

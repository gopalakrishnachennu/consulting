"""
HTMLParseDetector — Step 3 of platform detection pipeline

Fetches the company's career page HTML and scans for ATS-specific fingerprints
embedded in the page (script tags, iframe src, widget class names, etc.).

Compliance:
  - Honest User-Agent (GoCareers-Bot, not a spoofed browser)
  - robots.txt checked before fetching
  - 12-second timeout
  - Only reads publicly accessible career pages
"""
import logging
import time
from typing import Optional
from urllib.robotparser import RobotFileParser

import requests

from ..harvesters.base import BOT_USER_AGENT, _check_robots_allowed

logger = logging.getLogger(__name__)

HTML_SIGNATURES: dict[str, list[str]] = {
    "workday":            ["myworkdayjobs.com", "workday.com/assets"],
    "greenhouse":         ["boards.greenhouse.io", "greenhouse-job-board"],
    "lever":              ["jobs.lever.co", "lever-jobs"],
    "ashby":              ["ashbyhq.com", "ashby-job-board"],
    "jobvite":            ["jobs.jobvite.com"],
    "icims":              ["icims.com"],
    "recruitee":          ["recruitee.com"],
    "taleo":              ["taleo.net"],
    "zoho":               ["zoho.com/recruit"],
    "ultipro":            ["ultipro.com", "ukg.com"],
    "applicantpro":       ["applicantpro.com"],
    "applytojob":         ["applytojob.com"],
    "theapplicantmanager": ["theapplicantmanager.com"],
}

HEADERS = {
    "User-Agent": BOT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class HTMLParseDetector:
    """Step 3: Fetch career page and scan HTML for ATS fingerprints."""

    def detect(self, company) -> tuple[Optional[str], str, str]:
        url = (
            getattr(company, "career_site_url", "")
            or getattr(company, "website", "")
        )
        if not url:
            return None, "UNKNOWN", "UNDETECTED"

        # robots.txt gate — skip if disallowed
        if not _check_robots_allowed(url):
            logger.info(
                "[DETECT] HTMLParse: robots.txt blocked %s for company %s",
                url, getattr(company, "name", "?"),
            )
            return None, "UNKNOWN", "UNDETECTED"

        try:
            resp = requests.get(url, timeout=12, headers=HEADERS)
            logger.debug(
                "[DETECT] GET %s → %s (%d bytes)",
                url, resp.status_code, len(resp.content),
            )

            if resp.status_code >= 400:
                return None, "UNKNOWN", "UNDETECTED"

            html = resp.text.lower()
            for slug, sigs in HTML_SIGNATURES.items():
                for sig in sigs:
                    if sig.lower() in html:
                        return slug, "LOW", "HTML_PARSE"

        except requests.exceptions.Timeout:
            logger.debug("[DETECT] HTMLParse timeout for %s", url)
        except requests.exceptions.ConnectionError:
            logger.debug("[DETECT] HTMLParse connection error for %s", url)
        except Exception as exc:
            logger.debug("[DETECT] HTMLParse error for %s: %s", url, exc)

        return None, "UNKNOWN", "UNDETECTED"

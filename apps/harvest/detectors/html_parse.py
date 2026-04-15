from typing import Optional

import requests

HTML_SIGNATURES: dict[str, list[str]] = {
    "workday": ["myworkdayjobs.com", "workday.com/assets"],
    "greenhouse": ["boards.greenhouse.io", "greenhouse-job-board"],
    "lever": ["jobs.lever.co", "lever-jobs"],
    "ashby": ["ashbyhq.com", "ashby-job-board"],
    "jobvite": ["jobs.jobvite.com"],
    "icims": ["icims.com"],
    "recruitee": ["recruitee.com"],
    "taleo": ["taleo.net"],
    "zoho": ["zoho.com/recruit"],
    "ultipro": ["ultipro.com", "ukg.com"],
    "applicantpro": ["applicantpro.com"],
    "applytojob": ["applytojob.com"],
    "theapplicantmanager": ["theapplicantmanager.com"],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


class HTMLParseDetector:
    """Step 3: Fetch careers page and scan HTML for ATS fingerprints."""

    def detect(self, company) -> tuple[Optional[str], str, str]:
        url = getattr(company, "career_site_url", "") or getattr(company, "website", "")
        if not url:
            return None, "UNKNOWN", "UNDETECTED"

        try:
            resp = requests.get(url, timeout=12, headers=HEADERS)
            html = resp.text.lower()

            for slug, sigs in HTML_SIGNATURES.items():
                for sig in sigs:
                    if sig.lower() in html:
                        return slug, "LOW", "HTML_PARSE"

        except Exception:
            pass

        return None, "UNKNOWN", "UNDETECTED"

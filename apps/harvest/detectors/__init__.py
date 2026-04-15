import re

URL_PATTERNS: dict[str, list[str]] = {
    "workday": [
        "myworkdayjobs.com",
        "wd1.myworkday.com",
        "wd3.myworkday.com",
        "wd5.myworkday.com",
    ],
    "greenhouse": ["boards.greenhouse.io", "greenhouse.io/jobs"],
    "lever": ["jobs.lever.co", "lever.co"],
    "ashby": ["ashbyhq.com", "jobs.ashbyhq.com"],
    "jobvite": ["jobs.jobvite.com", "jobvite.com"],
    "icims": ["icims.com"],
    "recruitee": ["recruitee.com"],
    "taleo": ["taleo.net"],
    "zoho": ["zoho.com/recruit", "jobs.zoho.com"],
    "ultipro": ["ultipro.com", "ukg.com", "recruiting.ukg.net"],
    "applicantpro": ["applicantpro.com"],
    "applytojob": ["applytojob.com"],
    "theapplicantmanager": ["theapplicantmanager.com"],
}

TENANT_EXTRACTORS: dict[str, re.Pattern] = {
    "workday": re.compile(r"https?://([^.]+)\.myworkdayjobs\.com", re.I),
    "greenhouse": re.compile(r"boards\.greenhouse\.io/([^/?#\s]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([^/?#\s]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([^/?#\s]+)", re.I),
}


def extract_tenant(platform_slug: str, url: str) -> str:
    extractor = TENANT_EXTRACTORS.get(platform_slug)
    if extractor and url:
        m = extractor.search(url)
        if m:
            return m.group(1)
    return ""


def run_detection_pipeline(company) -> tuple[str | None, str, str]:
    """
    Run 3-step detection pipeline.
    Returns (platform_slug or None, confidence, detection_method).
    """
    from .url_pattern import URLPatternDetector
    from .http_head import HTTPHeadDetector
    from .html_parse import HTMLParseDetector

    slug, confidence, method = URLPatternDetector().detect(company)
    if not slug:
        slug, confidence, method = HTTPHeadDetector().detect(company)
    if not slug:
        slug, confidence, method = HTMLParseDetector().detect(company)
    return slug, confidence, method

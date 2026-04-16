import re

URL_PATTERNS: dict[str, list[str]] = {
    "workday": [
        "myworkdayjobs.com",
        "wd1.myworkday.com",
        "wd3.myworkday.com",
        "wd5.myworkday.com",
    ],
    "greenhouse": [
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "greenhouse.io/jobs",
    ],
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
    # ── New platforms discovered from real job URL analysis ───────────────────
    "workable": ["apply.workable.com", "workable.com/j/"],
    "bamboohr": ["bamboohr.com"],
    "smartrecruiters": ["jobs.smartrecruiters.com", "smartrecruiters.com/jobs"],
    "dayforce": ["jobs.dayforcehcm.com", "dayforcehcm.com"],
    "adp": ["workforcenow.adp.com", "myjobs.adp.com"],
    "oracle": ["oraclecloud.com/hcmUI", "fa.ocs.oraclecloud.com", "fa.us2.oraclecloud.com"],
}

TENANT_EXTRACTORS: dict[str, re.Pattern] = {
    # Workday is handled by _extract_workday_tenant() — not a simple regex.
    # Stored as "{tenant}|{jobboard}" e.g. "inotivco|EXT", "godirect|voya_jobs"
    "workday": re.compile(r"([^./]+)\.(?:wd\d+\.)?myworkdayjobs\.com", re.I),
    # ── Greenhouse: boards.greenhouse.io/{tenant} or job-boards.greenhouse.io/{tenant}
    "greenhouse": re.compile(r"(?:job-)?boards\.greenhouse\.io/([^/?#\s]+)", re.I),
    # ── Lever: jobs.lever.co/{tenant}
    "lever": re.compile(r"jobs\.lever\.co/([^/?#\s]+)", re.I),
    # ── Ashby: jobs.ashbyhq.com/{tenant}
    "ashby": re.compile(r"(?:jobs\.)?ashbyhq\.com/([^/?#\s]+)", re.I),
    # ── Jobvite: jobs.jobvite.com/careers/{tenant} or jobs.jobvite.com/{tenant}
    "jobvite": re.compile(r"jobs\.jobvite\.com/(?:careers/)?([^/?#\s]+)", re.I),
    # ── iCIMS: {tenant}.icims.com  ([^/.] prevents capturing https:// prefix)
    "icims": re.compile(r"([^/.]+)\.icims\.com", re.I),
    # ── Recruitee: {tenant}.recruitee.com
    "recruitee": re.compile(r"([^/.]+)\.recruitee\.com", re.I),
    # ── Taleo: {subdomain}.taleo.net/careersection/{section}/...
    # Stored as "{subdomain}|{career_section}" e.g. "aa224|ex", "uhg|10000"
    "taleo": re.compile(r"([^/.]+)\.taleo\.net(?:/careersection/([^/?#\s]+))?", re.I),
    # ── UltiPro/UKG: recruiting.ultipro.com/{TENANT_CODE}/JobBoard/...
    "ultipro": re.compile(
        r"(?:recruiting\d*\.ultipro\.com|recruiting\.ukg\.net)/([^/?#\s]+)", re.I
    ),
    # ── ApplicantPro: {tenant}.applicantpro.com
    "applicantpro": re.compile(r"([^/.]+)\.applicantpro\.com", re.I),
    # ── ApplyToJob: {tenant}.applytojob.com
    "applytojob": re.compile(r"([^/.]+)\.applytojob\.com", re.I),
    # ── The Applicant Manager: hire.theapplicantmanager.com?org={tenant}
    "theapplicantmanager": re.compile(r"org=([^&\s]+)", re.I),
    # ── Workable: apply.workable.com/{tenant}/j/{id}
    "workable": re.compile(r"(?:apply\.workable\.com|workable\.com/j)/([^/?#\s]+)", re.I),
    # ── BambooHR: {tenant}.bamboohr.com
    "bamboohr": re.compile(r"([^/.]+)\.bamboohr\.com", re.I),
    # ── SmartRecruiters: jobs.smartrecruiters.com/{Tenant}/...
    "smartrecruiters": re.compile(r"(?:jobs\.)?smartrecruiters\.com/([^/?#\s]+)", re.I),
    # ── Dayforce HCM: jobs.dayforcehcm.com/en-US/{tenant}/CANDIDATEPORTAL/...
    "dayforce": re.compile(r"dayforcehcm\.com/[^/]+/([^/?#\s]+)", re.I),
    # ── ADP: myjobs.adp.com/{tenant}/cx  (workforcenow.adp.com has no reusable tenant)
    "adp": re.compile(r"myjobs\.adp\.com/([^/?#\s]+)/cx", re.I),
    # ── Oracle HCM: stored as "{subdomain}|{sites_id}"
    # e.g. "eeho.fa.us2|CX" from https://eeho.fa.us2.oraclecloud.com/hcmUI/.../sites/CX
    "oracle": re.compile(
        r"([^/.]+\.fa\.[^/.]+)\.oraclecloud\.com/hcmUI/CandidateExperience/[^/]+/sites/([^/?#\s]+)",
        re.I,
    ),
}


def _extract_workday_tenant(url: str) -> str:
    """
    Return "{full_subdomain}|{jobboard}" for a Workday URL.

    Workday URLs: {company}.wd{N}.myworkdayjobs.com/{locale?}/{jobboard}/job/...
    We store the FULL subdomain including wd{N} so the career page URL is correct.
    e.g. "inotivco.wd5|EXT", "3m.wd1|Search", "hamiltonlane.wd108|Search"
    The harvester strips the .wd{N} suffix to get the API tenant.
    """
    # Capture company name AND optional .wd{N} part
    m = re.search(r"([^./]+)(\.wd\d+)?\.myworkdayjobs\.com", url, re.I)
    if not m:
        return ""
    company = m.group(1)
    wd_part = m.group(2) or ""          # e.g. ".wd5" or "" for bare myworkdayjobs.com
    full_subdomain = company + wd_part  # e.g. "inotivco.wd5"

    # Path after the host — skip optional locale (en-US, en-GB, de-DE …)
    path_m = re.search(
        r"myworkdayjobs\.com/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([^/?#\s]+)",
        url, re.I,
    )
    if path_m:
        jobboard = path_m.group(1)
        # "job" means we hit the job-detail segment — no board name in URL
        if jobboard.lower() != "job":
            return f"{full_subdomain}|{jobboard}"

    return full_subdomain


def extract_tenant(platform_slug: str, url: str) -> str:
    if platform_slug == "workday":
        return _extract_workday_tenant(url)

    # ── Greenhouse: handle embed format boards.greenhouse.io/embed/job_board?for=SLUG
    if platform_slug == "greenhouse":
        embed_m = re.search(r"greenhouse\.io/embed[^?]*\?.*?for=([^&\s]+)", url, re.I)
        if embed_m:
            return embed_m.group(1)
        m = TENANT_EXTRACTORS["greenhouse"].search(url)
        if m:
            slug = m.group(1)
            if slug.lower() not in ("embed", "job_board"):
                return slug
        return ""

    extractor = TENANT_EXTRACTORS.get(platform_slug)
    if not extractor or not url:
        return ""
    m = extractor.search(url)
    if not m:
        return ""

    # Taleo: groups = (subdomain, career_section) → store as "subdomain|section"
    if platform_slug == "taleo":
        subdomain = m.group(1) or ""
        section = m.group(2) or ""
        if subdomain and section:
            return f"{subdomain}|{section}"
        return subdomain

    # Oracle HCM: groups = (subdomain, sites_id) → store as "subdomain|sites_id"
    # e.g. "eeho.fa.us2|CX"
    if platform_slug == "oracle":
        subdomain = m.group(1) or ""
        sites_id = m.group(2) or ""
        if subdomain and sites_id:
            return f"{subdomain}|{sites_id}"
        return subdomain

    for g in m.groups():
        if g:
            return g
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

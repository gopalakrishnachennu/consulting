"""
Build the public career page URL for a company given its platform slug + tenant_id.

Used in the Company Labels table to show a direct link to each company's job board.
Also used by harvesters to construct the correct entry-point URL.
"""
from __future__ import annotations
import re


def _clean(tenant: str) -> str:
    """Strip any https:// or http:// prefix that may have been captured by old regex."""
    return re.sub(r"^https?://", "", (tenant or "").strip())


def build_career_url(platform_slug: str, tenant_id: str) -> str:
    """
    Return the public career-page URL for a company.
    Returns empty string if the platform/tenant is unknown or incomplete.
    """
    if not platform_slug or not tenant_id:
        return ""

    t = _clean(tenant_id)
    if not t:
        return ""

    builders = {
        "workday":             _workday,
        "greenhouse":          lambda t: f"https://boards.greenhouse.io/{t}",
        "lever":               lambda t: f"https://jobs.lever.co/{t}",
        "ashby":               lambda t: f"https://jobs.ashbyhq.com/{t}",
        "jobvite":             lambda t: f"https://jobs.jobvite.com/{t}/jobs",
        "icims":               lambda t: f"https://{t}.icims.com/jobs/search",
        "taleo":               _taleo,
        "recruitee":           lambda t: f"https://{t}.recruitee.com/",
        # UltiPro/UKG: company code only — /JobBoard suffix is correct public entry point
        "ultipro":             lambda t: f"https://recruiting.ultipro.com/{t}/JobBoard",
        "applicantpro":        lambda t: f"https://{t}.applicantpro.com/jobs/",
        "applytojob":          lambda t: f"https://{t}.applytojob.com/apply",
        "theapplicantmanager": lambda t: f"https://hire.theapplicantmanager.com/?org={t}",
        "zoho":                lambda t: f"https://jobs.zoho.com/portal/{t}/careers",
        "smartrecruiters":     lambda t: f"https://jobs.smartrecruiters.com/{t}",
        "bamboohr":            lambda t: f"https://{t}.bamboohr.com/careers",
        "dayforce":            lambda t: f"https://jobs.dayforcehcm.com/en-US/{t}/CANDIDATEPORTAL/jobs",
        # ADP: myjobs.adp.com/{tenant}/cx/job-listing  (cx alone is a spinner, job-listing shows listings)
        "adp":                 lambda t: f"https://myjobs.adp.com/{t}/cx/job-listing",
        "workable":            lambda t: f"https://apply.workable.com/{t}/",
        # Oracle: stored as "{subdomain}|{sites_id}"  e.g. "eeho.fa.us2|CX"
        "oracle":              _oracle,
    }

    builder = builders.get(platform_slug)
    if not builder:
        return ""
    return builder(t)


def _workday(tenant_id: str) -> str:
    t = _clean(tenant_id)
    if "|" in t:
        tenant, jobboard = t.split("|", 1)
        return f"https://{tenant}.myworkdayjobs.com/en-US/{jobboard}"
    return f"https://{t}.myworkdayjobs.com"


def _taleo(tenant_id: str) -> str:
    t = _clean(tenant_id)
    # Handle legacy bad values like "https://aarcorp|2"
    if "|" in t:
        subdomain, section = t.split("|", 1)
        subdomain = _clean(subdomain)
        return f"https://{subdomain}.taleo.net/careersection/{section}/jobsearch.ftl"
    return f"https://{t}.taleo.net/careersection/ex/jobsearch.ftl"


def _oracle(tenant_id: str) -> str:
    """
    Oracle HCM stored as "{subdomain}|{sites_id}"
    e.g. "eeho.fa.us2|CX"  →  https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX
    """
    t = _clean(tenant_id)
    if "|" in t:
        subdomain, sites_id = t.split("|", 1)
        subdomain = _clean(subdomain)
        return f"https://{subdomain}.oraclecloud.com/hcmUI/CandidateExperience/en/sites/{sites_id}"
    # Old format — just a sites_id with no subdomain, can't build URL
    return ""

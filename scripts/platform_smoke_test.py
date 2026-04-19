#!/usr/bin/env python3
"""
GoCareers — Platform Smoke Test
================================
Tests EACH job-board harvester against 10 real public companies.
Runs entirely standalone — no DB, no Celery, no Django required.

Usage:
    cd /path/to/consulting
    python scripts/platform_smoke_test.py [--platform greenhouse] [--max-jobs 3]

Output:
    - Colour-coded terminal progress
    - JSON report  → logs/smoke_test_YYYY-MM-DD_HH-MM.json
    - Markdown log → logs/smoke_test_YYYY-MM-DD_HH-MM.md   ← send this to any LLM to debug

The Markdown log is structured so that any LLM can understand:
  - Which platform was tested
  - Which company/tenant was used
  - How many jobs were returned
  - Whether descriptions were populated
  - Any errors with full stack traces
  - Suggested root-cause and fix

Author: GoCareers harvest team
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Ensure the project root is on sys.path ────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from harvest.harvesters import get_harvester

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):  print(f"{GREEN}  ✓ {msg}{RESET}")
def err(msg): print(f"{RED}  ✗ {msg}{RESET}")
def warn(msg):print(f"{YELLOW}  ⚠ {msg}{RESET}")
def info(msg):print(f"{CYAN}  → {msg}{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# KNOWN-GOOD TENANTS — 10 real public companies per platform
# (All are publicly reachable; no login required)
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_TENANTS: dict[str, list[dict]] = {

    # ── Verified 2026-04-19 ──────────────────────────────────────────────────────
    "greenhouse": [
        {"company": "Stripe",            "tenant_id": "stripe"},
        {"company": "Airbnb",            "tenant_id": "airbnb"},
        {"company": "Figma",             "tenant_id": "figma"},
        {"company": "Notion",            "tenant_id": "notion"},
        {"company": "Coinbase",          "tenant_id": "coinbase"},
        {"company": "Robinhood",         "tenant_id": "robinhood"},
        {"company": "Duolingo",          "tenant_id": "duolingo"},
        {"company": "Discord",           "tenant_id": "discord"},
        {"company": "Dropbox",           "tenant_id": "dropbox"},
        {"company": "Lyft",              "tenant_id": "lyft"},
    ],

    # tenant_id = Lever company slug  (api.lever.co/v0/postings/{slug})
    "lever": [
        {"company": "Age of Learning",   "tenant_id": "aofl"},
        {"company": "Agiloft",           "tenant_id": "agiloft"},
        {"company": "Aircall",           "tenant_id": "aircall"},
        {"company": "ChowNow",           "tenant_id": "chownow"},
        {"company": "Clear Capital",     "tenant_id": "clearcapital"},
        {"company": "Avante",            "tenant_id": "avante"},
        {"company": "Bellota Labs",      "tenant_id": "bellotalabs"},
        {"company": "Boson AI",          "tenant_id": "bosonai"},
        {"company": "Plaid",             "tenant_id": "plaid"},
        {"company": "CommonLit",         "tenant_id": "commonlit"},
    ],

    # tenant_id = Ashby organizationHostedJobsPageName
    "ashby": [
        {"company": "Vercel",            "tenant_id": "vercel"},
        {"company": "Linear",            "tenant_id": "linear"},
        {"company": "Supabase",          "tenant_id": "supabase"},
        {"company": "Retool",            "tenant_id": "retool"},
        {"company": "Resend",            "tenant_id": "resend"},
        {"company": "Loom",              "tenant_id": "loom"},
        {"company": "Watershed",         "tenant_id": "watershed"},
        {"company": "Wander",            "tenant_id": "wander"},
        {"company": "Ramp",              "tenant_id": "ramp"},
        {"company": "Arc",               "tenant_id": "arc"},
    ],

    # tenant_id = "{subdomain.wdN}|{jobboard_path}"  — DB-verified 2026-04-19
    "workday": [
        {"company": "PayPal",            "tenant_id": "paypal.wd1|jobs"},
        {"company": "Autodesk",          "tenant_id": "autodesk.wd1|Ext"},
        {"company": "Intel",             "tenant_id": "intel.wd1|External"},
        {"company": "CrowdStrike",       "tenant_id": "crowdstrike.wd5|crowdstrikecareers"},
        {"company": "Etsy",              "tenant_id": "etsy.wd5|Etsy_Careers"},
        {"company": "F5",                "tenant_id": "ffive.wd5|f5jobs"},
        {"company": "Fiserv",            "tenant_id": "fiserv.wd5|EXT"},
        {"company": "GEICO",             "tenant_id": "geico.wd1|External"},
        {"company": "Home Depot",        "tenant_id": "homedepot.wd5|CareerDepot"},
        {"company": "Nvidia",            "tenant_id": "nvidia.wd5|NVIDIAExternalCareerSite"},
    ],

    # tenant_id = SmartRecruiters company slug  — DB-verified 2026-04-19
    "smartrecruiters": [
        {"company": "NBC Universal",     "tenant_id": "NBCUniversal3"},
        {"company": "Boyd Gaming",       "tenant_id": "BoydGaming"},
        {"company": "Western Digital",   "tenant_id": "WesternDigital"},
        {"company": "Endava",            "tenant_id": "Endava"},
        {"company": "Equus Workforce",   "tenant_id": "Equus"},
        {"company": "Visa",              "tenant_id": "Visa"},
        {"company": "Ingram Content",    "tenant_id": "IngramContentGroup1"},
        {"company": "California ISO",    "tenant_id": "CaliforniaISO"},
        {"company": "The Wonderful Co",  "tenant_id": "TheWonderfulCompany"},
        {"company": "Sixt",              "tenant_id": "Sixt"},
    ],

    # tenant_id = Workable account slug  (apply.workable.com/api/v3/accounts/{slug})
    "workable": [
        {"company": "Facet Wealth",      "tenant_id": "facetwealth"},
        {"company": "Infotrack US",      "tenant_id": "infotrack-us"},
        {"company": "Innovaccer",        "tenant_id": "innovaccer-analytics"},
        {"company": "LifeMD",            "tenant_id": "lifemdcareers"},
        {"company": "mLabs",             "tenant_id": "mlabs"},
        {"company": "Protera",           "tenant_id": "protera"},
        {"company": "Evolv Technology",  "tenant_id": "evolv-technology"},
        {"company": "Perry Weather",     "tenant_id": "perryweather"},
        {"company": "PrePass",           "tenant_id": "prepass"},
        {"company": "Pony.ai",           "tenant_id": "pony-dot-ai"},
    ],

    # tenant_id = Recruitee company subdomain  ({slug}.recruitee.com)
    "recruitee": [
        {"company": "1X Technologies",  "tenant_id": "1x"},
        {"company": "Hard Rock Digital","tenant_id": "hardrockdigital"},
        {"company": "Incentivio",       "tenant_id": "incentivio"},
        {"company": "Viderity",         "tenant_id": "viderity"},
        {"company": "Xebia Poland",     "tenant_id": "xebiapoland"},
    ],

    # tenant_id = BambooHR subdomain  ({slug}.bamboohr.com)  — DB-verified 2026-04-19
    "bamboohr": [
        {"company": "Anchor QEA",        "tenant_id": "anchorqea"},
        {"company": "Bird Rock Systems", "tenant_id": "birdrock"},
        {"company": "Catalyst Consulting","tenant_id": "catconsult"},
        {"company": "Cloudhesive",       "tenant_id": "cloudhesive"},
        {"company": "Context Labs",      "tenant_id": "contextlabs"},
        {"company": "Cornelis Networks", "tenant_id": "cornelisnetworks"},
        {"company": "COVU",              "tenant_id": "covu"},
        {"company": "Extensiv",          "tenant_id": "extensiv"},
        {"company": "Gorilla Logic",     "tenant_id": "gorillalogic"},
        {"company": "Prometheus Group",  "tenant_id": "prometheusgroup"},
    ],

    # tenant_id = iCIMS full subdomain  ({tenant}.icims.com)  — DB-verified 2026-04-19
    "icims": [
        {"company": "Audacy",            "tenant_id": "careers-audacy"},
        {"company": "Atlas Air",         "tenant_id": "careers-atlasair"},
        {"company": "Axway",             "tenant_id": "careers-axway"},
        {"company": "Applied Systems",   "tenant_id": "careers-appliedsystems"},
        {"company": "Charles Schwab",    "tenant_id": "career-schwab"},
        {"company": "Cook Medical",      "tenant_id": "americas-cookmedical"},
        {"company": "Devereux",          "tenant_id": "careers-devereux"},
        {"company": "Fujifilm",          "tenant_id": "uscareers-fujifilm"},
        {"company": "First Citizens",    "tenant_id": "external-firstcitizens"},
        {"company": "ConstructConnect",  "tenant_id": "careers-constructconnect"},
    ],

    # tenant_id = Jobvite company slug  (jobs.jobvite.com/{slug}/jobs)  — DB-verified 2026-04-19
    "jobvite": [
        {"company": "ActionET",          "tenant_id": "actionet"},
        {"company": "ASH Companies",     "tenant_id": "ashcompanies"},
        {"company": "Barracuda Networks","tenant_id": "barracuda-networks-inc"},
        {"company": "Davis Wright Tremaine","tenant_id": "dwt"},
        {"company": "Dominion Enterprises","tenant_id": "dominionenterprises"},
        {"company": "Funko",             "tenant_id": "funko"},
        {"company": "GS1 US",            "tenant_id": "gs1us"},
        {"company": "Hoar Construction", "tenant_id": "hoar"},
        {"company": "Impact Networking", "tenant_id": "impactnetworking"},
        {"company": "LeoVegas",          "tenant_id": "leovegas"},
    ],

    # tenant_id = "{org}|{section}"  — DB-verified 2026-04-19
    "taleo": [
        {"company": "AAR Corp",          "tenant_id": "aarcorp|2"},
        {"company": "Community Health Network", "tenant_id": "chn|chn_ex_staff"},
        {"company": "Fort Bend ISD",     "tenant_id": "aa210|ex"},
        {"company": "Hyundai Capital",   "tenant_id": "hyundaicapital|ex"},
        {"company": "Unifirst",          "tenant_id": "unifirst|unf_external_simp"},
        {"company": "United Health Group","tenant_id": "uhg|10000"},
        {"company": "Woodforest Bank",   "tenant_id": "woodforest|2"},
        {"company": "Zions Bancorp",     "tenant_id": "zionsbancorp|joinexternal"},
        {"company": "McLane Company",    "tenant_id": "aa224|ex"},
        {"company": "Costco",            "tenant_id": "tbe"},
    ],

    # tenant_id = "{COMPANY_CODE}|{jobboard_guid}"  — DB-verified (GUID may need re-discovery)
    "ultipro": [
        {"company": "American Textile",  "tenant_id": "AME1080|6f09a190-4c6b-d891-7f4a-2fa995f11528"},
        {"company": "Arrivia",           "tenant_id": "INT1043EXCUR|ad5e5978-552f-4ef7-90c8-70ebb0a57994"},
        {"company": "Cinch Home Services","tenant_id": "CRO1005CCHS|cea1aee6-14f8-4303-b51d-f23507e998e7"},
        {"company": "Five Guys",         "tenant_id": "FIV1002FGLLC|d8695d89-a769-4e50-b0f9-9131de4202ca"},
        {"company": "Gray Media",        "tenant_id": "GRA1017GRYT|ae441110-89bd-444d-8ad2-b76c7b9db7a9"},
        {"company": "Guild Mortgage",    "tenant_id": "GUI1001|634382fd-6424-ec1b-8e37-2aa4ce4c10de"},
        {"company": "Hotwire Comms",     "tenant_id": "HOT1009HWC|047e3ef0-0c1c-4be3-97ce-617f4fcbc50c"},
        {"company": "GT Independence",   "tenant_id": "GTI1000GUAD|9dfb5104-5c54-40ef-9fef-bdc8060ce73d"},
        {"company": "First Horizon",     "tenant_id": "FIR1007FTN|c005ef3e-175a-49c4-ba29-9f431f673944"},
        {"company": "Finance of America","tenant_id": "fin1006fioa|ea26052b-b8a2-489f-b1dc-3acc6bac391d"},
    ],

    # tenant_id = "{fusionApps_subdomain}|{siteNumber}"  — DB-verified 2026-04-19
    "oracle": [
        {"company": "ACI Worldwide",     "tenant_id": "ebwg.fa.us2|CX"},
        {"company": "ADT",               "tenant_id": "fa-erqb-saasfaprod1.fa.ocs|CX_1"},
        {"company": "BNY Mellon",        "tenant_id": "eofe.fa.us2|CX_1001"},
        {"company": "Confluent",         "tenant_id": "egua.fa.us2|CX_1"},
        {"company": "BDO USA",           "tenant_id": "ebqb.fa.us2|BDOExperiencedCareers"},
        {"company": "CSC",               "tenant_id": "hczw.fa.us2|CX_1001"},
        {"company": "Denso",             "tenant_id": "hcwt.fa.us2|CX"},
        {"company": "Caesars Entertainment", "tenant_id": "edmn.fa.us2|CX_1"},
        {"company": "Community Health Systems", "tenant_id": "fa-evxo-saasfaprod1.fa.ocs|CX_1"},
        {"company": "Associated Wholesale Grocers", "tenant_id": "fa-etwq-saasfaprod1.fa.ocs|CX_1"},
    ],

    # tenant_id = "{slug}|CANDIDATEPORTAL"  — DB-verified 2026-04-19
    "dayforce": [
        {"company": "Atricure",          "tenant_id": "atricure|CANDIDATEPORTAL"},
        {"company": "Coca-Cola FL",      "tenant_id": "cokeflorida|CANDIDATEPORTAL"},
        {"company": "Corpay",            "tenant_id": "corpay|CANDIDATEPORTAL"},
        {"company": "Flow Control Group","tenant_id": "flowcontrol|CANDIDATEPORTAL"},
        {"company": "Hightower Advisors","tenant_id": "hightower|CANDIDATEPORTAL"},
        {"company": "Lynden",            "tenant_id": "lynden|CANDIDATEPORTAL"},
        {"company": "OnPoint CU",        "tenant_id": "onpoint|CANDIDATEPORTAL"},
        {"company": "OnTrac",            "tenant_id": "ontrac|CANDIDATEPORTAL"},
        {"company": "Paradigm",          "tenant_id": "paradigm|CANDIDATEPORTAL"},
        {"company": "Smile Doctors",     "tenant_id": "smiledoctors|CANDIDATEPORTAL"},
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# Stub company object (harvester.fetch_jobs needs company.name)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeCompany:
    def __init__(self, name: str):
        self.name = name


# ─────────────────────────────────────────────────────────────────────────────
# Per-company test
# ─────────────────────────────────────────────────────────────────────────────

def test_company(
    slug: str,
    company_name: str,
    tenant_id: str,
    max_jobs: int,
) -> dict[str, Any]:
    """
    Fetch up to max_jobs for one company/tenant and return a structured result dict.
    """
    result: dict[str, Any] = {
        "company":       company_name,
        "tenant_id":     tenant_id,
        "status":        "pending",
        "jobs_fetched":  0,
        "jobs_with_jd":  0,
        "jobs_no_jd":    0,
        "jd_pct":        0.0,
        "sample_job":    None,
        "error":         None,
        "error_type":    None,
        "traceback":     None,
        "duration_s":    0.0,
        "warnings":      [],
    }

    harvester = get_harvester(slug)
    if harvester is None:
        result["status"] = "ERROR"
        result["error"]  = f"No harvester registered for slug '{slug}'"
        result["error_type"] = "MISSING_HARVESTER"
        return result

    t0 = time.perf_counter()
    try:
        jobs = harvester.fetch_jobs(
            _FakeCompany(company_name),
            tenant_id,
            fetch_all=False,   # incremental — only recent jobs
            since_hours=24 * 365,  # last year — broad window for smoke test
        )
        result["duration_s"] = round(time.perf_counter() - t0, 2)

        # Cap at max_jobs for analysis
        jobs_sample = jobs[:max_jobs]
        result["jobs_fetched"] = len(jobs_sample)
        result["total_available"] = getattr(harvester, "last_total_available", 0) or len(jobs)

        if not jobs_sample:
            result["status"]   = "EMPTY"
            result["warnings"].append("No jobs returned — tenant may be inactive or tenant_id is wrong")
            return result

        # Analyse job descriptions
        for j in jobs_sample:
            desc = (j.get("description") or "").strip()
            if len(desc) > 50:
                result["jobs_with_jd"] += 1
            else:
                result["jobs_no_jd"] += 1

        result["jd_pct"] = round(
            100 * result["jobs_with_jd"] / len(jobs_sample), 1
        )

        # Capture a sample job (sanitised)
        first = jobs_sample[0]
        result["sample_job"] = {
            "title":          (first.get("title") or "")[:80],
            "location_raw":   (first.get("location_raw") or "")[:80],
            "employment_type":first.get("employment_type") or "",
            "experience_level":first.get("experience_level") or "",
            "description_len":len(first.get("description") or ""),
            "has_salary":     bool(first.get("salary_min") or first.get("salary_raw")),
            "has_requirements":bool(first.get("requirements")),
            "original_url":   (first.get("original_url") or "")[:120],
        }

        # Warnings
        if result["jd_pct"] < 50:
            result["warnings"].append(
                f"Only {result['jd_pct']}% of jobs have a description (threshold: 50%)"
            )
        if not any(j.get("title") for j in jobs_sample):
            result["warnings"].append("No job titles found — parsing may be broken")
        if all((j.get("employment_type") or "UNKNOWN") == "UNKNOWN" for j in jobs_sample):
            result["warnings"].append("employment_type is UNKNOWN for all jobs")

        result["status"] = "OK" if not result["warnings"] else "WARN"

    except Exception as exc:
        result["duration_s"]  = round(time.perf_counter() - t0, 2)
        result["status"]      = "ERROR"
        result["error"]       = str(exc)
        result["error_type"]  = type(exc).__name__
        result["traceback"]   = traceback.format_exc()

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Report builders
# ─────────────────────────────────────────────────────────────────────────────

STATUS_EMOJI = {"OK": "✅", "WARN": "⚠️", "ERROR": "❌", "EMPTY": "🔘", "pending": "⏳"}

def _pct_bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled) + f" {pct:.0f}%"


def build_markdown_report(
    all_results: dict[str, list[dict]],
    run_ts: str,
    max_jobs: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# GoCareers Harvest — Platform Smoke Test Report")
    lines.append(f"**Generated:** {run_ts}  |  **Jobs per company:** {max_jobs}")
    lines.append("")

    # ── Executive summary table ───────────────────────────────────────────────
    lines.append("## Executive Summary")
    lines.append("")
    lines.append("| Platform | Companies | ✅ OK | ⚠️ WARN | ❌ ERROR | 🔘 EMPTY | Avg JD% | Total Jobs |")
    lines.append("|---|---|---|---|---|---|---|---|")

    total_ok = total_warn = total_err = total_empty = 0

    for slug, results in all_results.items():
        ok_n   = sum(1 for r in results if r["status"] == "OK")
        warn_n = sum(1 for r in results if r["status"] == "WARN")
        err_n  = sum(1 for r in results if r["status"] == "ERROR")
        emp_n  = sum(1 for r in results if r["status"] == "EMPTY")
        jd_avg = (
            sum(r["jd_pct"] for r in results if r["status"] not in ("ERROR","EMPTY","pending"))
            / max(1, len([r for r in results if r["status"] not in ("ERROR","EMPTY","pending")]))
        )
        total_jobs = sum(r.get("jobs_fetched", 0) for r in results)
        lines.append(
            f"| **{slug}** | {len(results)} | {ok_n} | {warn_n} | {err_n} | {emp_n} "
            f"| {jd_avg:.0f}% | {total_jobs:,} |"
        )
        total_ok   += ok_n
        total_warn += warn_n
        total_err  += err_n
        total_empty += emp_n

    lines.append("")
    total_co = sum(len(v) for v in all_results.values())
    lines.append(
        f"> **Total:** {total_co} companies tested — "
        f"✅ {total_ok} OK · ⚠️ {total_warn} WARN · ❌ {total_err} ERROR · 🔘 {total_empty} EMPTY"
    )
    lines.append("")

    # ── Per-platform detail ───────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Per-Platform Detail")
    lines.append("")

    for slug, results in all_results.items():
        lines.append(f"### `{slug}`")
        lines.append("")

        for r in results:
            emoji = STATUS_EMOJI.get(r["status"], "❓")
            lines.append(f"#### {emoji} {r['company']} (`{r['tenant_id']}`)")
            lines.append("")
            lines.append(f"- **Status:** `{r['status']}`  **Duration:** {r['duration_s']}s")
            lines.append(f"- **Jobs fetched (sample):** {r['jobs_fetched']}  |  **Total available:** {r.get('total_available', '?')}")

            if r["status"] not in ("ERROR", "EMPTY"):
                bar = _pct_bar(r["jd_pct"])
                lines.append(f"- **JD coverage:** `{bar}`  ({r['jobs_with_jd']} with JD, {r['jobs_no_jd']} without)")

            if r["warnings"]:
                lines.append("- **Warnings:**")
                for w in r["warnings"]:
                    lines.append(f"  - ⚠️ {w}")

            if r["error"]:
                lines.append(f"- **Error:** `{r['error_type']}` — {r['error']}")
                if r["traceback"]:
                    lines.append(f"  <details><summary>Full traceback</summary>")
                    lines.append("")
                    lines.append("  ```")
                    lines.append(r["traceback"].rstrip())
                    lines.append("  ```")
                    lines.append("  </details>")

            if r.get("sample_job"):
                sj = r["sample_job"]
                lines.append(f"- **Sample job:**")
                lines.append(f"  - Title: `{sj['title']}`")
                lines.append(f"  - Location: `{sj['location_raw']}`")
                lines.append(f"  - Employment type: `{sj['employment_type']}`")
                lines.append(f"  - Experience level: `{sj['experience_level']}`")
                lines.append(f"  - Description length: **{sj['description_len']} chars**")
                lines.append(f"  - Has salary: {'✅' if sj['has_salary'] else '❌'}")
                lines.append(f"  - Has requirements: {'✅' if sj['has_requirements'] else '❌'}")
                lines.append(f"  - URL: `{sj['original_url']}`")

            lines.append("")

    # ── Issues section (for LLM) ──────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Issues Requiring Attention")
    lines.append("")
    lines.append(
        "> This section lists every ERROR and WARN, structured so an LLM can "
        "diagnose and fix them immediately.\n"
    )

    has_issues = False
    for slug, results in all_results.items():
        problems = [r for r in results if r["status"] in ("ERROR", "WARN", "EMPTY")]
        if not problems:
            continue
        has_issues = True
        lines.append(f"### Platform: `{slug}`")
        lines.append("")
        for r in problems:
            emoji = STATUS_EMOJI.get(r["status"], "❓")
            lines.append(f"#### {emoji} {r['company']} — `{r['status']}`")
            lines.append("")
            if r["error"]:
                lines.append(f"**Error type:** `{r['error_type']}`")
                lines.append(f"**Error message:**")
                lines.append(f"```")
                lines.append(r["error"])
                lines.append(f"```")
                if r["traceback"]:
                    lines.append(f"**Full traceback:**")
                    lines.append("```python")
                    lines.append(r["traceback"].rstrip())
                    lines.append("```")
            if r["warnings"]:
                lines.append("**Warnings:**")
                for w in r["warnings"]:
                    lines.append(f"- {w}")
            lines.append("")
            lines.append("**Context for LLM:**")
            lines.append(f"- File: `apps/harvest/harvesters/{slug}.py`")
            lines.append(f"- Harvester class: `{slug.title().replace('_','')}Harvester` (or similar)")
            lines.append(f"- Tenant ID used: `{r['tenant_id']}`")
            lines.append(f"- The company has a public job board at the expected URL")
            lines.append(f"- If the error is an HTTP error, check the platform's public API docs")
            lines.append(f"- If EMPTY: the tenant_id may be wrong or the company has no active jobs")
            lines.append(f"- If description missing: check `_normalize()` maps `description` field correctly")
            lines.append("")

    if not has_issues:
        lines.append("🎉 **No issues found — all platforms OK!**")
        lines.append("")

    # ── Recommendations ───────────────────────────────────────────────────────
    lines.append("---")
    lines.append("")
    lines.append("## Recommendations")
    lines.append("")

    recs = []
    for slug, results in all_results.items():
        err_count  = sum(1 for r in results if r["status"] == "ERROR")
        warn_count = sum(1 for r in results if r["status"] == "WARN")
        emp_count  = sum(1 for r in results if r["status"] == "EMPTY")
        total_jd_pct = (
            sum(r["jd_pct"] for r in results if r["status"] not in ("ERROR","EMPTY"))
            / max(1, len([r for r in results if r["status"] not in ("ERROR","EMPTY")]))
        )
        if err_count > len(results) // 2:
            recs.append(f"**CRITICAL** `{slug}`: {err_count}/{len(results)} companies errored — the harvester is likely broken. Check `apps/harvest/harvesters/{slug}.py`.")
        elif err_count > 0:
            recs.append(f"**HIGH** `{slug}`: {err_count} errors — inspect traceback above and fix error handling.")
        if emp_count > len(results) // 2:
            recs.append(f"**MEDIUM** `{slug}`: {emp_count}/{len(results)} returned empty — tenant IDs in smoke_test.py may be outdated; update them.")
        if total_jd_pct < 30:
            recs.append(f"**HIGH** `{slug}`: JD coverage only {total_jd_pct:.0f}% — job descriptions are not being fetched. Add per-job detail API call in `_normalize()`.")
        elif total_jd_pct < 70:
            recs.append(f"**MEDIUM** `{slug}`: JD coverage {total_jd_pct:.0f}% — some jobs missing descriptions. Investigate which jobs lack them and why.")

    if recs:
        for r in recs:
            lines.append(f"- {r}")
    else:
        lines.append("- ✅ No critical recommendations. System is healthy.")
    lines.append("")
    lines.append("---")
    lines.append(f"*Report generated by `scripts/platform_smoke_test.py` at {run_ts}*")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GoCareers platform smoke test")
    parser.add_argument("--platform", default=None,
                        help="Test only this platform (default: all)")
    parser.add_argument("--max-jobs", type=int, default=5,
                        help="Max jobs to analyse per company (default: 5)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds between companies (default: 1.5)")
    args = parser.parse_args()

    run_ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)

    platforms_to_test = (
        {args.platform: PLATFORM_TENANTS[args.platform]}
        if args.platform and args.platform in PLATFORM_TENANTS
        else PLATFORM_TENANTS
    )

    all_results: dict[str, list[dict]] = {}

    total_companies = sum(len(v) for v in platforms_to_test.values())
    done = 0

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║   GoCareers — Platform Smoke Test                    ║{RESET}")
    print(f"{BOLD}{CYAN}║   Platforms: {len(platforms_to_test):2d}   Companies: {total_companies:3d}   Jobs/co: {args.max_jobs}  ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════╝{RESET}\n")

    for slug, tenants in platforms_to_test.items():
        print(f"\n{BOLD}▶ {slug.upper()}{RESET}  ({len(tenants)} companies)")
        results: list[dict] = []

        for idx, tenant in enumerate(tenants, 1):
            company_name = tenant["company"]
            tenant_id    = tenant["tenant_id"]
            print(f"  [{idx:2d}/{len(tenants)}] {company_name[:40]:40s} ", end="", flush=True)

            r = test_company(slug, company_name, tenant_id, args.max_jobs)
            results.append(r)
            done += 1

            # Print one-line result
            if r["status"] == "OK":
                ok(f"{r['jobs_fetched']} jobs | JD: {r['jd_pct']:.0f}% | {r['duration_s']}s")
            elif r["status"] == "WARN":
                warn(f"{r['jobs_fetched']} jobs | JD: {r['jd_pct']:.0f}% | {r['duration_s']}s — {r['warnings'][0][:60]}")
            elif r["status"] == "EMPTY":
                info(f"0 jobs | {r['duration_s']}s — {(r['warnings'][0] if r['warnings'] else 'empty')[:60]}")
            else:
                err(f"{r['error_type']}: {(r['error'] or '')[:70]}")

            if idx < len(tenants):
                time.sleep(args.delay)

        all_results[slug] = results

    # ── Write logs ────────────────────────────────────────────────────────────
    json_path = logs_dir / f"smoke_test_{run_ts}.json"
    md_path   = logs_dir / f"smoke_test_{run_ts}.md"

    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    md_report = build_markdown_report(all_results, run_ts, args.max_jobs)
    with open(md_path, "w") as f:
        f.write(md_report)

    # ── Final summary ─────────────────────────────────────────────────────────
    total_ok   = sum(sum(1 for r in v if r["status"] == "OK")    for v in all_results.values())
    total_warn = sum(sum(1 for r in v if r["status"] == "WARN")  for v in all_results.values())
    total_err  = sum(sum(1 for r in v if r["status"] == "ERROR") for v in all_results.values())
    total_emp  = sum(sum(1 for r in v if r["status"] == "EMPTY") for v in all_results.values())

    print(f"\n{BOLD}{'═'*56}{RESET}")
    print(f"{BOLD}SMOKE TEST COMPLETE{RESET}")
    print(f"  ✅ OK    : {total_ok:3d}")
    print(f"  ⚠️  WARN  : {total_warn:3d}")
    print(f"  ❌ ERROR : {total_err:3d}")
    print(f"  🔘 EMPTY : {total_emp:3d}")
    print(f"{'═'*56}")
    print(f"  JSON report → {json_path}")
    print(f"  MD  report  → {md_path}  ← paste to any LLM to debug\n")


if __name__ == "__main__":
    main()

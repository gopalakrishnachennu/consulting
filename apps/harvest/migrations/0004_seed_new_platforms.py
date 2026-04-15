"""Add 6 new platforms discovered from real job URL analysis + fix Greenhouse URL pattern."""
from django.db import migrations

NEW_PLATFORMS = [
    {
        "name": "Workable",
        "slug": "workable",
        "url_patterns": ["apply.workable.com", "workable.com/j/"],
        "api_type": "html_scrape",
        "color_hex": "#7B61FF",
        "rate_limit_per_min": 10,
        "notes": "apply.workable.com/{tenant}/j/{id}",
    },
    {
        "name": "BambooHR",
        "slug": "bamboohr",
        "url_patterns": ["bamboohr.com"],
        "api_type": "html_scrape",
        "color_hex": "#73AC42",
        "rate_limit_per_min": 5,
        "notes": "{tenant}.bamboohr.com/careers",
    },
    {
        "name": "SmartRecruiters",
        "slug": "smartrecruiters",
        "url_patterns": ["jobs.smartrecruiters.com", "smartrecruiters.com/jobs"],
        "api_type": "html_scrape",
        "color_hex": "#0A3D62",
        "rate_limit_per_min": 10,
        "notes": "jobs.smartrecruiters.com/{Tenant}/...",
    },
    {
        "name": "Dayforce HCM",
        "slug": "dayforce",
        "url_patterns": ["jobs.dayforcehcm.com", "dayforcehcm.com"],
        "api_type": "html_scrape",
        "color_hex": "#00A3A1",
        "rate_limit_per_min": 5,
        "notes": "Formerly Ceridian Dayforce — jobs.dayforcehcm.com/en-US/{tenant}/CANDIDATEPORTAL",
    },
    {
        "name": "ADP",
        "slug": "adp",
        "url_patterns": ["workforcenow.adp.com", "myjobs.adp.com"],
        "api_type": "html_scrape",
        "color_hex": "#CC0000",
        "rate_limit_per_min": 5,
        "notes": "ADP Workforce Now & myjobs portal",
    },
    {
        "name": "Oracle HCM",
        "slug": "oracle",
        "url_patterns": ["oraclecloud.com/hcmUI", "fa.ocs.oraclecloud.com", "fa.us2.oraclecloud.com"],
        "api_type": "html_scrape",
        "color_hex": "#F80000",
        "rate_limit_per_min": 5,
        "notes": "Oracle Cloud HCM — various fa-*.oraclecloud.com subdomains",
    },
]

# Also update Greenhouse url_patterns to include job-boards.greenhouse.io
GREENHOUSE_EXTRA_PATTERN = "job-boards.greenhouse.io"


def seed(apps, schema_editor):
    JobBoardPlatform = apps.get_model("harvest", "JobBoardPlatform")
    for p in NEW_PLATFORMS:
        JobBoardPlatform.objects.get_or_create(slug=p["slug"], defaults=p)

    # Add job-boards.greenhouse.io to Greenhouse if missing
    gh = JobBoardPlatform.objects.filter(slug="greenhouse").first()
    if gh and GREENHOUSE_EXTRA_PATTERN not in (gh.url_patterns or []):
        gh.url_patterns = list(gh.url_patterns or []) + [GREENHOUSE_EXTRA_PATTERN]
        gh.save(update_fields=["url_patterns"])


def unseed(apps, schema_editor):
    JobBoardPlatform = apps.get_model("harvest", "JobBoardPlatform")
    JobBoardPlatform.objects.filter(slug__in=[p["slug"] for p in NEW_PLATFORMS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("harvest", "0003_harvestrun_detection_and_run_type"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

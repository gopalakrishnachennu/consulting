from django.db import migrations

PLATFORMS = [
    {"name": "Workday", "slug": "workday", "url_patterns": ["myworkdayjobs.com", "wd1.myworkday.com", "wd3.myworkday.com", "wd5.myworkday.com"], "api_type": "workday_api", "color_hex": "#0A66C2", "rate_limit_per_min": 10, "notes": "Largest enterprise ATS. Uses REST API with tenant subdomain."},
    {"name": "Greenhouse", "slug": "greenhouse", "url_patterns": ["boards.greenhouse.io", "greenhouse.io/jobs"], "api_type": "greenhouse_api", "color_hex": "#3AB44A", "rate_limit_per_min": 20, "notes": "Public JSON API at boards-api.greenhouse.io/v1/boards/{token}/jobs"},
    {"name": "Lever", "slug": "lever", "url_patterns": ["jobs.lever.co", "lever.co"], "api_type": "lever_api", "color_hex": "#1C1C1C", "rate_limit_per_min": 20, "notes": "Public REST API at api.lever.co/v0/postings/{company}"},
    {"name": "Ashby", "slug": "ashby", "url_patterns": ["ashbyhq.com", "jobs.ashbyhq.com"], "api_type": "ashby_graphql", "color_hex": "#7C3AED", "rate_limit_per_min": 15, "notes": "GraphQL API at jobs.ashbyhq.com/api/non-user-graphql"},
    {"name": "Jobvite", "slug": "jobvite", "url_patterns": ["jobs.jobvite.com", "jobvite.com"], "api_type": "html_scrape", "color_hex": "#E8473F", "rate_limit_per_min": 5},
    {"name": "iCIMS", "slug": "icims", "url_patterns": ["icims.com"], "api_type": "html_scrape", "color_hex": "#00A3E0", "rate_limit_per_min": 5},
    {"name": "Recruitee", "slug": "recruitee", "url_patterns": ["recruitee.com"], "api_type": "html_scrape", "color_hex": "#F7931E", "rate_limit_per_min": 10},
    {"name": "Taleo", "slug": "taleo", "url_patterns": ["taleo.net"], "api_type": "html_scrape", "color_hex": "#CC0000", "rate_limit_per_min": 5, "notes": "Oracle Taleo - slow HTML scrape"},
    {"name": "Zoho Recruit", "slug": "zoho", "url_patterns": ["zoho.com/recruit", "jobs.zoho.com"], "api_type": "html_scrape", "color_hex": "#E42527", "rate_limit_per_min": 5},
    {"name": "UltiPro / UKG", "slug": "ultipro", "url_patterns": ["ultipro.com", "ukg.com", "recruiting.ukg.net"], "api_type": "html_scrape", "color_hex": "#004F98", "rate_limit_per_min": 5},
    {"name": "ApplicantPro", "slug": "applicantpro", "url_patterns": ["applicantpro.com"], "api_type": "html_scrape", "color_hex": "#2E86AB", "rate_limit_per_min": 5},
    {"name": "ApplyToJob", "slug": "applytojob", "url_patterns": ["applytojob.com"], "api_type": "html_scrape", "color_hex": "#F18F01", "rate_limit_per_min": 5},
    {"name": "The Applicant Manager", "slug": "theapplicantmanager", "url_patterns": ["theapplicantmanager.com"], "api_type": "html_scrape", "color_hex": "#6B7280", "rate_limit_per_min": 5},
]


def seed_platforms(apps, schema_editor):
    JobBoardPlatform = apps.get_model("harvest", "JobBoardPlatform")
    for p in PLATFORMS:
        JobBoardPlatform.objects.get_or_create(slug=p["slug"], defaults=p)


def unseed_platforms(apps, schema_editor):
    JobBoardPlatform = apps.get_model("harvest", "JobBoardPlatform")
    JobBoardPlatform.objects.filter(slug__in=[p["slug"] for p in PLATFORMS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("harvest", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_platforms, unseed_platforms),
    ]

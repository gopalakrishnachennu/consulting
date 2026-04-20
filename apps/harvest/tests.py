"""Fast checks for career URLs, tenant extraction, harvester wiring, and smoke command."""

from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from apps.harvest.career_url import build_career_url
from apps.harvest.detectors import extract_tenant
from apps.harvest.harvesters import (
    TeamtailorHarvester,
    ZohoHarvester,
    get_harvester,
)
from apps.harvest.jarvis import JobJarvis
from apps.harvest.platform_engine import ImplementationKind, dedicated_slugs, kind_for_slug


class HarvestUrlAndRegistryTests(SimpleTestCase):
    def test_build_career_url_zoho(self):
        self.assertEqual(
            build_career_url("zoho", "acme"),
            "https://jobs.zoho.com/portal/acme/careers",
        )
        self.assertEqual(
            build_career_url("zoho", "acme.zohorecruit.com"),
            "https://acme.zohorecruit.com/jobs/Careers",
        )

    def test_extract_tenant_subdomain_hosts(self):
        self.assertEqual(
            extract_tenant("teamtailor", "https://widgets.teamtailor.com/jobs"),
            "widgets",
        )
        self.assertEqual(
            extract_tenant("breezy", "https://foo.breezy.hr/p/1"),
            "foo",
        )
        self.assertEqual(
            extract_tenant("zoho", "https://jobs.zoho.com/portal/acme/careers"),
            "acme",
        )
        self.assertEqual(
            extract_tenant("zoho", "https://acme.zohorecruit.com/jobs/Careers"),
            "acme",
        )

    def test_get_harvester_and_platform_kind(self):
        self.assertIsInstance(get_harvester("zoho"), ZohoHarvester)
        self.assertIsInstance(get_harvester("teamtailor"), TeamtailorHarvester)
        self.assertEqual(kind_for_slug("zoho"), ImplementationKind.DEDICATED)
        self.assertEqual(kind_for_slug("teamtailor"), ImplementationKind.DEDICATED)
        self.assertIn("zoho", dedicated_slugs())
        self.assertIn("teamtailor", dedicated_slugs())


class SmokeTestHarvestCommandTests(TestCase):
    """Dry-run must not require network or Celery."""

    def test_smoke_test_harvest_dry_run_exits_zero(self):
        out = StringIO()
        err = StringIO()
        try:
            call_command("smoke_test_harvest", "--dry-run", stdout=out, stderr=err)
        except SystemExit as e:
            self.fail(f"smoke_test_harvest --dry-run raised SystemExit({e.code})")
        self.assertIn("Dry run finished", out.getvalue())


class JarvisPlatformApiExtractionTests(SimpleTestCase):
    """Verify Jarvis _platform_api paths populate description (backfill relies on this)."""

    def test_workday_cxs_payload_maps_job_description(self):
        jarvis = JobJarvis()
        wd_url = (
            "https://acme.wd1.myworkdayjobs.com/en-US/Search/job/"
            "Remote-Engineer_R_99999"
        )
        fake_job = {
            "title": "Remote Engineer",
            "externalPath": "/job/Remote-Engineer_R_99999",
            "locationsText": "Remote",
            "bulletFields": ["R_99999"],
            "jobDescription": {"content": "<p>Workday JD body</p>"},
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"jobPostings": [fake_job]}
        with patch.object(jarvis._session, "post", return_value=mock_resp):
            out = jarvis._workday(wd_url)
        self.assertIsNotNone(out)
        self.assertIn("Workday JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Remote Engineer")

    def test_smartrecruiters_detail_maps_sections(self):
        jarvis = JobJarvis()
        url = "https://jobs.smartrecruiters.com/DemoCo/111222333"
        detail = {
            "name": "QA Role",
            "ref": url,
            "location": {"city": "Austin", "region": "TX", "country": "US"},
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": "<p>SR JD</p>"},
                    "qualifications": {"text": "<p>Reqs</p>"},
                }
            },
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = detail
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._smartrecruiters(url)
        self.assertIsNotNone(out)
        self.assertIn("SR JD", out.get("description", ""))
        self.assertIn("Reqs", out.get("requirements", ""))

    def test_recruitee_offers_list_matches_slug(self):
        jarvis = JobJarvis()
        url = "https://widgets.recruitee.com/o/backend-engineer"
        offers = {
            "offers": [
                {
                    "id": 42,
                    "slug": "backend-engineer",
                    "title": "Backend Engineer",
                    "description": "<p>Recruitee JD</p>",
                    "requirements": "",
                    "city": "Berlin",
                    "country": "DE",
                    "careers_url": url,
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = offers
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._recruitee(url)
        self.assertIsNotNone(out)
        self.assertIn("Recruitee JD", out.get("description", ""))

    def test_bamboohr_detail_json_maps_description(self):
        jarvis = JobJarvis()
        url = "https://acme.bamboohr.com/careers/12345"
        payload = {
            "result": {
                "jobOpening": {
                    "description": "<p>Bamboo JD</p>",
                    "jobTitle": "Analyst",
                    "location": {"city": "NYC", "state": "NY", "addressCountry": "US"},
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = payload
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._bamboohr(url)
        self.assertIsNotNone(out)
        self.assertIn("Bamboo JD", out.get("description", ""))
        self.assertEqual(out.get("title"), "Analyst")

    def test_icims_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://careers-acme.icims.com/jobs/12345/job"
        html = (
            '<html><body>'
            '<h1 class="iCIMS_JobTitle">Software Engineer</h1>'
            '<div class="iCIMS_JobContent"><p>iCIMS JD body that is long enough to pass minimum threshold of 72 chars easily here.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._icims(url)
        self.assertIsNotNone(out)
        self.assertIn("iCIMS JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Software Engineer")

    def test_jobvite_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://jobs.jobvite.com/acmecorp/job/oABC123"
        html = (
            '<html><body>'
            '<h2 class="jv-header">QA Lead</h2>'
            '<div class="jv-job-detail-description"><p>Jobvite JD body here with plenty of text to pass the minimum character threshold easily.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._jobvite(url)
        self.assertIsNotNone(out)
        self.assertIn("Jobvite JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "QA Lead")

    def test_taleo_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://aa224.taleo.net/careersection/ex/jobdetail.ftl?job=12345&lang=en"
        html = (
            '<html><body>'
            '<h1 id="requisitionDescriptionInterface.reqTitleLinkAction.row1">PM Role</h1>'
            '<div id="requisitionDescriptionInterface.ID1702.row1"><p>Taleo JD body with enough content to pass the seventy two character minimum threshold.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._taleo(url)
        self.assertIsNotNone(out)
        self.assertIn("Taleo JD body", out.get("description", ""))

    def test_oracle_ce_rest_api(self):
        jarvis = JobJarvis()
        url = "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/300001"
        api_resp = {
            "items": [{
                "Title": "Oracle Dev",
                "ExternalDescriptionStr": "<p>Oracle JD body</p>",
                "PrimaryLocation": "Redwood City, CA",
                "Organization": "Engineering",
            }]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = api_resp
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._oracle(url)
        self.assertIsNotNone(out)
        self.assertIn("Oracle JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Oracle Dev")

    def test_dayforce_job_detail_api(self):
        jarvis = JobJarvis()
        url = "https://jobs.dayforcehcm.com/en-US/corpay/CANDIDATEPORTAL/jobs/12345"
        api_resp = {
            "JobTitle": "Payroll Analyst",
            "Description": "<p>Dayforce JD body</p>",
            "JobLocation": "Tampa, FL",
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = api_resp
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._dayforce(url)
        self.assertIsNotNone(out)
        self.assertIn("Dayforce JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Payroll Analyst")

    def test_breezy_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://acme.breezy.hr/p/abc123-software-dev"
        html = (
            '<html><body>'
            '<h1>Software Dev</h1>'
            '<div class="description"><p>Breezy JD body with enough words to clear the seventy two character minimum check easily.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._breezy(url)
        self.assertIsNotNone(out)
        self.assertIn("Breezy JD body", out.get("description", ""))

    def test_teamtailor_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://career.teamtailor.com/jobs/12345-qa-engineer"
        html = (
            '<html><body>'
            '<h1>QA Engineer</h1>'
            '<div class="job-description"><p>Teamtailor JD body with plenty of characters to easily clear the minimum threshold.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._teamtailor(url)
        self.assertIsNotNone(out)
        self.assertIn("Teamtailor JD body", out.get("description", ""))

    def test_zoho_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://jobs.zoho.com/portal/acme/apply/123"
        html = (
            '<html><body>'
            '<h1>Data Analyst</h1>'
            '<div class="job-description"><p>Zoho JD body with sufficient text content to pass the seventy-two character minimum check.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._zoho(url)
        self.assertIsNotNone(out)
        self.assertIn("Zoho JD body", out.get("description", ""))

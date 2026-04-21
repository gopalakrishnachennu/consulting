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

    def test_workday_detail_api_maps_job_description(self):
        jarvis = JobJarvis()
        wd_url = (
            "https://acme.wd1.myworkdayjobs.com/en-US/Search/job/"
            "Remote-Engineer_R_99999"
        )
        detail_resp = {
            "jobPostingInfo": {
                "title": "Remote Engineer",
                "location": "Remote",
                "externalJobId": "R_99999",
                "jobDescription": "<p>Workday JD body</p>",
            },
        }
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = detail_resp
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._workday(wd_url)
        self.assertIsNotNone(out)
        self.assertIn("Workday JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Remote Engineer")

    def test_workday_search_fallback_when_detail_404s(self):
        jarvis = JobJarvis()
        wd_url = (
            "https://3m.wd1.myworkdayjobs.com/Search/job/"
            "US-MN/Engineer_R01049764"
        )
        # Detail returns 404
        detail_404 = MagicMock()
        detail_404.ok = False
        detail_404.status_code = 404

        # Search returns a result with description in search data
        search_resp = MagicMock()
        search_resp.ok = True
        search_resp.json.return_value = {
            "jobPostings": [{
                "title": "Manufacturing Engineer",
                "externalPath": "/job/US-MN/Engineer_R01049764",
                "locationsText": "Maplewood, MN",
                "bulletFields": ["R01049764"],
                "jobDescription": {"content": "<p>Workday search JD</p>"},
            }]
        }

        # Detail for correct path returns full JD
        detail_ok = MagicMock()
        detail_ok.ok = True
        detail_ok.json.return_value = {
            "jobPostingInfo": {
                "title": "Manufacturing Engineer",
                "location": "Maplewood, MN",
                "jobDescription": "<p>Full Workday JD from detail</p>",
            }
        }

        call_count = {"n": 0}
        def mock_get(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return detail_404  # first detail call fails
            return detail_ok  # second detail call succeeds

        with patch.object(jarvis._session, "get", side_effect=mock_get):
            with patch.object(jarvis._session, "post", return_value=search_resp):
                out = jarvis._workday(wd_url)
        self.assertIsNotNone(out)
        self.assertIn("Full Workday JD", out.get("description", ""))

    def test_smartrecruiters_normalize_posting_id_strips_seo_slug(self):
        from apps.harvest.jarvis import _smartrecruiters_normalize_posting_id

        self.assertEqual(
            _smartrecruiters_normalize_posting_id(
                "744000121421842-mgr-strategic-rebids-930951-",
            ),
            "744000121421842",
        )
        self.assertEqual(
            _smartrecruiters_normalize_posting_id("111222333"),
            "111222333",
        )

    def test_smartrecruiters_accepts_rest_api_url(self):
        """Apply links sometimes store api.smartrecruiters.com/v1/companies/.../postings/id."""
        jarvis = JobJarvis()
        url = "https://api.smartrecruiters.com/v1/companies/WesternDigital/postings/744000112340137"
        captured = {}

        def fake_get(u, **kwargs):
            captured["u"] = u
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "id": "744000112340137",
                "name": "Engineer",
                "ref": "https://jobs.smartrecruiters.com/WesternDigital/744000112340137",
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>API body</p>"},
                    }
                },
            }
            return resp

        with patch.object(jarvis, "_http_get", side_effect=fake_get):
            out = jarvis._smartrecruiters(url)
        self.assertIsNotNone(out)
        self.assertIn("WesternDigital", captured.get("u", ""))
        self.assertIn("/postings/744000112340137", captured.get("u", ""))
        self.assertIn("API body", out.get("description", ""))

    def test_smartrecruiters_api_request_strips_seo_slug_from_url(self):
        """Detail API must receive numeric id only, not ``744...-title-slug``."""
        from apps.harvest.jarvis import _smartrecruiters_normalize_posting_id

        jarvis = JobJarvis()
        url = "https://jobs.smartrecruiters.com/DemoCo/744000121421842-mgr-title-"
        captured = {}

        def fake_get(u, **kwargs):
            captured["detail_url"] = u
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "id": "744000121421842",
                "name": "Role",
                "ref": url,
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>Body</p>"},
                    }
                },
            }
            return resp

        with patch.object(jarvis, "_http_get", side_effect=fake_get):
            out = jarvis._smartrecruiters(url)
        self.assertIsNotNone(out)
        self.assertIn("/postings/744000121421842", captured.get("detail_url", ""))
        self.assertNotIn("mgr-title", captured.get("detail_url", ""))
        self.assertIn("Body", out.get("description", ""))
        self.assertEqual(
            _smartrecruiters_normalize_posting_id("744000121421842-mgr-title-"),
            "744000121421842",
        )

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
        with patch.object(jarvis, "_http_get", return_value=mock_resp):
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

    def test_ultipro_embedded_json_in_html(self):
        jarvis = JobJarvis()
        url = (
            "https://recruiting.ultipro.com/INT1043EXCUR/JobBoard/"
            "ad5e5978-552f-4ef7-90c8-70ebb0a57994/OpportunityDetail"
            "?opportunityId=c19385b5-7296-4f1d-88d8-3cbf7507693f"
        )
        html = (
            "<html><script>\n"
            'var opportunity = new US.Opportunity.CandidateOpportunityDetail('
            '{"Title":"Finance Intern",'
            '"Description":"<p>UKG UltiPro full JD body with enough text for tests.</p>",'
            '"Locations":[{"LocalizedDescription":"Scottsdale HQ",'
            '"Address":{"City":"Scottsdale","State":{"Code":"AZ"},"Country":{"Code":"USA"}}}]}'
            ");\n</script></html>"
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        mock_resp.url = url
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._ultipro(url)
        self.assertIsNotNone(out)
        self.assertIn("UKG UltiPro full JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Finance Intern")
        self.assertIn("Scottsdale", out.get("location_raw", ""))

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
        with patch.object(jarvis._session, "get", return_value=mock_resp) as mock_get:
            out = jarvis._oracle(url)
        self.assertIsNotNone(out)
        self.assertIn("Oracle JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Oracle Dev")
        finder = mock_get.call_args.kwargs.get("params", {}).get("finder", "")
        self.assertIn("ById;", finder)
        self.assertNotIn("findReqDetails", finder)

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


class JarvisFetchGateTests(SimpleTestCase):
    """JarvisFetchGate: retries and concurrency wrapper for outbound HTTP."""

    def test_retries_502_then_success(self):
        from unittest.mock import MagicMock, patch

        from apps.harvest.http_limits import JarvisFetchGate

        gate = JarvisFetchGate(50, 10, 3, 0.01)
        session = MagicMock()
        bad = MagicMock()
        bad.status_code = 502
        good = MagicMock()
        good.status_code = 200
        session.get.side_effect = [bad, good]
        with patch("apps.harvest.http_limits.time.sleep"):
            r = gate.request(session, "GET", "https://example.com/job/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(session.get.call_count, 2)

    def test_no_retry_on_404(self):
        from unittest.mock import MagicMock, patch

        from apps.harvest.http_limits import JarvisFetchGate

        gate = JarvisFetchGate(50, 10, 3, 0.01)
        session = MagicMock()
        nf = MagicMock()
        nf.status_code = 404
        session.get.return_value = nf
        with patch("apps.harvest.http_limits.time.sleep"):
            r = gate.request(session, "GET", "https://example.com/missing")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(session.get.call_count, 1)


class SmartRecruitersSupportTests(SimpleTestCase):
    """Canonical API URLs from list payload — avoids case-sensitive slug mismatches."""

    def test_backfill_fetch_url_uses_company_identifier_from_payload(self):
        from types import SimpleNamespace

        from harvest.smartrecruiters_support import backfill_fetch_url_for_raw_job

        job = SimpleNamespace(
            original_url="https://jobs.smartrecruiters.com/wrongslug/744000112340137",
            external_id="744000112340137",
            raw_payload={
                "company": {"identifier": "WesternDigital", "name": "WD"},
                "id": "744000112340137",
            },
        )
        u = backfill_fetch_url_for_raw_job(job)
        self.assertTrue(u.startswith("https://api.smartrecruiters.com/v1/companies/"))
        self.assertIn("WesternDigital", u)
        self.assertIn("/postings/744000112340137", u)


class BackfillJdEligibilityTests(TestCase):
    """Regression: skipped/failed backfill uses description=' ' — must stay eligible."""

    def test_space_placeholder_description_remains_in_backfill_queue(self):
        import hashlib

        from companies.models import Company

        from harvest.models import RawJob
        from harvest.tasks import _backfill_eligible_queryset

        c = Company.objects.create(name="BackfillEligTestCo")
        url = "https://example.com/job/backfill-elig-1"
        h = hashlib.sha256(url.encode()).hexdigest()
        j = RawJob.objects.create(
            company=c,
            title="Test",
            url_hash=h,
            original_url=url,
            description=" ",
        )
        self.assertTrue(_backfill_eligible_queryset(None).filter(pk=j.pk).exists())

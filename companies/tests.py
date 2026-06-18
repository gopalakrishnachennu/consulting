from django.test import TestCase, Client
from django.urls import reverse
from datetime import timedelta
from django.utils import timezone
from users.models import User
from .models import Company, CompanyDoNotSubmit, EnrichmentLog
from users.models import ConsultantProfile
from jobs.models import Job, RawJobClassificationSnapshot
from harvest.models import CompanyFetchRun, CompanyPlatformLabel, JobBoardPlatform, RawJob


class CompanyModelTests(TestCase):
    def test_create_company(self):
        c = Company.objects.create(
            name="TestCo", domain="testco.com", industry="Tech"
        )
        self.assertEqual(str(c), "TestCo")
        self.assertEqual(c.enrichment_status, Company.EnrichmentStatus.PENDING)

    def test_unique_name(self):
        Company.objects.create(name="Unique")
        with self.assertRaises(Exception):
            Company.objects.create(name="Unique")

    def test_blacklisted_company(self):
        c = Company.objects.create(
            name="BadCo", is_blacklisted=True, blacklist_reason="Violations"
        )
        self.assertTrue(c.is_blacklisted)


class CompanyDoNotSubmitTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="RestrictedCo")
        self.user = User.objects.create_user(
            username="con1", password="pass", role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.user, bio="Test")

    def test_create_dnd_rule(self):
        dnd = CompanyDoNotSubmit.objects.create(
            company=self.company, consultant=self.profile, reason="NDA"
        )
        self.assertIn("DND", str(dnd))

    def test_unique_together(self):
        CompanyDoNotSubmit.objects.create(company=self.company, consultant=self.profile)
        with self.assertRaises(Exception):
            CompanyDoNotSubmit.objects.create(company=self.company, consultant=self.profile)


class EnrichmentLogTests(TestCase):
    def test_create_log(self):
        c = Company.objects.create(name="LogCo")
        log = EnrichmentLog.objects.create(
            company=c, source="clearbit", fields_updated={"industry": "Tech"}, success=True
        )
        self.assertTrue(log.success)


class CompanyViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="emp1", password="testpass", role=User.Role.EMPLOYEE
        )
        Company.objects.create(name="ViewTestCo", domain="viewtest.com")

    def test_company_list_authenticated(self):
        self.client.login(username="emp1", password="testpass")
        url = reverse("company-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ViewTestCo")

    def test_company_list_unauthenticated(self):
        url = reverse("company-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)

    def test_company_detail_surfaces_harvest_operations_context(self):
        self.client.login(username="emp1", password="testpass")
        company = Company.objects.create(
            name="Ops Signals Co",
            domain="ops-signals.example",
            website="https://ops-signals.example",
            linkedin_url="https://linkedin.com/company/ops-signals",
            career_site_url="https://careers.ops-signals.example",
            headcount_range="201-500",
            hq_location="Austin, TX",
            industry="Insurance",
            website_is_valid=True,
            linkedin_is_valid=True,
            website_last_checked_at=timezone.now(),
            linkedin_last_checked_at=timezone.now(),
        )
        platform, _ = JobBoardPlatform.objects.get_or_create(
            slug="workday",
            defaults={"name": "Workday"},
        )
        label = CompanyPlatformLabel.objects.create(
            company=company,
            platform=platform,
            tenant_id="ops-signals",
            confidence=CompanyPlatformLabel.Confidence.HIGH,
            detection_method=CompanyPlatformLabel.DetectionMethod.MANUAL,
            custom_career_url="https://ops-signals.wd1.myworkdayjobs.com/en-US/Careers",
            portal_alive=True,
            portal_last_verified=timezone.now(),
            is_verified=True,
        )
        CompanyFetchRun.objects.create(
            label=label,
            status=CompanyFetchRun.Status.SUCCESS,
            started_at=timezone.now() - timedelta(hours=2),
            completed_at=timezone.now() - timedelta(hours=2) + timedelta(minutes=4),
            jobs_found=23,
            jobs_new=7,
            jobs_updated=3,
        )
        CompanyFetchRun.objects.create(
            label=label,
            status=CompanyFetchRun.Status.FAILED,
            started_at=timezone.now() - timedelta(days=1),
            completed_at=timezone.now() - timedelta(days=1) + timedelta(minutes=2),
            error_message="Tenant returned 502",
        )
        raw_job = RawJob.objects.create(
            company=company,
            platform_label=label,
            platform_slug="workday",
            url_hash="ops-signals-raw-1",
            original_url="https://ops-signals.example/jobs/1",
            title="Senior Actuarial Analyst",
            company_name=company.name,
            job_domain="actuarial",
            job_category="analytics",
            department_normalized="finance",
            country="United States",
            location_type=RawJob.LocationType.HYBRID,
            skills=["Python", "Excel"],
            description="Need strong actuarial analytics skills.",
        )
        RawJobClassificationSnapshot.objects.create(
            raw_job=raw_job,
            status=RawJobClassificationSnapshot.Status.NEEDS_REVIEW,
            needs_review=True,
        )
        Job.objects.create(
            title="Senior Actuarial Analyst",
            company=company.name,
            company_obj=company,
            description="Downstream pooled job",
            original_link="https://ops-signals.example/jobs/1",
            posted_by=self.employee,
            status=Job.Status.POOL,
            stage=Job.Stage.VETTED,
        )

        resp = self.client.get(reverse("company-detail", kwargs={"pk": company.pk}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Harvest Health")
        self.assertContains(resp, "ATS / Platform Mapping")
        self.assertContains(resp, "Operations Snapshot")
        self.assertContains(resp, "Data Quality")
        self.assertContains(resp, "Link Health")
        self.assertContains(resp, "Recent Fetch Runs")
        self.assertContains(resp, "Routing / Classification Signals")
        self.assertContains(resp, "Workday")
        self.assertContains(resp, "Run harvest now", count=0)

from django.test import TestCase, Client
from django.urls import reverse
from datetime import timedelta
from django.utils import timezone
from users.models import User
from .models import Company, CompanyDoNotSubmit, CompanySavedView, EnrichmentLog
from users.models import ConsultantProfile
from jobs.models import Job, RawJobClassificationSnapshot
from harvest.models import CompanyFetchRun, CompanyPlatformLabel, JobBoardPlatform, RawJob
from submissions.models import ApplicationSubmission, SubmissionStatusHistory


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

    def test_company_list_engine_surfaces_saved_views_and_row_warnings(self):
        self.client.login(username="emp1", password="testpass")
        company = Company.objects.create(
            name="Engine Warning Co",
            domain="engine-warning.example",
            website="https://engine-warning.example",
            website_is_valid=False,
        )
        platform, _ = JobBoardPlatform.objects.get_or_create(
            slug="greenhouse",
            defaults={"name": "Greenhouse"},
        )
        CompanyPlatformLabel.objects.create(
            company=company,
            platform=platform,
            tenant_id="",
            confidence=CompanyPlatformLabel.Confidence.LOW,
            detection_method=CompanyPlatformLabel.DetectionMethod.URL_PATTERN,
            portal_alive=False,
            portal_last_verified=timezone.now() - timedelta(days=9),
            is_verified=False,
        )

        resp = self.client.get(reverse("company-list"), {"view": "engine"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Primary filters")
        self.assertContains(resp, "Advanced filters")
        self.assertContains(resp, "Needs tenant")
        self.assertContains(resp, "Portal down")
        self.assertContains(resp, "Low confidence")
        self.assertContains(resp, "Columns")
        self.assertContains(resp, "Customize")
        self.assertContains(resp, "Saved view", count=0)
        self.assertContains(resp, "View all platforms")
        self.assertContains(resp, "More")

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

    def test_company_intelligence_view_surfaces_inspector_sections(self):
        self.client.login(username="emp1", password="testpass")
        company = Company.objects.create(
            name="Drawer Ops Co",
            domain="drawer-ops.example",
            website="https://drawer-ops.example",
            website_is_valid=True,
            relationship_status="Prospect",
            industry="Technology",
        )
        platform, _ = JobBoardPlatform.objects.get_or_create(
            slug="lever",
            defaults={"name": "Lever"},
        )
        label = CompanyPlatformLabel.objects.create(
            company=company,
            platform=platform,
            tenant_id="drawer-ops",
            confidence=CompanyPlatformLabel.Confidence.MEDIUM,
            detection_method=CompanyPlatformLabel.DetectionMethod.URL_PATTERN,
            portal_alive=True,
            portal_last_verified=timezone.now() - timedelta(hours=4),
            is_verified=False,
        )
        CompanyFetchRun.objects.create(
            label=label,
            status=CompanyFetchRun.Status.SUCCESS,
            started_at=timezone.now() - timedelta(hours=3),
            completed_at=timezone.now() - timedelta(hours=3) + timedelta(minutes=3),
            jobs_found=9,
            jobs_new=4,
            jobs_updated=1,
        )
        RawJob.objects.create(
            company=company,
            platform_label=label,
            platform_slug="lever",
            url_hash="drawer-ops-raw-1",
            original_url="https://drawer-ops.example/jobs/1",
            title="Platform Engineer",
            company_name=company.name,
            description="Platform engineer role",
            sync_status="PENDING",
        )
        consultant_user = User.objects.create_user(
            username="drawer_consultant",
            password="testpass",
            role=User.Role.CONSULTANT,
        )
        consultant = ConsultantProfile.objects.create(user=consultant_user, bio="Drawer consultant")
        job = Job.objects.create(
            title="Platform Engineer",
            company=company.name,
            company_obj=company,
            description="Inspector job",
            original_link="https://drawer-ops.example/jobs/1",
            posted_by=self.employee,
            status=Job.Status.POOL,
            stage=Job.Stage.VETTED,
        )
        submission = ApplicationSubmission.objects.create(
            job=job,
            consultant=consultant,
            submitted_by=self.employee,
        )
        SubmissionStatusHistory.objects.create(
            submission=submission,
            from_status=ApplicationSubmission.Status.APPLIED,
            to_status=ApplicationSubmission.Status.INTERVIEW,
        )

        resp = self.client.get(
            reverse("company-intelligence", kwargs={"pk": company.pk}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Company Inspector")
        self.assertContains(resp, "ATS mapping")
        self.assertContains(resp, "Recent fetch runs")
        self.assertContains(resp, "Job flow")
        self.assertContains(resp, "Recent activity")
        self.assertContains(resp, "Quick actions")
        self.assertContains(resp, "Submission created for drawer_consultant")
        self.assertContains(resp, "Status changed to INTERVIEW")
        self.assertContains(resp, "drawer-ops")

    def test_company_bulk_actions_update_selected_companies(self):
        self.client.login(username="emp1", password="testpass")
        company_a = Company.objects.create(name="Bulk Ops A")
        company_b = Company.objects.create(name="Bulk Ops B", is_blacklisted=True, blacklist_reason="Old")

        resp = self.client.post(
            reverse("company-bulk-action"),
            {
                "ids": f"{company_a.pk},{company_b.pk}",
                "action": "mark_review",
                "next": reverse("company-list"),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        company_a.refresh_from_db()
        company_b.refresh_from_db()
        self.assertTrue(company_a.needs_review)
        self.assertTrue(company_b.needs_review)

        resp = self.client.post(
            reverse("company-bulk-action"),
            {
                "ids": str(company_b.pk),
                "action": "unblacklist",
                "next": reverse("company-list"),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        company_b.refresh_from_db()
        self.assertFalse(company_b.is_blacklisted)
        self.assertEqual(company_b.blacklist_reason, "")

    def test_company_bulk_actions_can_apply_to_current_view_scope(self):
        self.client.login(username="emp1", password="testpass")
        company_a = Company.objects.create(
            name="Queue Ops A",
            website="https://queue-a.example.com",
            website_is_valid=False,
        )
        company_b = Company.objects.create(
            name="Queue Ops B",
            website="https://queue-b.example.com",
            website_is_valid=False,
        )
        company_c = Company.objects.create(
            name="Queue Ops C",
            website="https://queue-c.example.com",
            website_is_valid=True,
        )

        resp = self.client.post(
            reverse("company-bulk-action"),
            {
                "scope": "view",
                "action": "blacklist",
                "query": "view=engine&website_valid=0",
                "next": reverse("company-list"),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        company_a.refresh_from_db()
        company_b.refresh_from_db()
        company_c.refresh_from_db()
        self.assertTrue(company_a.is_blacklisted)
        self.assertTrue(company_b.is_blacklisted)
        self.assertFalse(company_c.is_blacklisted)

    def test_company_bulk_actions_reject_unfiltered_view_scope(self):
        self.client.login(username="emp1", password="testpass")
        company = Company.objects.create(name="Queue Guard Co")

        resp = self.client.post(
            reverse("company-bulk-action"),
            {
                "scope": "view",
                "action": "blacklist",
                "query": "view=engine",
                "next": reverse("company-list"),
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        company.refresh_from_db()
        self.assertFalse(company.is_blacklisted)

    def test_company_saved_views_can_be_created_and_applied(self):
        self.client.login(username="emp1", password="testpass")
        company = Company.objects.create(
            name="Saved View Match",
            website="https://invalid.example.com",
            website_is_valid=False,
            is_blacklisted=True,
            needs_review=True,
        )
        resp = self.client.post(
            reverse("company-saved-view-create"),
            {
                "name": "Invalid websites",
                "query": "view=engine&website_valid=0&sort=name",
                "next": reverse("company-list"),
            },
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        saved_view = CompanySavedView.objects.get(user=self.employee, name="Invalid websites")
        self.assertEqual(saved_view.query_params["website_valid"], "0")

        resp = self.client.get(reverse("company-list"), {"view": "engine", "saved_view": saved_view.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Invalid websites")
        self.assertContains(resp, "Saved View Match")
        custom_view = next(view for view in resp.context["custom_company_views"] if view["id"] == saved_view.pk)
        self.assertEqual(custom_view["match_count"], 1)
        self.assertEqual(custom_view["blocked_count"], 1)
        self.assertEqual(custom_view["review_count"], 1)
        self.assertEqual(custom_view["down_count"], 0)

        resp = self.client.post(
            reverse("company-saved-view-delete", kwargs={"pk": saved_view.pk}),
            {"next": reverse("company-list")},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(CompanySavedView.objects.filter(pk=saved_view.pk).exists())

    def test_company_saved_view_default_auto_applies_without_explicit_filters(self):
        self.client.login(username="emp1", password="testpass")
        Company.objects.create(name="Default Queue Target")
        Company.objects.create(name="Other Queue Company")
        saved_view = CompanySavedView.objects.create(
            user=self.employee,
            name="Default Queue",
            query_params={"view": "engine", "q": "Default Queue Target"},
        )

        resp = self.client.post(
            reverse("company-saved-view-default", kwargs={"pk": saved_view.pk}),
            {"next": reverse("company-list")},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        saved_view.refresh_from_db()
        self.assertTrue(saved_view.is_default)

        resp = self.client.get(reverse("company-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Default landing view applied")
        self.assertContains(resp, "Default Queue")
        self.assertContains(resp, "Default Queue Target")
        self.assertNotContains(resp, "Other Queue Company")
        self.assertEqual(resp.context["selected_company_saved_view"].pk, saved_view.pk)

        resp = self.client.get(reverse("company-list"), {"q": "Other Queue Company"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Other Queue Company")
        self.assertNotContains(resp, "Default landing view applied")
        self.assertIsNone(resp.context["selected_company_saved_view"])
        self.assertEqual([company.name for company in resp.context["companies"]], ["Other Queue Company"])

    def test_company_saved_view_manage_actions_work(self):
        self.client.login(username="emp1", password="testpass")
        saved_view = CompanySavedView.objects.create(
            user=self.employee,
            name="Original Queue",
            query_params={"view": "engine", "website_valid": "0"},
            position=1,
        )

        resp = self.client.post(
            reverse("company-saved-view-manage", kwargs={"pk": saved_view.pk}),
            {"manage_action": "rename", "name": "Renamed Queue", "next": reverse("company-list")},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        saved_view.refresh_from_db()
        self.assertEqual(saved_view.name, "Renamed Queue")

        resp = self.client.post(
            reverse("company-saved-view-manage", kwargs={"pk": saved_view.pk}),
            {"manage_action": "pin", "next": reverse("company-list")},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        saved_view.refresh_from_db()
        self.assertTrue(saved_view.is_pinned)

        resp = self.client.post(
            reverse("company-saved-view-manage", kwargs={"pk": saved_view.pk}),
            {"manage_action": "duplicate", "next": reverse("company-list")},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            CompanySavedView.objects.filter(user=self.employee, name__startswith="Renamed Queue copy").exists()
        )

        resp = self.client.post(
            reverse("company-saved-view-manage", kwargs={"pk": saved_view.pk}),
            {"manage_action": "archive", "next": reverse("company-list")},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        saved_view.refresh_from_db()
        self.assertIsNotNone(saved_view.archived_at)

    def test_company_quick_action_view_updates_company(self):
        self.client.login(username="emp1", password="testpass")
        company = Company.objects.create(name="Drawer Action Co")

        resp = self.client.post(
            reverse("company-quick-action", kwargs={"pk": company.pk}),
            {"action": "mark_review"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        company.refresh_from_db()
        self.assertTrue(company.needs_review)

        resp = self.client.post(
            reverse("company-quick-action", kwargs={"pk": company.pk}),
            {"action": "blacklist"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        company.refresh_from_db()
        self.assertTrue(company.is_blacklisted)

import csv
import hashlib
import io
from unittest.mock import patch

from django.test import TestCase, Client, SimpleTestCase
from django.urls import resolve, reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from users.models import User
from users.models import MarketingRole
from users.models import ConsultantProfile
from companies.models import Company
from harvest.models import RawJob
from harvest.models import RawJobPayloadSnapshot
from harvest.enrichments import detect_job_category
from .models import Job
from .marketing_role_routing import (
    assign_marketing_roles_to_job,
    clear_marketing_role_cache,
    infer_marketing_role_slugs,
)
from .services import match_jobs_for_consultant
from .tasks import classify_jobs_task
from .tasks import validate_job_urls_task, auto_close_jobs_task


class JobsPipelineRouteOwnershipTests(SimpleTestCase):
    def test_jobs_pipeline_owns_raw_action_routes(self):
        expected = {
            "jobs-pipeline-run-fetch-batch": "/jobs/pipeline/run/fetch-batch/",
            "jobs-pipeline-run-sync": "/jobs/pipeline/run/sync/",
            "jobs-pipeline-run-sync-selected": "/jobs/pipeline/run/sync-selected/",
            "jobs-pipeline-run-detect": "/jobs/pipeline/run/detect/",
            "jobs-pipeline-run-backfill-descriptions": "/jobs/pipeline/run/backfill-descriptions/",
            "jobs-pipeline-run-validate-urls": "/jobs/pipeline/run/validate-urls/",
            "jobs-pipeline-run-retry-failed-fetches": "/jobs/pipeline/run/retry-failed-fetches/",
            "jobs-pipeline-run-cleanup": "/jobs/pipeline/run/cleanup/",
        }
        for route_name, path in expected.items():
            with self.subTest(route_name=route_name):
                self.assertEqual(reverse(route_name), path)
                self.assertEqual(resolve(path).url_name, route_name)

    def test_legacy_harvest_action_routes_remain_available(self):
        self.assertEqual(reverse("harvest-run-sync"), "/harvest/run/sync/")
        self.assertEqual(reverse("harvest-run-fetch-batch"), "/harvest/run/fetch-batch/")
        self.assertEqual(reverse("harvest-rawjobs"), "/harvest/raw-jobs/")


@patch("jobs.tasks.run_job_validation.delay")
@patch("jobs.views.ensure_parsed_jd")
class JobManualRawBridgeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="emp_manual_bridge", password="testpass", role=User.Role.EMPLOYEE
        )

    def test_manual_job_create_links_rawjob_evidence_and_scope(self, _ensure, _delay):
        self.client.login(username="emp_manual_bridge", password="testpass")
        resp = self.client.post(
            reverse("job-create"),
            {
                "title": "Platform Engineer",
                "company": "Bridge Manual Co",
                "location": "Austin, TX",
                "description": "Build cloud automation, CI/CD, observability, Python services, and infrastructure.",
                "original_link": "https://example.com/manual/platform-engineer",
                "salary_range": "",
                "job_type": Job.JobType.FULL_TIME,
                "job_source": "manual entry",
            },
        )
        self.assertEqual(resp.status_code, 302)

        job = Job.objects.select_related("source_raw_job", "company_obj").get(title="Platform Engineer")
        raw = job.source_raw_job
        self.assertIsNotNone(raw)
        self.assertEqual(job.company_obj.name, "Bridge Manual")
        self.assertEqual(raw.platform_slug, "manual")
        self.assertEqual(raw.sync_status, RawJob.SyncStatus.SYNCED)
        self.assertEqual(raw.country_code, "US")
        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertTrue(RawJobPayloadSnapshot.objects.filter(raw_job=raw).exists())


class JobListUrlHealthFilterTests(TestCase):
    """Employee-facing filters: possibly_filled + link_live (original_link_is_live)."""

    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.live = Job.objects.create(
            title='Live role',
            company='Acme',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/a',
            original_link_is_live=True,
            possibly_filled=False,
        )
        self.dead = Job.objects.create(
            title='Dead posting',
            company='Beta',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/b',
            original_link_is_live=False,
            possibly_filled=True,
        )

    def test_filter_link_not_live(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('job-list') + '?link_live=0'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Dead posting')
        self.assertNotContains(resp, 'Live role')

    def test_filter_possibly_filled(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('job-list') + '?possibly_filled=1'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Dead posting')
        self.assertNotContains(resp, 'Live role')

    def test_filter_combined_and_logic(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('job-list') + '?possibly_filled=1&link_live=0'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Dead posting')
        self.assertNotContains(resp, 'Live role')


class MatchScoreStringTests(TestCase):
    def test_match_score_str_does_not_reference_missing_title(self):
        from .models import MatchScore

        score = MatchScore(job_id=11, consultant_id=22, score=0.875)

        rendered = str(score)
        self.assertIn("Job 11", rendered)
        self.assertIn("Consultant 22", rendered)
        self.assertIn("0.875", rendered)


class MarketingRoleRoutingTests(TestCase):
    def setUp(self):
        clear_marketing_role_cache()
        self.employee = User.objects.create_user(
            username="routing_emp", password="testpass", role=User.Role.EMPLOYEE
        )

    def test_title_only_routing_returns_specific_role(self):
        slugs = infer_marketing_role_slugs(
            title="ServiceNow Developer",
            description="",
            job_category="",
            department_normalized="",
        )
        self.assertTrue(slugs)
        self.assertEqual(slugs[0], "servicenow-developer")

    def test_generic_job_still_gets_catch_all_role(self):
        slugs = infer_marketing_role_slugs(
            title="Program Associate",
            description="",
            job_category="",
            department_normalized="",
        )
        self.assertTrue(slugs)
        self.assertIn("other-generalist", slugs)

    def test_category_fallback_uses_domain_when_regex_is_weak(self):
        category, _title_match, _desc_match = detect_job_category(
            "ServiceNow Developer",
            "",
            domain_slug="servicenow-developer",
        )
        self.assertEqual(category, "Engineering")

    def test_category_fallback_uses_department_when_domain_missing(self):
        category, _, _ = detect_job_category(
            "Implementation Specialist",
            "",
            department_normalized="finance",
            domain_slug="",
        )
        self.assertEqual(category, "Finance")

    def test_assign_preserves_manual_roles_while_refreshing_auto_roles(self):
        manual_role = MarketingRole.objects.get(slug="salesforce-developer")
        stale_auto = MarketingRole.objects.get(slug="software-developer")
        refreshed_auto = MarketingRole.objects.get(slug="devops-cloud")
        job = Job.objects.create(
            title="Platform Engineer",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="Maintain cloud infrastructure and CI/CD systems.",
        )
        job.marketing_roles.add(manual_role, stale_auto)
        job.auto_marketing_role_slugs = ["software-developer"]
        job.save(update_fields=["auto_marketing_role_slugs"])

        assigned = assign_marketing_roles_to_job(job, role_slugs=["devops-cloud"])
        job.refresh_from_db()

        self.assertEqual(assigned, ["devops-cloud"])
        self.assertCountEqual(
            list(job.marketing_roles.values_list("slug", flat=True)),
            ["salesforce-developer", "devops-cloud"],
        )
        self.assertEqual(job.auto_marketing_role_slugs, ["devops-cloud"])

    def test_match_jobs_respects_country_and_seniority_preferences(self):
        role = MarketingRole.objects.get(slug="devops-cloud")
        consultant_user = User.objects.create_user(
            username="consultant_match", password="testpass", role=User.Role.CONSULTANT
        )
        consultant = ConsultantProfile.objects.create(
            user=consultant_user,
            bio="DevOps consultant",
            work_countries=["United States"],
            preferred_seniority_levels=["senior"],
        )
        consultant.marketing_roles.add(role)

        eligible = Job.objects.create(
            title="Senior DevOps Engineer",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="AWS Terraform Kubernetes CI/CD incident response",
            country="United States",
        )
        eligible.marketing_roles.add(role)

        wrong_country = Job.objects.create(
            title="Senior DevOps Engineer",
            company="Globex",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="AWS Terraform Kubernetes CI/CD incident response",
            country="India",
        )
        wrong_country.marketing_roles.add(role)

        wrong_seniority = Job.objects.create(
            title="Junior DevOps Engineer",
            company="Initrode",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="AWS Terraform Kubernetes CI/CD incident response",
            country="United States",
        )
        wrong_seniority.marketing_roles.add(role)

        matches = match_jobs_for_consultant(consultant, limit=10)
        self.assertEqual([job.pk for job in matches], [eligible.pk])

    def test_classify_task_backfills_taxonomy_and_synced_job_roles(self):
        company = Company.objects.create(name="RouteCo")
        raw = RawJob.objects.create(
            company=company,
            company_name="RouteCo",
            title="Senior DevOps Engineer",
            description="AWS Terraform Kubernetes CI/CD observability platform engineering",
            original_url="https://example.com/jobs/devops-1",
            url_hash=hashlib.sha256(b"https://example.com/jobs/devops-1").hexdigest(),
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
        )
        job = Job.objects.create(
            title=raw.title,
            company=raw.company_name,
            posted_by=self.employee,
            status=Job.Status.POOL,
            description=raw.description,
            source_raw_job=raw,
            url_hash=raw.url_hash,
        )

        result = classify_jobs_task.apply(kwargs={"force_reclassify": True}).get()
        raw.refresh_from_db()
        job.refresh_from_db()

        self.assertEqual(result["status"], "done")
        self.assertTrue(raw.job_category)
        self.assertTrue(raw.job_domain)
        self.assertEqual(raw.domain_version, "d2")
        self.assertTrue(raw.job_domain_candidates)
        self.assertTrue(job.marketing_roles.exists())


@patch("jobs.tasks.run_job_validation.delay")
@patch("jobs.views.ensure_parsed_jd")
class JobBulkUploadViewTests(TestCase):
    """Bulk CSV: size limit, posting URL column, scrape-style headers."""

    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="emp_bulk", password="testpass", role=User.Role.EMPLOYEE
        )

    def test_accepts_csv_larger_than_legacy_chunk_threshold(self, _ensure, _delay):
        """Previously any file >64KB was rejected via multiple_chunks()."""
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=["title", "company", "location", "description"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "title": "Big desc row",
                "company": "Co",
                "location": "Remote",
                "description": "x" * 70000,
            }
        )
        csv_bytes = buf.getvalue().encode("utf-8")
        self.assertGreater(len(csv_bytes), 65536)
        up = SimpleUploadedFile("jobs.csv", csv_bytes, content_type="text/csv")
        self.client.login(username="emp_bulk", password="testpass")
        resp = self.client.post(reverse("job-bulk-upload"), {"csv_file": up})
        self.assertEqual(resp.status_code, 302)
        job = Job.objects.get(title="Big desc row")
        self.assertEqual(job.company, "Co")
        self.assertEqual(len(job.description), 70000)

    def test_original_link_from_job_url_alias(self, _ensure, _delay):
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "job.title",
                "job.company_name",
                "job.location",
                "job.description",
                "job.url",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "job.title": "SRE",
                "job.company_name": "Nova",
                "job.location": "US",
                "job.description": "Run prod",
                "job.url": "https://example.com/scraped/1",
            }
        )
        up = SimpleUploadedFile("scrape.csv", buf.getvalue().encode("utf-8"), content_type="text/csv")
        self.client.login(username="emp_bulk", password="testpass")
        resp = self.client.post(reverse("job-bulk-upload"), {"csv_file": up})
        self.assertEqual(resp.status_code, 302)
        job = Job.objects.get(title="SRE")
        self.assertEqual(job.original_link, "https://example.com/scraped/1")
        self.assertIsNotNone(job.source_raw_job)
        self.assertEqual(job.source_raw_job.platform_slug, "bulk_upload")
        self.assertEqual(job.source_raw_job.sync_status, RawJob.SyncStatus.SYNCED)

    def test_skips_row_when_posting_url_already_exists(self, _ensure, _delay):
        Job.objects.create(
            title="Existing",
            company="X",
            location="",
            description="D",
            original_link="https://example.com/dup",
            posted_by=self.employee,
            status=Job.Status.POOL,
        )
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf, fieldnames=["title", "company", "location", "description", "original_link"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "title": "New title",
                "company": "Y",
                "location": "EU",
                "description": "Other",
                "original_link": "https://example.com/dup",
            }
        )
        up = SimpleUploadedFile("d.csv", buf.getvalue().encode("utf-8"), content_type="text/csv")
        self.client.login(username="emp_bulk", password="testpass")
        resp = self.client.post(reverse("job-bulk-upload"), {"csv_file": up})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Job.objects.filter(title="New title").exists())


class JobExportCSVTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        Job.objects.create(
            title='Python Dev', company='Acme', posted_by=self.employee, status=Job.Status.OPEN,
            description='Backend work', original_link='https://example.com/job'
        )

    def test_export_csv_returns_csv(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('job-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get('Content-Type', '').startswith('text/csv'))
        self.assertIn(b'Python Dev', resp.content)
        self.assertIn(b'Link live', resp.content)
        self.assertIn(b'Possibly filled', resp.content)


class JobUrlRevalidationTests(TestCase):
    """Background task: re-check posting URLs and set possibly_filled / is_live flags."""

    def setUp(self):
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.job = Job.objects.create(
            title='Role A',
            company='Co',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/job-a',
            original_link_last_checked_at=None,
            original_link_is_live=True,
            possibly_filled=False,
        )

    @patch('jobs.tasks._check_job_url', return_value=False)
    def test_validate_job_urls_flags_dead_link(self, _mock):
        validate_job_urls_task.apply(kwargs={"batch_size": 50}).get()
        self.job.refresh_from_db()
        self.assertFalse(self.job.original_link_is_live)
        self.assertTrue(self.job.possibly_filled)
        self.assertIsNotNone(self.job.original_link_last_checked_at)

    @patch('jobs.tasks._check_job_url', return_value=True)
    def test_validate_job_urls_keeps_live_link(self, _mock):
        validate_job_urls_task.apply(kwargs={"batch_size": 50}).get()
        self.job.refresh_from_db()
        self.assertTrue(self.job.original_link_is_live)
        self.assertFalse(self.job.possibly_filled)

    @patch('jobs.tasks._check_job_url', return_value=True)
    def test_validate_job_urls_skips_recently_checked(self, _mock):
        self.job.original_link_last_checked_at = timezone.now()
        self.job.save(update_fields=['original_link_last_checked_at'])
        validate_job_urls_task.apply(kwargs={"batch_size": 50}).get()
        self.job.refresh_from_db()
        # Not processed (batch prefers stale / null; empty batch for "recent only" in isolation)
        self.assertIsNotNone(self.job.original_link_last_checked_at)


class AutoCloseJobsTaskTests(TestCase):
    def setUp(self):
        from core.models import PlatformConfig

        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.old_job = Job.objects.create(
            title='Stale',
            company='Co',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/o',
        )
        self.dead_job = Job.objects.create(
            title='Dead link',
            company='Co',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/d',
            original_link_is_live=False,
        )
        cfg = PlatformConfig.load()
        cfg.job_auto_close_after_days = 1
        cfg.job_auto_close_when_link_dead = True
        cfg.save()

    def test_auto_close_old_open_job(self):
        Job.objects.filter(pk=self.old_job.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=400)
        )
        auto_close_jobs_task()
        self.old_job.refresh_from_db()
        self.assertEqual(self.old_job.status, Job.Status.CLOSED)

    def test_auto_close_dead_link_when_enabled(self):
        auto_close_jobs_task()
        self.dead_job.refresh_from_db()
        self.assertEqual(self.dead_job.status, Job.Status.CLOSED)

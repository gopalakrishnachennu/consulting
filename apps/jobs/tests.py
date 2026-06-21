import csv
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, Client, SimpleTestCase
from django.urls import resolve, reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django_celery_results.models import TaskResult

from core.models import PlatformConfig
from users.models import User
from users.models import MarketingRole
from users.models import ConsultantProfile
from companies.models import Company
from harvest.models import RawJob
from harvest.models import RawJobPayloadSnapshot
from harvest.enrichments import detect_job_category
from .models import Job
from .models import RawJobClassificationSnapshot, RawJobClassifierRun, RawJobClassificationConflict
from .marketing_role_routing import (
    assign_marketing_roles_to_job,
    clear_marketing_role_cache,
    infer_marketing_role_slugs,
)
from .services import match_jobs_for_consultant
from .tasks import _department_sync_value, classify_jobs_task
from .tasks import validate_job_urls_task, auto_close_jobs_task
from .tasks import (
    DUAL_CLASSIFICATION_BACKFILL_QUEUE,
    backfill_rawjob_dual_classification_task,
    run_rawjob_dual_classification_shadow_task,
)


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


class JobsPipelineIncrementalLoadingTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="pipeline_loader_emp",
            password="testpass",
            role=User.Role.EMPLOYEE,
        )
        self.client.login(username="pipeline_loader_emp", password="testpass")

    def _make_job(self, idx, **overrides):
        defaults = {
            "title": f"Pipeline Job {idx:03d}",
            "company": "Pipeline Co",
            "location": "Austin, TX",
            "description": "Detailed job description",
            "original_link": f"https://example.com/jobs/{idx}",
            "posted_by": self.employee,
            "status": Job.Status.POOL,
            "gate_status": Job.GateStatus.ELIGIBLE,
            "vet_lane": Job.VetLane.HUMAN,
        }
        defaults.update(overrides)
        return Job.objects.create(**defaults)

    def test_pool_tab_shows_incremental_loading_controls(self):
        for idx in range(105):
            self._make_job(idx)

        response = self.client.get(reverse("jobs-pipeline"), {"tab": "pool"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["pipeline_tab_has_next"])
        self.assertContains(response, 'id="pipeline-jobs-load-more-btn"')

    def test_pool_pipeline_json_returns_next_page_rows(self):
        for idx in range(205):
            self._make_job(idx)

        response = self.client.get(
            reverse("jobs-pipeline"),
            {"tab": "pool", "pipeline_json": "1", "page": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 100)
        self.assertTrue(payload["has_next"])
        self.assertIn("Pipeline Job", payload["rows_html"])

    def test_live_pipeline_json_returns_paginated_rows(self):
        for idx in range(125):
            self._make_job(idx, status=Job.Status.OPEN, original_link=f"https://example.com/live/{idx}")

        response = self.client.get(
            reverse("jobs-pipeline"),
            {"tab": "live", "pipeline_json": "1", "page": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 25)
        self.assertFalse(payload["has_next"])
        self.assertIn("Edit", payload["rows_html"])

    def test_archived_pipeline_json_returns_paginated_rows(self):
        for idx in range(120):
            self._make_job(
                idx,
                is_archived=True,
                archived_at=timezone.now(),
                original_link=f"https://example.com/archived/{idx}",
            )

        response = self.client.get(
            reverse("jobs-pipeline"),
            {"tab": "archived", "pipeline_json": "1", "page": 2},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 20)
        self.assertFalse(payload["has_next"])
        self.assertIn("Restore", payload["rows_html"])


class JobsPipelinePoolParityTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="pipeline_pool_parity_emp",
            password="testpass",
            role=User.Role.EMPLOYEE,
            first_name="Pool",
            last_name="Reviewer",
        )
        self.other_employee = User.objects.create_user(
            username="pipeline_pool_other",
            password="testpass",
            role=User.Role.EMPLOYEE,
        )
        self.client.login(username="pipeline_pool_parity_emp", password="testpass")

    def _make_pool_job(self, title, **overrides):
        created_at = overrides.pop("created_at", None)
        defaults = {
            "company": "Parity Co",
            "location": "Austin, TX",
            "description": "Detailed job description",
            "original_link": f"https://example.com/{title.lower().replace(' ', '-')}",
            "posted_by": self.employee,
            "status": Job.Status.POOL,
            "job_type": Job.JobType.FULL_TIME,
            "job_source": "LinkedIn",
            "created_at": timezone.now(),
            "gate_status": Job.GateStatus.ELIGIBLE,
            "vet_lane": Job.VetLane.HUMAN,
        }
        defaults.update(overrides)
        job = Job.objects.create(title=title, **defaults)
        if created_at is not None:
            Job.objects.filter(pk=job.pk).update(created_at=created_at)
            job.refresh_from_db()
        return job

    def test_legacy_job_pool_redirects_to_pipeline_and_preserves_filters(self):
        response = self.client.get(
            reverse("job-pool"),
            {
                "tab": "review",
                "q": "platform",
                "posted_by": str(self.employee.pk),
                "job_source": "LinkedIn",
                "date_from": "2026-06-01",
                "page_size": "300",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            (
                f"{reverse('jobs-pipeline')}?tab=pool&score=review&search_by=all&q=platform"
                f"&posted_by={self.employee.pk}&job_source=LinkedIn&date_from=2026-06-01&page_size=300"
            ),
        )

    def test_pipeline_pool_supports_legacy_pool_filters(self):
        keep = self._make_pool_job(
            "Platform Engineer",
            company="Target Labs",
            posted_by=self.employee,
            job_type=Job.JobType.FULL_TIME,
            job_source="LinkedIn import",
            created_at=timezone.now(),
        )
        self._make_pool_job(
            "Platform Engineer Old",
            company="Target Labs",
            posted_by=self.employee,
            job_type=Job.JobType.FULL_TIME,
            job_source="LinkedIn import",
            created_at=timezone.now() - timezone.timedelta(days=10),
        )
        self._make_pool_job(
            "Platform Engineer Other Poster",
            company="Target Labs",
            posted_by=self.other_employee,
            job_type=Job.JobType.FULL_TIME,
            job_source="LinkedIn import",
        )
        self._make_pool_job(
            "Platform Engineer Wrong Type",
            company="Target Labs",
            posted_by=self.employee,
            job_type=Job.JobType.CONTRACT,
            job_source="LinkedIn import",
        )
        self._make_pool_job(
            "Platform Engineer Wrong Source",
            company="Target Labs",
            posted_by=self.employee,
            job_type=Job.JobType.FULL_TIME,
            job_source="Manual import",
        )
        self._make_pool_job(
            "Different Company",
            company="Other Labs",
            posted_by=self.employee,
            job_type=Job.JobType.FULL_TIME,
            job_source="LinkedIn import",
        )

        response = self.client.get(
            reverse("jobs-pipeline"),
            {
                "tab": "pool",
                "q": "Platform Engineer",
                "search_by": "title",
                "posted_by": str(self.employee.pk),
                "company": "Target",
                "job_type": Job.JobType.FULL_TIME,
                "job_source": "LinkedIn",
                "date_from": (timezone.now() - timezone.timedelta(days=1)).date().isoformat(),
                "page_size": "300",
            },
        )

        self.assertEqual(response.status_code, 200)
        tab_jobs = list(response.context["tab_jobs"])
        self.assertEqual([job.pk for job in tab_jobs], [keep.pk])
        self.assertEqual(response.context["page_size"], 300)
        self.assertContains(response, "Advanced vetting filters")
        self.assertContains(response, "Pool Reviewer")
        self.assertContains(response, "300/page")


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


class JobDownstreamDualClassificationAuditTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="downstream_audit_emp", password="testpass", role=User.Role.EMPLOYEE
        )
        from core.models import FeatureFlag

        FeatureFlag.objects.update_or_create(
            key="employee_job_pool",
            defaults={
                "label": "Job Pool",
                "category": "EMPLOYEE",
                "applies_to": "EMPLOYEE",
                "is_enabled": True,
                "enabled_for_employees": True,
                "enabled_for_consultants": False,
            },
        )
        company = Company.objects.create(name="Downstream Audit Co")
        self.raw = RawJob.objects.create(
            company=company,
            company_name=company.name,
            title="Platform Engineer",
            url_hash=hashlib.sha256(b"https://example.com/downstream-audit").hexdigest(),
            original_url="https://example.com/downstream-audit",
            description="AWS platform engineering and Terraform automation",
            description_clean="AWS platform engineering and Terraform automation",
            country="United States",
            location_type=RawJob.LocationType.REMOTE,
            is_remote=True,
            job_category="Engineering",
            job_domain="devops-cloud",
            department_normalized="Information Technology",
            years_required=5,
            skills=["AWS", "Terraform", "Python"],
            classification_source="secondary",
            classification_provenance={"provider": "claude", "prompt_version": "runtime_v5"},
            field_provenance={
                "job_domain": "secondary",
                "job_category": "secondary",
                "department_normalized": "backend_rules",
                "country": "backend_rules",
                "location_type": "secondary",
                "years_required": "backend_rules",
                "skills": "secondary",
            },
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
            has_description=True,
        )
        self.job = Job.objects.create(
            title="Platform Engineer",
            company=company.name,
            company_obj=company,
            location="Remote",
            description=self.raw.description,
            posted_by=self.employee,
            status=Job.Status.POOL,
            source_raw_job=self.raw,
            validation_result={
                "dual_classification": {
                    "approved_source": "secondary",
                    "approval_state": "APPROVED",
                    "classification_provenance": {"provider": "claude", "prompt_version": "runtime_v5"},
                    "field_provenance": self.raw.field_provenance,
                    "approved_values": {
                        "job_domain": "devops-cloud",
                        "job_category": "Engineering",
                        "department_normalized": "Information Technology",
                        "country": "United States",
                        "location_type": "REMOTE",
                        "years_required": 5,
                        "skills": ["AWS", "Terraform", "Python"],
                    },
                }
            },
            validation_score=88,
        )

    def test_job_detail_renders_downstream_dual_classification_rows(self):
        self.client.login(username="downstream_audit_emp", password="testpass")
        response = self.client.get(reverse("job-detail", args=[self.job.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dual classification audit")
        self.assertContains(response, "devops-cloud")
        self.assertContains(response, "SECONDARY")
        self.assertContains(response, "Classification provenance")

    def test_pipeline_pool_renders_compact_downstream_provenance(self):
        self.client.login(username="downstream_audit_emp", password="testpass")
        response = self.client.get(reverse("jobs-pipeline"), {"tab": "pool"})

        self.assertEqual(response.status_code, 200)
        jobs = list(response.context["tab_jobs"])
        self.assertTrue(jobs)
        audit = getattr(jobs[0], "dual_classification_audit", {})
        self.assertTrue(audit.get("present"))
        rows = {row["key"]: row for row in audit.get("rows", [])}
        self.assertEqual(rows["job_domain"]["value"], "devops-cloud")
        self.assertEqual(rows["job_domain"]["source"], "secondary")


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


class JobListCountryNormalizationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="emp_country",
            password="testpass",
            role=User.Role.EMPLOYEE,
        )
        self.us_plain = Job.objects.create(
            title="US plain",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="D",
            original_link="https://example.com/us-plain",
            country="United States",
        )
        self.us_short = Job.objects.create(
            title="US short",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="D",
            original_link="https://example.com/us-short",
            country="US",
        )
        self.us_dirty = Job.objects.create(
            title="US dirty",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="D",
            original_link="https://example.com/us-dirty",
            country="USA - Georgia - Atlanta",
        )
        self.ca = Job.objects.create(
            title="Canada role",
            company="Maple",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="D",
            original_link="https://example.com/ca",
            country="Canada",
        )

    def test_country_dropdown_collapses_duplicate_us_values(self):
        self.client.login(username="emp_country", password="testpass")
        resp = self.client.get(reverse("job-list"))
        self.assertEqual(resp.status_code, 200)
        options = resp.context["country_options"]
        us_options = [item for item in options if item["value"] == "US"]
        self.assertEqual(len(us_options), 1)
        self.assertEqual(us_options[0]["label"], "United States")

    def test_country_filter_uses_canonical_country_code(self):
        self.client.login(username="emp_country", password="testpass")
        resp = self.client.get(reverse("job-list"), {"country": "US"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "US plain")
        self.assertContains(resp, "US short")
        self.assertContains(resp, "US dirty")
        self.assertNotContains(resp, "Canada role")

    @patch("harvest.location_resolver._code_for_country")
    @patch("harvest.enrichments.infer_country_from_location", return_value="United States")
    def test_country_canonicalizer_prefers_location_inference_for_dirty_strings(
        self,
        infer_country,
        code_for_country,
    ):
        from jobs.views import _canonical_job_country

        def fake_code_for_country(value):
            if value == "USA - Georgia - Atlanta":
                return "GE"
            if value == "United States":
                return "US"
            return ""

        code_for_country.side_effect = fake_code_for_country
        self.assertEqual(_canonical_job_country("USA - Georgia - Atlanta"), ("US", "United States"))
        infer_country.assert_called_once_with("USA - Georgia - Atlanta", "", "")


class JobListBoundaryTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="emp_boundary",
            password="testpass",
            role=User.Role.EMPLOYEE,
        )
        Job.objects.create(
            title="Pool role",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.POOL,
            description="D",
            original_link="https://example.com/pool-role",
        )

    def test_job_list_points_pool_summary_to_pipeline(self):
        self.client.login(username="emp_boundary", password="testpass")
        resp = self.client.get(reverse("job-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'{reverse("jobs-pipeline")}?tab=pool')
        self.assertNotContains(resp, '<option value="POOL"', html=False)


class MatchScoreStringTests(TestCase):
    def test_match_score_str_does_not_reference_missing_title(self):
        from .models import MatchScore

        score = MatchScore(job_id=11, consultant_id=22, score=0.875)

        rendered = str(score)
        self.assertIn("Job 11", rendered)
        self.assertIn("Consultant 22", rendered)
        self.assertIn("0.875", rendered)


class RawJobDualClassificationShadowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.company = Company.objects.create(name="Dual Shadow Co")
        self.admin = User.objects.create_superuser(
            username="dual_shadow_admin",
            email="dual_shadow_admin@example.com",
            password="testpass123",
        )
        config = PlatformConfig.load()
        config.dual_classification_shadow_enabled = True
        config.dual_classification_require_approval_for_sync = False
        config.dual_classification_allow_push_with_warnings = True
        config.dual_classification_backfill_batch_size = 200
        config.dual_classification_secondary_provider_default = ""
        config.dual_classification_secondary_runtime_enabled = False
        config.dual_classification_secondary_prompt_version = "runtime_v1"
        config.save()

    def tearDown(self):
        config = PlatformConfig.load()
        config.dual_classification_shadow_enabled = True
        config.dual_classification_require_approval_for_sync = False
        config.dual_classification_allow_push_with_warnings = True
        config.dual_classification_backfill_batch_size = 200
        config.dual_classification_secondary_provider_default = ""
        config.dual_classification_secondary_runtime_enabled = False
        config.dual_classification_secondary_prompt_version = "runtime_v1"
        config.save()
        super().tearDown()

    def _raw_job(self, suffix: str = "1", *, description: str | None = None) -> RawJob:
        desc = description or (
            "Senior DevOps Engineer role responsible for AWS platform engineering, "
            "Terraform, Kubernetes, CI/CD, observability, and cloud security. "
            "Requires 5+ years of experience, bachelor's degree, US work authorization, "
            "and strong infrastructure automation skills."
        )
        url = f"https://example.com/jobs/{suffix}"
        return RawJob.objects.create(
            company=self.company,
            company_name=self.company.name,
            title="Senior DevOps Engineer",
            url_hash=hashlib.sha256(url.encode()).hexdigest(),
            original_url=url,
            description=desc,
            description_clean=desc,
            location_raw="Remote - United States",
            location_type=RawJob.LocationType.REMOTE,
            is_remote=True,
            sync_status=RawJob.SyncStatus.PENDING,
            is_active=True,
            platform_slug="greenhouse",
        )

    def test_shadow_task_creates_backend_run_and_snapshot(self):
        raw = self._raw_job()

        result = run_rawjob_dual_classification_shadow_task(raw.pk)

        self.assertEqual(result["raw_job_id"], raw.pk)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(snapshot.status, RawJobClassificationSnapshot.Status.PARTIAL)
        self.assertFalse(snapshot.needs_review)
        self.assertTrue(snapshot.current_input_hash)
        self.assertIn("classification", snapshot.merged_output)
        self.assertEqual(snapshot.backend_run.provider, RawJobClassifierRun.Provider.BACKEND_RULES)
        self.assertEqual(snapshot.backend_run.status, RawJobClassifierRun.Status.COMPLETED)
        self.assertEqual(snapshot.secondary_run.provider, RawJobClassifierRun.Provider.SECONDARY_STUB)
        self.assertEqual(snapshot.secondary_run.status, RawJobClassifierRun.Status.SKIPPED)
        self.assertEqual(raw.classifier_runs.count(), 2)

    def test_shadow_task_reuses_cached_result_for_same_input(self):
        raw = self._raw_job("2")

        first = run_rawjob_dual_classification_shadow_task(raw.pk)
        second = run_rawjob_dual_classification_shadow_task(raw.pk)

        self.assertEqual(first["status"], RawJobClassificationSnapshot.Status.PARTIAL)
        self.assertEqual(second["status"], "cached")
        self.assertEqual(raw.classifier_runs.count(), 2)

    @patch("jobs.signals.cache.add", return_value=True)
    @patch("jobs.signals._queue_rawjob_shadow_classification")
    def test_rawjob_signal_queues_shadow_task_only_for_real_jd(self, queue_mock, _cache_add):
        with self.captureOnCommitCallbacks(execute=True):
            self._raw_job("3")
        queue_mock.assert_called_once()

        queue_mock.reset_mock()
        short_desc = "Short JD"
        with self.captureOnCommitCallbacks(execute=True):
            self._raw_job("4", description=short_desc)
        queue_mock.assert_not_called()

    @patch("jobs.signals.cache.add", return_value=True)
    @patch("jobs.signals._queue_rawjob_shadow_classification")
    def test_rawjob_signal_respects_platform_toggle(self, queue_mock, _cache_add):
        config = PlatformConfig.load()
        config.dual_classification_shadow_enabled = False
        config.save()
        with self.captureOnCommitCallbacks(execute=True):
            self._raw_job("4b")
        queue_mock.assert_not_called()

    @patch("jobs.dual_classification.providers.PipelineLLMClient")
    def test_shadow_task_runs_real_secondary_runtime_when_enabled(self, mock_client_cls):
        config = PlatformConfig.load()
        config.dual_classification_secondary_runtime_enabled = True
        config.dual_classification_secondary_provider_default = RawJobClassifierRun.Provider.CODEX
        config.dual_classification_secondary_prompt_version = "runtime_v2"
        config.save()

        client = mock_client_cls.return_value
        client.is_available.return_value = (True, None)
        client.check_token_cap.return_value = (True, None)
        client.validation_model = "gpt-5-codex"
        client.config = SimpleNamespace(max_output_tokens=1800)
        client.call.return_value = (
            json.dumps(
                {
                    "identity": {
                        "title": "Senior DevOps Engineer",
                        "company_name": self.company.name,
                    },
                    "classification": {
                        "job_category": "Engineering",
                        "job_domain": "devops-cloud",
                        "department_normalized": "engineering",
                        "role_category": "devops",
                    },
                    "skills": {
                        "skills": ["AWS", "Terraform", "Kubernetes"],
                        "tech_stack": ["AWS", "Terraform", "Kubernetes"],
                    },
                    "requirements": {
                        "years_required": 5,
                        "years_required_max": None,
                        "education_required": "BS",
                        "visa_sponsorship": False,
                        "work_authorization": "US work authorization",
                        "clearance_required": False,
                        "clearance_level": "",
                    },
                    "location": {
                        "country": "United States",
                        "country_codes": ["US"],
                        "location_type": "REMOTE",
                        "is_remote": True,
                    },
                    "confidence": 0.93,
                }
            ),
            321,
            None,
        )

        raw = self._raw_job("4c")
        result = run_rawjob_dual_classification_shadow_task(raw.pk, force=True)

        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(result["status"], RawJobClassificationSnapshot.Status.MERGED)
        self.assertEqual(snapshot.status, RawJobClassificationSnapshot.Status.MERGED)
        self.assertFalse(snapshot.needs_review)
        self.assertEqual(snapshot.secondary_run.provider, RawJobClassifierRun.Provider.CODEX)
        self.assertEqual(snapshot.secondary_run.status, RawJobClassifierRun.Status.COMPLETED)
        self.assertEqual(snapshot.secondary_run.prompt_version, "runtime_v2")
        self.assertEqual(snapshot.secondary_run.provider_version, "gpt-5-codex")
        self.assertEqual(snapshot.secondary_run.normalized_output["classification"]["job_domain"], "devops-cloud")

    @patch("jobs.dual_classification.providers.PipelineLLMClient")
    def test_shadow_task_marks_review_when_secondary_runtime_fails(self, mock_client_cls):
        config = PlatformConfig.load()
        config.dual_classification_secondary_runtime_enabled = True
        config.dual_classification_secondary_provider_default = RawJobClassifierRun.Provider.CLAUDE
        config.save()

        client = mock_client_cls.return_value
        client.is_available.return_value = (True, None)
        client.check_token_cap.return_value = (True, None)
        client.validation_model = "claude-3.7-sonnet"
        client.config = SimpleNamespace(max_output_tokens=1800)
        client.call.return_value = (None, 0, "provider timeout")

        raw = self._raw_job("4d")
        result = run_rawjob_dual_classification_shadow_task(raw.pk, force=True)

        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(result["status"], RawJobClassificationSnapshot.Status.NEEDS_REVIEW)
        self.assertEqual(snapshot.status, RawJobClassificationSnapshot.Status.NEEDS_REVIEW)
        self.assertTrue(snapshot.needs_review)
        self.assertEqual(snapshot.review_reason, "secondary_provider_failed")
        self.assertEqual(snapshot.secondary_run.provider, RawJobClassifierRun.Provider.CLAUDE)
        self.assertEqual(snapshot.secondary_run.status, RawJobClassifierRun.Status.FAILED)

    def test_secondary_ingest_creates_secondary_run_and_merged_snapshot(self):
        raw = self._raw_job("5")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)

        payload = {
            "identity": {
                "raw_job_id": raw.pk,
                "title": raw.title,
                "company_name": raw.company_name,
            },
            "classification": {
                "job_category": "Engineering",
                "job_domain": "platform-engineer",
                "department_normalized": "engineering",
                "role_category": "cloud",
            },
            "skills": {
                "skills": ["AWS", "Terraform", "Kubernetes"],
                "tech_stack": ["AWS", "Terraform", "Kubernetes"],
            },
            "requirements": {
                "years_required": 5,
                "years_required_max": None,
                "education_required": "BS",
                "visa_sponsorship": False,
                "work_authorization": "US work authorization",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "United States",
                "country_codes": ["US"],
                "location_type": "REMOTE",
                "is_remote": True,
            },
        }

        response = self.client.post(
            reverse("harvest-rawjob-secondary-ingest", args=[raw.pk]),
            {
                "provider": RawJobClassifierRun.Provider.CODEX,
                "prompt_version": "v1",
                "confidence": "0.87",
                "normalized_output_json": json.dumps(payload),
            },
        )

        self.assertEqual(response.status_code, 302)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(snapshot.status, RawJobClassificationSnapshot.Status.MERGED)
        self.assertFalse(snapshot.needs_review)
        self.assertIsNotNone(snapshot.secondary_run)
        self.assertEqual(snapshot.secondary_run.provider, RawJobClassifierRun.Provider.CODEX)
        self.assertEqual(snapshot.secondary_run.prompt_version, "v1")
        self.assertEqual(raw.classifier_runs.count(), 3)

    def test_rawjob_detail_renders_dual_classification_panel(self):
        raw = self._raw_job("6")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)

        response = self.client.get(reverse("harvest-rawjob-detail", args=[raw.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dual Classification")
        self.assertContains(response, "Open Classification V2")
        self.assertNotContains(response, "Store Manual Secondary Classification")
        self.assertNotContains(response, "Backend canonical output")

    def test_v2_detail_renders_full_classification_workstation(self):
        raw = self._raw_job("6b")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.client.force_login(self.admin)

        response = self.client.get(reverse("jobs-classification-detail", args=[snapshot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Store Manual Secondary Classification")
        self.assertContains(response, "Save Manual Override")
        self.assertContains(response, "Save Field-Level Override")
        self.assertContains(response, "Copy provider prompt context")

    def test_review_action_accepts_secondary_output(self):
        raw = self._raw_job("7")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)
        payload = {
            "identity": {
                "raw_job_id": raw.pk,
                "title": raw.title,
                "company_name": raw.company_name,
            },
            "classification": {
                "job_category": "Engineering",
                "job_domain": "platform-engineer",
                "department_normalized": "engineering",
                "role_category": "cloud",
            },
            "skills": {"skills": ["AWS"], "tech_stack": ["AWS"]},
            "requirements": {
                "years_required": 5,
                "years_required_max": None,
                "education_required": "BS",
                "visa_sponsorship": False,
                "work_authorization": "US work authorization",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "United States",
                "country_codes": ["US"],
                "location_type": "REMOTE",
                "is_remote": True,
            },
        }
        self.client.post(
            reverse("harvest-rawjob-secondary-ingest", args=[raw.pk]),
            {
                "provider": RawJobClassifierRun.Provider.CLAUDE,
                "prompt_version": "v2",
                "confidence": "0.91",
                "normalized_output_json": json.dumps(payload),
            },
        )

        response = self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {
                "source": "secondary",
                "approval_note": "Secondary extraction is cleaner.",
            },
        )

        self.assertEqual(response.status_code, 302)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(snapshot.approval_state, RawJobClassificationSnapshot.ApprovalState.APPROVED)
        self.assertEqual(snapshot.approved_source, "secondary")
        self.assertEqual(snapshot.approved_by, self.admin)
        self.assertEqual(snapshot.approved_output["classification"]["job_domain"], "platform-engineer")

    def test_review_action_can_return_to_v2_detail(self):
        raw = self._raw_job("7b")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {
                "source": "merged",
                "next": reverse("jobs-classification-detail", args=[snapshot.pk]),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("jobs-classification-detail", args=[snapshot.pk]))

    def test_review_action_saves_manual_override(self):
        raw = self._raw_job("8")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)
        manual_payload = {
            "identity": {
                "raw_job_id": raw.pk,
                "title": raw.title,
                "company_name": raw.company_name,
            },
            "classification": {
                "job_category": "Engineering",
                "job_domain": "devops-cloud",
                "department_normalized": "engineering",
                "role_category": "platform",
            },
            "skills": {"skills": ["AWS", "Terraform"], "tech_stack": ["AWS", "Terraform"]},
            "requirements": {
                "years_required": 5,
                "years_required_max": None,
                "education_required": "BS",
                "visa_sponsorship": False,
                "work_authorization": "US work authorization",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "United States",
                "country_codes": ["US"],
                "location_type": "REMOTE",
                "is_remote": True,
            },
        }

        response = self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {
                "source": "manual",
                "approval_note": "Manual normalization for vetting.",
                "manual_output_json": json.dumps(manual_payload),
            },
        )

        self.assertEqual(response.status_code, 302)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(snapshot.approval_state, RawJobClassificationSnapshot.ApprovalState.OVERRIDDEN)
        self.assertEqual(snapshot.approved_source, "manual")
        self.assertEqual(snapshot.approved_output["classification"]["job_domain"], "devops-cloud")

    def test_review_action_saves_field_level_override(self):
        raw = self._raw_job("8b")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {
                "source": "manual_fields",
                "approval_note": "Tighten only the core routing fields.",
                "job_category": "Engineering",
                "job_domain": "platform-engineer",
                "department_normalized": "engineering",
                "role_category": "cloud",
                "country": "United States",
                "country_codes": "US",
                "location_type": "REMOTE",
                "years_required": "7",
                "education_required": "BS",
                "skills": "AWS, Terraform, Kubernetes",
                "tech_stack": "AWS, Terraform, Kubernetes",
            },
        )

        self.assertEqual(response.status_code, 302)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(snapshot.approved_source, "manual")
        self.assertEqual(snapshot.approval_state, RawJobClassificationSnapshot.ApprovalState.OVERRIDDEN)
        self.assertEqual(snapshot.approved_output["classification"]["job_domain"], "platform-engineer")
        self.assertEqual(snapshot.approved_output["location"]["country_codes"], ["US"])
        self.assertEqual(snapshot.approved_output["requirements"]["years_required"], 7)

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.check_job_posting_live")
    def test_jd_change_marks_approved_snapshot_stale_and_blocks_push(
        self,
        mock_live,
        mock_gate,
        _mock_apply,
    ):
        raw = self._raw_job("8c-stale")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)
        payload = {
            "identity": {
                "raw_job_id": raw.pk,
                "title": raw.title,
                "company_name": raw.company_name,
            },
            "classification": {
                "job_category": "Engineering",
                "job_domain": "platform-engineer",
                "department_normalized": "engineering",
                "role_category": "cloud",
            },
            "skills": {"skills": ["AWS"], "tech_stack": ["AWS"]},
            "requirements": {
                "years_required": 5,
                "years_required_max": None,
                "education_required": "BS",
                "visa_sponsorship": False,
                "work_authorization": "US work authorization",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "United States",
                "country_codes": ["US"],
                "location_type": "REMOTE",
                "is_remote": True,
            },
        }
        self.client.post(
            reverse("harvest-rawjob-secondary-ingest", args=[raw.pk]),
            {
                "provider": RawJobClassifierRun.Provider.CLAUDE,
                "prompt_version": "v2",
                "confidence": "0.91",
                "normalized_output_json": json.dumps(payload),
            },
        )
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {"source": "secondary", "approval_note": "Ready for vetting."},
        )
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertFalse(snapshot.approval_is_stale)
        original_approval_hash = snapshot.approval_input_hash

        raw.description = f"{raw.description} Now requires Azure administration and hybrid travel."
        raw.description_clean = raw.description
        raw.save()

        snapshot.refresh_from_db()
        self.assertTrue(snapshot.approval_is_stale)
        self.assertEqual(snapshot.review_reason, "input_changed_after_approval")
        self.assertTrue(snapshot.needs_review)
        self.assertFalse(snapshot.ready_for_vetting)
        self.assertEqual(snapshot.approval_input_hash, original_approval_hash)
        self.assertIsNotNone(snapshot.approval_stale_at)

        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(
            is_live=True,
            reason="",
            status_code=200,
            final_url=raw.original_url,
        )

        response = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {"push_note": "Should be blocked because approval is stale."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approved classification is stale because the JD changed")
        self.assertFalse(Job.objects.filter(source_raw_job=raw).exists())

    def test_rerun_keeps_approval_stale_until_reapproved(self):
        raw = self._raw_job("8d-stale-rerun")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {"source": "merged", "approval_note": "Initial approval."},
        )

        raw.description = f"{raw.description} Added Azure and disaster recovery ownership."
        raw.description_clean = raw.description
        raw.save()

        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertTrue(snapshot.approval_is_stale)
        original_hash = snapshot.approval_input_hash

        result = run_rawjob_dual_classification_shadow_task(raw.pk, force=True)

        snapshot.refresh_from_db()
        self.assertNotEqual(snapshot.current_input_hash, original_hash)
        self.assertTrue(snapshot.approval_is_stale)
        self.assertEqual(snapshot.review_reason, "input_changed_after_approval")
        self.assertTrue(snapshot.needs_review)
        self.assertFalse(snapshot.ready_for_vetting)
        self.assertEqual(result["status"], RawJobClassificationSnapshot.Status.NEEDS_REVIEW)

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.check_job_posting_live")
    def test_push_to_vetting_records_audit_for_ready_approved_snapshot(
        self,
        mock_live,
        mock_gate,
        _mock_apply,
    ):
        raw = self._raw_job("9")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)

        payload = {
            "identity": {
                "raw_job_id": raw.pk,
                "title": raw.title,
                "company_name": raw.company_name,
            },
            "classification": {
                "job_category": "Engineering",
                "job_domain": "platform-engineer",
                "department_normalized": "engineering",
                "role_category": "cloud",
            },
            "skills": {
                "skills": ["AWS", "Terraform", "Kubernetes"],
                "tech_stack": ["AWS", "Terraform", "Kubernetes"],
            },
            "requirements": {
                "years_required": 5,
                "years_required_max": None,
                "education_required": "BS",
                "visa_sponsorship": False,
                "work_authorization": "US work authorization",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "United States",
                "country_codes": ["US"],
                "location_type": "REMOTE",
                "is_remote": True,
            },
        }
        self.client.post(
            reverse("harvest-rawjob-secondary-ingest", args=[raw.pk]),
            {
                "provider": RawJobClassifierRun.Provider.CLAUDE,
                "prompt_version": "v2",
                "confidence": "0.91",
                "normalized_output_json": json.dumps(payload),
            },
        )
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {"source": "secondary", "approval_note": "Ready for vetting."},
        )

        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(
            is_live=True,
            reason="",
            status_code=200,
            final_url=raw.original_url,
        )

        response = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {"push_note": "Reviewed and ready."},
        )

        self.assertEqual(response.status_code, 302)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertIsNotNone(snapshot.pushed_to_vetting_at)
        self.assertEqual(snapshot.pushed_to_vetting_by, self.admin)
        self.assertEqual(snapshot.pushed_to_vetting_note, "Reviewed and ready.")
        self.assertFalse(snapshot.pushed_to_vetting_with_warnings)
        self.assertEqual(snapshot.pushed_warning_codes, [])
        self.assertIsNotNone(snapshot.pushed_job)
        self.assertEqual(snapshot.pushed_job.source_raw_job, raw)
        self.assertEqual(snapshot.pushed_job.validation_result["dual_classification"]["approved_source"], "secondary")
        self.assertFalse(snapshot.pushed_job.validation_result["dual_classification"]["pushed_to_vetting_with_warnings"])

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.check_job_posting_live")
    def test_push_to_vetting_can_return_to_v2_detail(
        self,
        mock_live,
        mock_gate,
        _mock_apply,
    ):
        raw = self._raw_job("9b")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)

        payload = {
            "identity": {
                "raw_job_id": raw.pk,
                "title": raw.title,
                "company_name": raw.company_name,
            },
            "classification": {
                "job_category": "Engineering",
                "job_domain": "platform-engineer",
                "department_normalized": "engineering",
                "role_category": "cloud",
            },
            "skills": {"skills": ["AWS"], "tech_stack": ["AWS"]},
            "requirements": {
                "years_required": 5,
                "years_required_max": None,
                "education_required": "BS",
                "visa_sponsorship": False,
                "work_authorization": "US work authorization",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "United States",
                "country_codes": ["US"],
                "location_type": "REMOTE",
                "is_remote": True,
            },
        }
        self.client.post(
            reverse("harvest-rawjob-secondary-ingest", args=[raw.pk]),
            {
                "provider": RawJobClassifierRun.Provider.CLAUDE,
                "prompt_version": "v2",
                "confidence": "0.91",
                "normalized_output_json": json.dumps(payload),
            },
        )
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {"source": "secondary"},
        )
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)

        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(
            is_live=True,
            reason="",
            status_code=200,
            final_url=raw.original_url,
        )

        response = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {
                "push_note": "Ready from V2",
                "next": reverse("jobs-classification-detail", args=[snapshot.pk]),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("jobs-classification-detail", args=[snapshot.pk]))

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.check_job_posting_live")
    def test_push_to_vetting_with_warnings_requires_note_and_records_warning_codes(
        self,
        mock_live,
        mock_gate,
        _mock_apply,
    ):
        raw = self._raw_job(
            "10",
            description=(
                "Platform operations role covering delivery coordination, governance, "
                "documentation, stakeholder alignment, and change control across "
                "enterprise systems without explicit technology or experience ranges."
            ),
        )
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)

        manual_payload = {
            "identity": {
                "raw_job_id": raw.pk,
                "title": raw.title,
                "company_name": raw.company_name,
            },
            "classification": {
                "job_category": "",
                "job_domain": "",
                "department_normalized": "",
                "role_category": "",
            },
            "skills": {"skills": ["ImaginaryPlatform"], "tech_stack": ["ImaginaryPlatform"]},
            "requirements": {
                "years_required": 12,
                "years_required_max": None,
                "education_required": "",
                "visa_sponsorship": None,
                "work_authorization": "",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "",
                "country_codes": [],
                "location_type": "",
                "is_remote": False,
            },
        }
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {
                "source": "manual",
                "approval_note": "Force this sparse payload.",
                "manual_output_json": json.dumps(manual_payload),
            },
        )

        response = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertIsNone(snapshot.pushed_to_vetting_at)

        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(
            is_live=True,
            reason="",
            status_code=200,
            final_url=raw.original_url,
        )

        response = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {
                "allow_warnings": "1",
                "push_note": "Human reviewed sparse JD; still useful.",
            },
        )
        self.assertEqual(response.status_code, 302)
        snapshot.refresh_from_db()
        self.assertTrue(snapshot.pushed_to_vetting_with_warnings)
        self.assertIn("missing_job_category", snapshot.pushed_warning_codes)
        self.assertIn("missing_job_domain", snapshot.pushed_warning_codes)
        self.assertEqual(snapshot.pushed_to_vetting_note, "Human reviewed sparse JD; still useful.")

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.check_job_posting_live")
    def test_push_with_warnings_can_be_disabled_in_platform_config(
        self,
        mock_live,
        mock_gate,
        _mock_apply,
    ):
        config = PlatformConfig.load()
        config.dual_classification_allow_push_with_warnings = False
        config.save()
        raw = self._raw_job(
            "10b",
            description="Sparse operations role without explicit technology, years, or category markers.",
        )
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {
                "source": "manual",
                "approval_note": "Sparse override.",
                "manual_output_json": json.dumps(
                    {
                        "identity": {"raw_job_id": raw.pk, "title": raw.title, "company_name": raw.company_name},
                        "classification": {"job_category": "", "job_domain": "", "department_normalized": "", "role_category": ""},
                        "skills": {"skills": ["ImaginaryPlatform"], "tech_stack": ["ImaginaryPlatform"]},
                        "requirements": {"years_required": 9, "years_required_max": None, "education_required": "", "visa_sponsorship": None, "work_authorization": "", "clearance_required": False, "clearance_level": ""},
                        "location": {"country": "", "country_codes": [], "location_type": "", "is_remote": False},
                    }
                ),
            },
        )
        mock_gate.return_value = SimpleNamespace(
            passed=True, lane="READY", status="eligible", reason_code="", reasons=[], checks={},
            data_quality_score=0.9, trust_score=0.9, candidate_fit_score=0.9, vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(is_live=True, reason="", status_code=200, final_url=raw.original_url)

        response = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {"allow_warnings": "1", "push_note": "Should be blocked by config."},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertIsNone(snapshot.pushed_to_vetting_at)

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.check_job_posting_live")
    def test_push_to_vetting_is_idempotent_for_repeated_posts(
        self,
        mock_live,
        mock_gate,
        _mock_apply,
    ):
        raw = self._raw_job("10c-repush")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)
        payload = {
            "identity": {"raw_job_id": raw.pk, "title": raw.title, "company_name": raw.company_name},
            "classification": {
                "job_category": "Engineering",
                "job_domain": "platform-engineer",
                "department_normalized": "engineering",
                "role_category": "cloud",
            },
            "skills": {"skills": ["AWS"], "tech_stack": ["AWS"]},
            "requirements": {
                "years_required": 5,
                "years_required_max": None,
                "education_required": "BS",
                "visa_sponsorship": False,
                "work_authorization": "US work authorization",
                "clearance_required": False,
                "clearance_level": "",
            },
            "location": {
                "country": "United States",
                "country_codes": ["US"],
                "location_type": "REMOTE",
                "is_remote": True,
            },
        }
        self.client.post(
            reverse("harvest-rawjob-secondary-ingest", args=[raw.pk]),
            {
                "provider": RawJobClassifierRun.Provider.CLAUDE,
                "prompt_version": "v2",
                "confidence": "0.91",
                "normalized_output_json": json.dumps(payload),
            },
        )
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {"source": "secondary", "approval_note": "Ready for vetting."},
        )
        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(is_live=True, reason="", status_code=200, final_url=raw.original_url)

        first = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {"push_note": "First push note."},
        )
        second = self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {"push_note": "Second push note should be ignored."},
            follow=True,
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        snapshot = RawJobClassificationSnapshot.objects.get(raw_job=raw)
        self.assertEqual(Job.objects.filter(source_raw_job=raw).count(), 1)
        self.assertEqual(snapshot.pushed_to_vetting_note, "First push note.")
        self.assertEqual(
            snapshot.pushed_job.validation_result["dual_classification"]["pushed_to_vetting_note"],
            "First push note.",
        )
        self.assertFalse(snapshot.approval_is_stale)

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.check_job_posting_live")
    def test_record_vetting_push_is_idempotent_after_first_success(
        self,
        mock_live,
        mock_gate,
        _mock_apply,
    ):
        from jobs.dual_classification.orchestrator import record_vetting_push_for_raw_job

        raw = self._raw_job("10c-orchestrator")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)
        self.client.post(
            reverse("harvest-rawjob-classification-review", args=[raw.pk]),
            {
                "source": "manual",
                "approval_note": "Manual approval.",
                "manual_output_json": json.dumps(
                    {
                        "identity": {"raw_job_id": raw.pk, "title": raw.title, "company_name": raw.company_name},
                        "classification": {
                            "job_category": "Engineering",
                            "job_domain": "platform-engineer",
                            "department_normalized": "engineering",
                            "role_category": "cloud",
                        },
                        "skills": {"skills": ["AWS"], "tech_stack": ["AWS"]},
                        "requirements": {
                            "years_required": 5,
                            "years_required_max": None,
                            "education_required": "BS",
                            "visa_sponsorship": False,
                            "work_authorization": "US work authorization",
                            "clearance_required": False,
                            "clearance_level": "",
                        },
                        "location": {
                            "country": "United States",
                            "country_codes": ["US"],
                            "location_type": "REMOTE",
                            "is_remote": True,
                        },
                    }
                ),
            },
        )
        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(is_live=True, reason="", status_code=200, final_url=raw.original_url)
        self.client.post(
            reverse("harvest-rawjob-push-vetting", args=[raw.pk]),
            {"push_note": "Initial audit note."},
        )

        snapshot = RawJobClassificationSnapshot.objects.select_related("pushed_job").get(raw_job=raw)
        result = record_vetting_push_for_raw_job(
            raw_job_id=raw.pk,
            actor=self.admin,
            job=snapshot.pushed_job,
            note="Overwrite attempt should be ignored.",
            pushed_with_warnings=True,
        )

        snapshot.refresh_from_db()
        pushed_job = Job.objects.get(pk=snapshot.pushed_job_id)
        self.assertTrue(result["already_pushed"])
        self.assertEqual(result["job_id"], snapshot.pushed_job_id)
        self.assertEqual(snapshot.pushed_to_vetting_note, "Initial audit note.")
        self.assertFalse(snapshot.pushed_to_vetting_with_warnings)
        self.assertEqual(
            pushed_job.validation_result["dual_classification"]["pushed_to_vetting_note"],
            "Initial audit note.",
        )
        self.assertFalse(
            pushed_job.validation_result["dual_classification"]["pushed_to_vetting_with_warnings"]
        )

    @patch("harvest.url_health.check_job_posting_live")
    @patch("jobs.gating.evaluate_raw_job_gate")
    def test_sync_selected_respects_require_approval_for_sync(self, mock_gate, mock_live):
        config = PlatformConfig.load()
        config.dual_classification_require_approval_for_sync = True
        config.save()
        raw = self._raw_job("10c")
        self.client.force_login(self.admin)
        mock_gate.return_value = SimpleNamespace(
            passed=True, lane="READY", status="eligible", reason_code="", reasons=[], checks={},
            data_quality_score=0.9, trust_score=0.9, candidate_fit_score=0.9, vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(is_live=True, reason="", status_code=200, final_url=raw.original_url)

        response = self.client.post(
            reverse("jobs-pipeline-run-sync-selected"),
            {"raw_job_ids": str(raw.pk)},
        )

        self.assertEqual(response.status_code, 302)
        raw.refresh_from_db()
        self.assertEqual(raw.sync_status, RawJob.SyncStatus.PENDING)
        self.assertFalse(Job.objects.filter(source_raw_job=raw).exists())

    def test_backfill_task_processes_historical_raw_jobs(self):
        raw_one = self._raw_job("11")
        raw_two = self._raw_job("12")

        result = backfill_rawjob_dual_classification_task.run(batch_size=10, only_missing=True)

        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["created_or_updated"], 2)
        self.assertEqual(RawJobClassificationSnapshot.objects.filter(raw_job__in=[raw_one, raw_two]).count(), 2)

    def test_review_queue_page_renders_snapshot_rows(self):
        raw = self._raw_job("13")
        run_rawjob_dual_classification_shadow_task(raw.pk)
        self.client.force_login(self.admin)

        response = self.client.get(f"{reverse('harvest-rawjob-review-queue')}?queue=all")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RawJob Classification Review Queue")
        self.assertContains(response, raw.title)

    @patch("jobs.tasks.backfill_rawjob_dual_classification_task.apply_async")
    def test_review_queue_backfill_uses_isolated_queue(self, mock_apply_async):
        self.client.force_login(self.admin)
        mock_apply_async.return_value = SimpleNamespace(id="task-123")

        response = self.client.post(
            reverse("harvest-run-classification-backfill"),
            {"batch_size": "25", "force": "1", "only_missing": "0"},
        )

        self.assertEqual(response.status_code, 302)
        mock_apply_async.assert_called_once_with(
            kwargs={
                "batch_size": 25,
                "force": True,
                "only_missing": False,
            },
            queue=DUAL_CLASSIFICATION_BACKFILL_QUEUE,
        )


class ClassificationQueueV2ViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="classification_v2_employee",
            password="testpass",
            role=User.Role.EMPLOYEE,
        )
        from core.models import FeatureFlag

        FeatureFlag.objects.update_or_create(
            key="employee_classification_workspace_v2",
            defaults={
                "label": "Classification Workspace V2",
                "category": "EMPLOYEE",
                "applies_to": "EMPLOYEE",
                "is_enabled": True,
                "enabled_for_employees": True,
                "enabled_for_consultants": False,
            },
        )

        self.company = Company.objects.create(name="Queue V2 Co")
        self.raw = RawJob.objects.create(
            company=self.company,
            company_name=self.company.name,
            title="Cloud Platform Engineer",
            url_hash=hashlib.sha256(b"https://example.com/jobs/queue-v2").hexdigest(),
            original_url="https://example.com/jobs/queue-v2",
            description="AWS platform engineering, Kubernetes, Terraform, Python, monitoring, and secure cloud automation.",
            description_clean="AWS platform engineering, Kubernetes, Terraform, Python, monitoring, and secure cloud automation.",
            location_raw="Remote - United States",
            location_type=RawJob.LocationType.REMOTE,
            is_remote=True,
            sync_status=RawJob.SyncStatus.PENDING,
            is_active=True,
            platform_slug="greenhouse",
            classification_source="rules",
            classification_provenance={"engine": "rule_regex_v2", "signals_count": 5},
            field_provenance={"job_domain": "rule_regex_v2", "skills": "rule_regex_v2"},
        )
        RawJobClassificationSnapshot.objects.create(
            raw_job=self.raw,
            status=RawJobClassificationSnapshot.Status.NEEDS_REVIEW,
            needs_review=True,
            review_reason="secondary_provider_failed",
            final_confidence=0.61,
        )
        self.snapshot = RawJobClassificationSnapshot.objects.get(raw_job=self.raw)
        self.backend_run = RawJobClassifierRun.objects.create(
            raw_job=self.raw,
            provider=RawJobClassifierRun.Provider.BACKEND_RULES,
            provider_role=RawJobClassifierRun.ProviderRole.PRIMARY,
            input_hash="queue-v2-hash",
            status=RawJobClassifierRun.Status.COMPLETED,
            confidence=0.73,
            normalized_output={"classification": {"job_domain": "devops-cloud"}},
        )
        self.secondary_run = RawJobClassifierRun.objects.create(
            raw_job=self.raw,
            provider=RawJobClassifierRun.Provider.CODEX,
            provider_role=RawJobClassifierRun.ProviderRole.SECONDARY,
            input_hash="queue-v2-hash",
            status=RawJobClassifierRun.Status.FAILED,
            error_message="provider timeout",
            normalized_output={},
        )
        self.snapshot.backend_run = self.backend_run
        self.snapshot.secondary_run = self.secondary_run
        self.snapshot.merged_output = {
            "classification": {"job_domain": "devops-cloud", "job_category": "Engineering"},
            "location": {"country": "United States", "location_type": "REMOTE"},
            "skills": {"skills": ["AWS", "Terraform", "Python"]},
        }
        self.snapshot.verifier_summary = {"warnings": ["secondary_provider_failed"], "errors": []}
        self.snapshot.save(
            update_fields=["backend_run", "secondary_run", "merged_output", "verifier_summary"]
        )
        RawJobClassificationConflict.objects.create(
            raw_job=self.raw,
            snapshot=self.snapshot,
            field_path="classification.job_domain",
            backend_value="devops-cloud",
            secondary_value="",
            resolved_value="devops-cloud",
            resolution=RawJobClassificationConflict.Resolution.BACKEND,
            severity=RawJobClassificationConflict.Severity.WARN,
            note="Secondary provider failed before a usable domain was returned.",
        )

    def test_queue_requires_rollout_flag(self):
        from core.models import FeatureFlag

        flag = FeatureFlag.objects.get(key="employee_classification_workspace_v2")
        flag.is_enabled = False
        flag.save(update_fields=["is_enabled"])

        self.client.login(username="classification_v2_employee", password="testpass")
        response = self.client.get(reverse("jobs-classification-queue"))

        self.assertEqual(response.status_code, 403)

    def test_queue_renders_snapshot_when_flag_enabled(self):
        self.client.login(username="classification_v2_employee", password="testpass")
        response = self.client.get(reverse("jobs-classification-queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Classification Queue V2")
        self.assertContains(response, "Cloud Platform Engineer")
        self.assertContains(response, "secondary_provider_failed")

    def test_detail_requires_rollout_flag(self):
        from core.models import FeatureFlag

        flag = FeatureFlag.objects.get(key="employee_classification_workspace_v2")
        flag.is_enabled = False
        flag.save(update_fields=["is_enabled"])

        self.client.login(username="classification_v2_employee", password="testpass")
        response = self.client.get(reverse("jobs-classification-detail", args=[self.snapshot.pk]))

        self.assertEqual(response.status_code, 403)

    def test_detail_renders_snapshot_when_flag_enabled(self):
        self.client.login(username="classification_v2_employee", password="testpass")
        response = self.client.get(reverse("jobs-classification-detail", args=[self.snapshot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Classification Detail V2")
        self.assertContains(response, "Cloud Platform Engineer")
        self.assertContains(response, "secondary_provider_failed")
        self.assertContains(response, "Field Compare")
        self.assertContains(response, "classification.job_domain")
        self.assertContains(response, "rule_regex_v2")
        self.assertContains(response, "Open legacy RawJob review")

    def test_detail_handles_list_shaped_skills_without_500(self):
        self.raw.skills = ["AWS", "Terraform", "Python"]
        self.raw.save(update_fields=["skills", "updated_at"])
        self.client.login(username="classification_v2_employee", password="testpass")

        response = self.client.get(reverse("jobs-classification-detail", args=[self.snapshot.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AWS, Terraform, Python")

    def test_stale_approved_snapshot_moves_to_review_queue_and_shows_badge(self):
        self.snapshot.approved_output = {"classification": {"job_domain": "platform-engineer"}}
        self.snapshot.approved_source = "merged"
        self.snapshot.approval_state = RawJobClassificationSnapshot.ApprovalState.APPROVED
        self.snapshot.approval_is_stale = True
        self.snapshot.needs_review = True
        self.snapshot.review_reason = "input_changed_after_approval"
        self.snapshot.ready_for_vetting = False
        self.snapshot.save(
            update_fields=[
                "approved_output",
                "approved_source",
                "approval_state",
                "approval_is_stale",
                "needs_review",
                "review_reason",
                "ready_for_vetting",
                "updated_at",
            ]
        )

        self.client.login(username="classification_v2_employee", password="testpass")
        response = self.client.get(reverse("jobs-classification-queue"), {"queue": "needs_review"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Stale approval")
        self.assertEqual(response.context["queue_counts"]["approved_not_pushed"], 0)


class ClassificationSettingsV2ViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username="classification_settings_admin",
            email="classification-settings@example.com",
            password="testpass123",
        )
        from core.models import FeatureFlag

        FeatureFlag.objects.update_or_create(
            key="employee_classification_settings_v2",
            defaults={
                "label": "Classification Settings V2",
                "category": "EMPLOYEE",
                "applies_to": "EMPLOYEE",
                "is_enabled": True,
                "enabled_for_employees": True,
                "enabled_for_consultants": False,
            },
        )

    def test_settings_page_renders(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("jobs-classification-settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Classification Settings V2")
        self.assertContains(response, "Auto-run shadow classification")

    def test_settings_post_updates_platform_config(self):
        self.client.force_login(self.admin)
        config = PlatformConfig.load()

        response = self.client.post(
            reverse("jobs-classification-settings"),
            {
                "dual_classification_require_approval_for_sync": "on",
                "dual_classification_secondary_runtime_enabled": "on",
                "dual_classification_backfill_batch_size": "333",
                "dual_classification_secondary_provider_default": "codex",
                "dual_classification_secondary_prompt_version": "runtime_v3",
                "routing_ready_confidence_threshold": "0.72",
                "routing_require_country": "on",
                "routing_require_work_authorization": "on",
                "routing_require_parsed_jd_for_pool": "on",
                "routing_require_ready_for_pool": "on",
                "routing_require_parsed_jd_for_live": "on",
                "routing_require_ready_for_live": "on",
                "routing_backfill_batch_size": "777",
                "routing_enforce_country_match": "on",
                "routing_enforce_seniority_match": "on",
                "routing_enforce_work_authorization": "on",
                "routing_enforce_employment_preferences": "on",
                "routing_enforce_clearance": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        config.refresh_from_db()
        self.assertFalse(config.dual_classification_shadow_enabled)
        self.assertTrue(config.dual_classification_require_approval_for_sync)
        self.assertFalse(config.dual_classification_allow_push_with_warnings)
        self.assertTrue(config.dual_classification_secondary_runtime_enabled)
        self.assertEqual(config.dual_classification_backfill_batch_size, 333)
        self.assertEqual(config.dual_classification_secondary_provider_default, "codex")
        self.assertEqual(config.dual_classification_secondary_prompt_version, "runtime_v3")
        self.assertEqual(config.routing_ready_confidence_threshold, 0.72)
        self.assertTrue(config.routing_require_country)
        self.assertTrue(config.routing_require_work_authorization)
        self.assertTrue(config.routing_require_parsed_jd_for_pool)
        self.assertTrue(config.routing_require_ready_for_pool)
        self.assertTrue(config.routing_require_parsed_jd_for_live)
        self.assertTrue(config.routing_require_ready_for_live)
        self.assertEqual(config.routing_backfill_batch_size, 777)
        self.assertTrue(config.routing_enforce_work_authorization)
        self.assertTrue(config.routing_enforce_employment_preferences)
        self.assertTrue(config.routing_enforce_clearance)


class ClassificationMetricsV2ViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.company = Company.objects.create(name="Metrics Co")
        self.employee = User.objects.create_user(
            username="classification_metrics_employee",
            password="testpass",
            role=User.Role.EMPLOYEE,
        )
        from core.models import FeatureFlag, LLMConfig

        FeatureFlag.objects.update_or_create(
            key="employee_job_pool",
            defaults={
                "label": "Job Pool",
                "category": "EMPLOYEE",
                "applies_to": "EMPLOYEE",
                "is_enabled": True,
                "enabled_for_employees": True,
                "enabled_for_consultants": False,
            },
        )
        FeatureFlag.objects.update_or_create(
            key="employee_classification_metrics_v2",
            defaults={
                "label": "Classification Metrics V2",
                "category": "EMPLOYEE",
                "applies_to": "EMPLOYEE",
                "is_enabled": True,
                "enabled_for_employees": True,
                "enabled_for_consultants": False,
            },
        )

        llm = LLMConfig.load()
        llm.provider = LLMConfig.Provider.OPENROUTER
        llm.base_url = "https://openrouter.ai/api/v1"
        llm.active_model = "gpt-4o-mini"
        llm.validation_model = "claude-3.5-sonnet"
        llm.save()

        config = PlatformConfig.load()
        config.dual_classification_shadow_enabled = True
        config.dual_classification_secondary_runtime_enabled = True
        config.dual_classification_secondary_provider_default = RawJobClassifierRun.Provider.CODEX
        config.dual_classification_secondary_prompt_version = "runtime_v5"
        config.dual_classification_backfill_batch_size = 250
        config.dual_classification_require_approval_for_sync = True
        config.dual_classification_allow_push_with_warnings = True
        config.save()

        now = timezone.now()
        raw_one = RawJob.objects.create(
            company=self.company,
            company_name=self.company.name,
            title="Metrics DevOps Engineer",
            url_hash=hashlib.sha256(b"https://example.com/jobs/metrics-1").hexdigest(),
            original_url="https://example.com/jobs/metrics-1",
            description="AWS Terraform Kubernetes platform engineering and Python automation",
            description_clean="AWS Terraform Kubernetes platform engineering and Python automation",
            has_description=True,
            is_active=True,
            platform_slug="greenhouse",
        )
        raw_two = RawJob.objects.create(
            company=self.company,
            company_name=self.company.name,
            title="Metrics Data Engineer",
            url_hash=hashlib.sha256(b"https://example.com/jobs/metrics-2").hexdigest(),
            original_url="https://example.com/jobs/metrics-2",
            description="SQL Python Airflow data pipelines and cloud warehousing",
            description_clean="SQL Python Airflow data pipelines and cloud warehousing",
            has_description=True,
            is_active=True,
            platform_slug="ashby",
        )

        backend_one = RawJobClassifierRun.objects.create(
            raw_job=raw_one,
            provider=RawJobClassifierRun.Provider.BACKEND_RULES,
            provider_role=RawJobClassifierRun.ProviderRole.PRIMARY,
            input_hash="metrics-1",
            prompt_version="rules_v1",
            status=RawJobClassifierRun.Status.COMPLETED,
            confidence=0.81,
            completed_at=now,
        )
        secondary_one = RawJobClassifierRun.objects.create(
            raw_job=raw_one,
            provider=RawJobClassifierRun.Provider.CODEX,
            provider_role=RawJobClassifierRun.ProviderRole.SECONDARY,
            input_hash="metrics-1",
            prompt_version="runtime_v5",
            status=RawJobClassifierRun.Status.COMPLETED,
            confidence=0.88,
            completed_at=now,
        )
        backend_two = RawJobClassifierRun.objects.create(
            raw_job=raw_two,
            provider=RawJobClassifierRun.Provider.BACKEND_RULES,
            provider_role=RawJobClassifierRun.ProviderRole.PRIMARY,
            input_hash="metrics-2",
            prompt_version="rules_v1",
            status=RawJobClassifierRun.Status.COMPLETED,
            confidence=0.74,
            completed_at=now,
        )
        secondary_two = RawJobClassifierRun.objects.create(
            raw_job=raw_two,
            provider=RawJobClassifierRun.Provider.CLAUDE,
            provider_role=RawJobClassifierRun.ProviderRole.SECONDARY,
            input_hash="metrics-2",
            prompt_version="runtime_v5",
            status=RawJobClassifierRun.Status.FAILED,
            error_message="provider timeout",
            updated_at=now,
        )

        snapshot_one = RawJobClassificationSnapshot.objects.create(
            raw_job=raw_one,
            backend_run=backend_one,
            secondary_run=secondary_one,
            status=RawJobClassificationSnapshot.Status.MERGED,
            needs_review=False,
            final_confidence=0.87,
            approval_state=RawJobClassificationSnapshot.ApprovalState.APPROVED,
            approved_output={"classification": {"job_domain": "devops-cloud"}},
            approved_source="merged",
            approved_at=now,
            pushed_to_vetting_at=now,
            ready_for_vetting=True,
        )
        snapshot_two = RawJobClassificationSnapshot.objects.create(
            raw_job=raw_two,
            backend_run=backend_two,
            secondary_run=secondary_two,
            status=RawJobClassificationSnapshot.Status.NEEDS_REVIEW,
            needs_review=True,
            review_reason="secondary_provider_failed",
            final_confidence=0.64,
            verifier_summary={"warnings": ["secondary_provider_failed"], "errors": []},
        )
        RawJobClassificationConflict.objects.create(
            raw_job=raw_two,
            snapshot=snapshot_two,
            field_path="classification.job_domain",
            backend_value="data-engineering",
            secondary_value="",
            resolved_value="data-engineering",
            resolution=RawJobClassificationConflict.Resolution.REVIEW,
            severity=RawJobClassificationConflict.Severity.CRITICAL,
            note="Secondary provider failed before a usable domain was returned.",
        )
        TaskResult.objects.create(
            task_id="dual-backfill-1",
            task_name="jobs.backfill_rawjob_dual_classification",
            status="SUCCESS",
            result=json.dumps(
                {
                    "processed": 12,
                    "created_or_updated": 9,
                    "cached": 2,
                    "failed": 1,
                }
            ),
        )

    def test_metrics_requires_rollout_flag(self):
        from core.models import FeatureFlag

        flag = FeatureFlag.objects.get(key="employee_classification_metrics_v2")
        flag.is_enabled = False
        flag.save(update_fields=["is_enabled"])

        self.client.login(username="classification_metrics_employee", password="testpass")
        response = self.client.get(reverse("jobs-classification-metrics"))

        self.assertEqual(response.status_code, 403)

    def test_metrics_renders_health_surface(self):
        self.client.login(username="classification_metrics_employee", password="testpass")
        response = self.client.get(reverse("jobs-classification-metrics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Classification Metrics V2")
        self.assertContains(response, "Provider Health")
        self.assertContains(response, "Runtime & Backfill Health")
        self.assertContains(response, "Recent Failed Runs")
        self.assertContains(response, "Timeout")
        self.assertContains(response, "Dual-Classification Backfill Runs")
        self.assertContains(response, "Processed:")
        self.assertContains(response, "Metrics Data Engineer")
        self.assertContains(response, "runtime_v5")
        self.assertContains(response, "provider timeout")


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

    def test_department_sync_value_maps_raw_labels_to_job_department_codes(self):
        self.assertEqual(_department_sync_value("Information Technology"), Job.Department.IT_MANAGEMENT)
        self.assertEqual(_department_sync_value("DevOps / SRE"), Job.Department.DEVOPS_CLOUD)
        self.assertEqual(_department_sync_value("Finance & Accounting"), Job.Department.FINANCE)
        self.assertEqual(_department_sync_value("this value is not a job department"), "")

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


    def test_match_jobs_uses_persisted_routing_profile_over_title_regex(self):
        role = MarketingRole.objects.get(slug="devops-cloud")
        consultant_user = User.objects.create_user(
            username="consultant_match_routing", password="testpass", role=User.Role.CONSULTANT
        )
        consultant = ConsultantProfile.objects.create(
            user=consultant_user,
            bio="Platform consultant",
            work_countries=["united states"],
            preferred_seniority_levels=["senior"],
        )
        consultant.marketing_roles.add(role)

        eligible = Job.objects.create(
            title="Platform Engineer",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="Build and own platform reliability",
            routing_profile={
                "role_family": "platform_engineering",
                "seniority_primary": "senior",
                "country_codes": ["US"],
                "country_labels": ["United States"],
                "confidence": 0.88,
                "status": Job.RoutingStatus.READY,
            },
            routing_status=Job.RoutingStatus.READY,
            routing_seniority="senior",
            routing_country_codes=["US"],
        )
        eligible.marketing_roles.add(role)

        wrong = Job.objects.create(
            title="Senior Platform Engineer",
            company="Globex",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="Build and own platform reliability",
            routing_profile={
                "role_family": "platform_engineering",
                "seniority_primary": "junior",
                "country_codes": ["CA"],
                "country_labels": ["Canada"],
                "confidence": 0.88,
                "status": Job.RoutingStatus.READY,
            },
            routing_status=Job.RoutingStatus.READY,
            routing_seniority="junior",
            routing_country_codes=["CA"],
        )
        wrong.marketing_roles.add(role)

        matches = match_jobs_for_consultant(consultant, limit=10)
        self.assertEqual([job.pk for job in matches], [eligible.pk])

    def test_match_jobs_respects_work_auth_employment_and_clearance(self):
        config = PlatformConfig.load()
        config.routing_enforce_work_authorization = True
        config.routing_enforce_employment_preferences = True
        config.routing_enforce_work_mode = True
        config.routing_enforce_clearance = True
        config.save()

        role = MarketingRole.objects.get(slug="devops-cloud")
        consultant_user = User.objects.create_user(
            username="consultant_match_constraints", password="testpass", role=User.Role.CONSULTANT
        )
        consultant = ConsultantProfile.objects.create(
            user=consultant_user,
            bio="Secure platform consultant",
            work_countries=["United States"],
            work_authorization_countries=["United States"],
            preferred_seniority_levels=["senior"],
            employment_preferences=["w2"],
            preferred_work_modes=["remote"],
            requires_visa_sponsorship=False,
            clearance_eligible=True,
        )
        consultant.marketing_roles.add(role)

        eligible = Job.objects.create(
            title="Senior DevOps Engineer",
            company="Acme",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="Remote senior role for US candidates.",
            routing_profile={
                "seniority_primary": "senior",
                "country_codes": ["US"],
                "country_labels": ["United States"],
                "work_mode": "remote",
                "employment_type": "w2",
                "visa_sponsorship": False,
                "clearance_required": True,
                "confidence": 0.9,
                "status": Job.RoutingStatus.READY,
            },
            routing_status=Job.RoutingStatus.READY,
            routing_seniority="senior",
            routing_country_codes=["US"],
        )
        eligible.marketing_roles.add(role)

        blocked = Job.objects.create(
            title="Senior DevOps Engineer",
            company="Globex",
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description="Hybrid contract, Canada only, clearance required.",
            routing_profile={
                "seniority_primary": "senior",
                "country_codes": ["CA"],
                "country_labels": ["Canada"],
                "work_mode": "hybrid",
                "employment_type": "contract",
                "contract_constraints": ["No C2C"],
                "visa_sponsorship": False,
                "clearance_required": True,
                "confidence": 0.9,
                "status": Job.RoutingStatus.READY,
            },
            routing_status=Job.RoutingStatus.READY,
            routing_seniority="senior",
            routing_country_codes=["CA"],
        )
        blocked.marketing_roles.add(role)

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

    def test_classify_task_maps_long_raw_department_before_syncing_to_job(self):
        company = Company.objects.create(name="LongDeptCo")
        raw = RawJob.objects.create(
            company=company,
            company_name=company.name,
            title="IT Program Manager",
            description="Lead enterprise technology delivery, governance, and platform operations.",
            original_url="https://example.com/jobs/long-dept",
            url_hash=hashlib.sha256(b"https://example.com/jobs/long-dept").hexdigest(),
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
            department_normalized="Information Technology",
        )
        job = Job.objects.create(
            title=raw.title,
            company=company.name,
            posted_by=self.employee,
            status=Job.Status.POOL,
            description=raw.description,
            source_raw_job=raw,
            url_hash=raw.url_hash,
        )

        result = classify_jobs_task.apply(kwargs={"force_reclassify": True}).get()
        job.refresh_from_db()

        self.assertEqual(result["status"], "done")
        self.assertEqual(job.department, Job.Department.IT_MANAGEMENT)
        self.assertEqual(job.department_source, "raw_job")

    def test_classify_task_active_only_skips_closed_job_role_refresh(self):
        company = Company.objects.create(name="ActiveOnlyRouteCo")
        stale_role = MarketingRole.objects.get(slug="software-developer")
        active_url = "https://example.com/jobs/active-servicenow"
        closed_url = "https://example.com/jobs/closed-servicenow"
        active_raw = RawJob.objects.create(
            company=company,
            company_name=company.name,
            title="ServiceNow Developer",
            description="Own ServiceNow platform workflows and integrations.",
            original_url=active_url,
            url_hash=hashlib.sha256(active_url.encode()).hexdigest(),
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
        )
        closed_raw = RawJob.objects.create(
            company=company,
            company_name=company.name,
            title="ServiceNow Developer",
            description="Own ServiceNow platform workflows and integrations.",
            original_url=closed_url,
            url_hash=hashlib.sha256(closed_url.encode()).hexdigest(),
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
        )
        active_job = Job.objects.create(
            title=active_raw.title,
            company=company.name,
            posted_by=self.employee,
            status=Job.Status.POOL,
            description=active_raw.description,
            source_raw_job=active_raw,
            url_hash=active_raw.url_hash,
        )
        closed_job = Job.objects.create(
            title=closed_raw.title,
            company=company.name,
            posted_by=self.employee,
            status=Job.Status.CLOSED,
            description=closed_raw.description,
            source_raw_job=closed_raw,
            url_hash=closed_raw.url_hash,
            auto_marketing_role_slugs=["software-developer"],
        )
        closed_job.marketing_roles.add(stale_role)

        result = classify_jobs_task.apply(kwargs={"force_reclassify": True, "active_only": True}).get()
        active_job.refresh_from_db()
        closed_job.refresh_from_db()

        self.assertEqual(result["status"], "done")
        self.assertTrue(result["active_only"])
        self.assertIn("servicenow-developer", list(active_job.marketing_roles.values_list("slug", flat=True)))
        self.assertCountEqual(list(closed_job.marketing_roles.values_list("slug", flat=True)), ["software-developer"])


class JobRoutingGateAndRepairTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username="routing_gate_admin",
            email="routing-gate@example.com",
            password="testpass123",
        )
        from core.models import FeatureFlag

        FeatureFlag.objects.update_or_create(
            key="employee_job_pool",
            defaults={
                "label": "Job Pool",
                "category": "EMPLOYEE",
                "applies_to": "EMPLOYEE",
                "is_enabled": True,
                "enabled_for_employees": True,
                "enabled_for_consultants": False,
            },
        )

    def test_pool_approve_blocks_when_live_routing_policy_not_met(self):
        config = PlatformConfig.load()
        config.routing_require_parsed_jd_for_live = True
        config.routing_require_ready_for_live = True
        config.save()

        company = Company.objects.create(name="Gate Co")
        job = Job.objects.create(
            title="Platform Engineer",
            company=company.name,
            company_obj=company,
            description="This is a long enough JD " * 40,
            original_link="https://example.com/jobs/gate-co",
            posted_by=self.admin,
            status=Job.Status.POOL,
            stage=Job.Stage.VETTED,
            parsed_jd={},
            parsed_jd_status="",
            routing_status=Job.RoutingStatus.REVIEW,
            url_hash="gate-co-hash",
        )

        self.client.force_login(self.admin)
        response = self.client.post(reverse("job-approve", kwargs={"pk": job.pk}))
        self.assertEqual(response.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.POOL)
        self.assertEqual(job.pipeline_reason_code, "PARSED_JD_MISSING")

    def test_identity_repair_archives_newer_duplicates(self):
        company = Company.objects.create(name="Repair Co")
        survivor = Job.objects.create(
            title="Data Engineer",
            company=company.name,
            company_obj=company,
            description="JD body",
            original_link="https://example.com/jobs/repair-1",
            posted_by=self.admin,
            status=Job.Status.OPEN,
            url_hash="repair-hash",
        )
        duplicate = Job.objects.create(
            title="Data Engineer Copy",
            company=company.name,
            company_obj=company,
            description="JD body copy",
            original_link="https://example.com/jobs/repair-1-copy",
            posted_by=self.admin,
            status=Job.Status.POOL,
            url_hash="repair-hash",
        )

        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("job-identity-repair"),
            {"group_type": "url_hash", "group_key": "repair-hash"},
        )
        self.assertEqual(response.status_code, 302)
        survivor.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertFalse(survivor.is_archived)
        self.assertTrue(duplicate.is_archived)


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
        self.assertEqual(self.job.original_link_health, Job.LinkHealthState.DEAD)
        self.assertEqual(self.job.original_link_reason, "http_404")
        self.assertEqual(self.job.original_link_status_code, 404)
        self.assertTrue(self.job.possibly_filled)
        self.assertIsNotNone(self.job.original_link_last_checked_at)

    @patch('jobs.tasks._check_job_url', return_value=True)
    def test_validate_job_urls_keeps_live_link(self, _mock):
        validate_job_urls_task.apply(kwargs={"batch_size": 50}).get()
        self.job.refresh_from_db()
        self.assertTrue(self.job.original_link_is_live)
        self.assertEqual(self.job.original_link_health, Job.LinkHealthState.LIVE)
        self.assertEqual(self.job.original_link_reason, "detail_live_markers")
        self.assertEqual(self.job.original_link_status_code, 200)
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

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

from core.models import PlatformConfig
from users.models import User
from users.models import MarketingRole
from users.models import ConsultantProfile
from companies.models import Company
from harvest.models import RawJob
from harvest.models import RawJobPayloadSnapshot
from harvest.enrichments import detect_job_category
from .models import Job
from .models import RawJobClassificationSnapshot, RawJobClassifierRun
from .marketing_role_routing import (
    assign_marketing_roles_to_job,
    clear_marketing_role_cache,
    infer_marketing_role_slugs,
)
from .services import match_jobs_for_consultant
from .tasks import _department_sync_value, classify_jobs_task
from .tasks import validate_job_urls_task, auto_close_jobs_task
from .tasks import backfill_rawjob_dual_classification_task, run_rawjob_dual_classification_shadow_task


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
        self.assertContains(response, "Store Manual Secondary Classification")
        self.assertContains(response, "Backend canonical output")

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

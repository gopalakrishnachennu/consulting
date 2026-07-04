import json

from django.test import TestCase, override_settings
from django.urls import reverse

from harvest.models import HarvestEngineConfig, JobBoardPlatform, RawJob


@override_settings(HARVEST_PUSH_SECRET="test-secret")
class PushApiStrictIntakeTests(TestCase):
    def setUp(self):
        cfg = HarvestEngineConfig.get()
        cfg.selective_filter_enabled = True
        cfg.save()
        JobBoardPlatform.objects.get_or_create(name="Greenhouse", slug="greenhouse")
        self.url = reverse("harvest-push-jobs")
        self.auth = {"HTTP_AUTHORIZATION": "Bearer test-secret"}

    def _post_jobs(self, jobs):
        return self.client.post(
            self.url,
            data=json.dumps({"jobs": jobs, "trigger_pipeline": False}),
            content_type="application/json",
            **self.auth,
        )

    def test_push_api_drops_non_strong_title_in_strict_mode(self):
        response = self._post_jobs([
            {
                "original_url": "https://example.com/jobs/field-readiness-lead-1",
                "company_name": "Acme Inc",
                "title": "Field Readiness Lead (Remote)",
                "platform_slug": "greenhouse",
            }
        ])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 0)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertEqual(RawJob.objects.count(), 0)

    def test_push_api_persists_title_gate_for_strong_title(self):
        response = self._post_jobs([
            {
                "original_url": "https://example.com/jobs/senior-data-engineer-1",
                "company_name": "Acme Inc",
                "title": "Senior Data Engineer",
                "platform_slug": "greenhouse",
                "description": "Build data pipelines, warehousing, orchestration, and platform tooling.",
            }
        ])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)
        raw = RawJob.objects.get()
        self.assertEqual(raw.filter_decision, "STRONG")
        self.assertEqual(raw.title_gate_decision, "HARD_YES")
        self.assertEqual(raw.role_category, "data")
        self.assertIsNotNone(raw.filter_snapshot_id)

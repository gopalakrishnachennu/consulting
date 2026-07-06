from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from companies.models import Company
from harvest.dead_link_review import (
    apply_dead_link_review_action,
    flag_dead_raw_jobs_for_review,
    raw_job_auto_purge_eligible,
    raw_job_requires_admin_review,
)
from harvest.models import DeadLinkReviewItem, RawJob
from jobs.models import Job


class DeadLinkReviewServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme Corp")
        self.raw = RawJob.objects.create(
            company=self.company,
            company_name="Acme Corp",
            title="Engineer",
            original_url="https://example.com/job/1",
            platform_slug="workday",
            sync_status=RawJob.SyncStatus.PENDING,
            is_active=False,
            raw_payload={
                "link_health": {
                    "state": "DEAD",
                    "reason": "workday_search_no_match",
                    "checked_at": timezone.now().isoformat(),
                }
            },
        )

    def test_pending_raw_job_is_auto_purge_eligible(self):
        self.assertFalse(raw_job_requires_admin_review(self.raw))
        self.assertTrue(raw_job_auto_purge_eligible(self.raw))

    def test_synced_raw_job_requires_admin_review(self):
        self.raw.sync_status = RawJob.SyncStatus.SYNCED
        self.raw.save(update_fields=["sync_status"])
        self.assertTrue(raw_job_requires_admin_review(self.raw))
        self.assertFalse(raw_job_auto_purge_eligible(self.raw))

    def test_flag_creates_pending_review_for_synced_dead_row(self):
        self.raw.sync_status = RawJob.SyncStatus.SYNCED
        self.raw.save(update_fields=["sync_status"])
        flagged = flag_dead_raw_jobs_for_review([self.raw.id])
        self.assertEqual(flagged, 1)
        item = DeadLinkReviewItem.objects.get(raw_job=self.raw)
        self.assertEqual(item.status, DeadLinkReviewItem.Status.PENDING)
        self.assertEqual(item.link_health_reason, "workday_search_no_match")

    def test_flag_skips_non_pipeline_raw_rows(self):
        flagged = flag_dead_raw_jobs_for_review([self.raw.id])
        self.assertEqual(flagged, 0)
        self.assertFalse(DeadLinkReviewItem.objects.filter(raw_job=self.raw).exists())

    def test_purge_deletes_raw_row(self):
        self.raw.sync_status = RawJob.SyncStatus.SYNCED
        self.raw.save(update_fields=["sync_status"])
        flag_dead_raw_jobs_for_review([self.raw.id])
        item = DeadLinkReviewItem.objects.get(raw_job=self.raw)
        user = get_user_model().objects.create_superuser("admin", "admin@test.com", "pass")
        result = apply_dead_link_review_action([item.id], "purge", actor=user)
        self.assertEqual(result["purged_raw_jobs"], 1)
        self.assertFalse(RawJob.objects.filter(pk=self.raw.id).exists())

    def test_dismiss_restores_active_monitoring(self):
        self.raw.sync_status = RawJob.SyncStatus.SYNCED
        self.raw.save(update_fields=["sync_status"])
        flag_dead_raw_jobs_for_review([self.raw.id])
        item = DeadLinkReviewItem.objects.get(raw_job=self.raw)
        user = get_user_model().objects.create_superuser("admin2", "admin2@test.com", "pass")
        apply_dead_link_review_action([item.id], "dismiss", actor=user)
        self.raw.refresh_from_db()
        item.refresh_from_db()
        self.assertTrue(self.raw.is_active)
        self.assertEqual(item.status, DeadLinkReviewItem.Status.DISMISSED)


class DeadLinkReviewViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_superuser("ops", "ops@test.com", "pass")
        self.company = Company.objects.create(name="Beta Inc")
        self.raw = RawJob.objects.create(
            company=self.company,
            company_name="Beta Inc",
            title="Analyst",
            original_url="https://example.com/job/2",
            platform_slug="greenhouse",
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=False,
            raw_payload={"link_health": {"state": "DEAD", "reason": "greenhouse_api_not_found"}},
        )
        self.job = Job.objects.create(
            title="Analyst",
            company="Beta Inc",
            company_obj=self.company,
            description="Test job description for dead link review.",
            source_raw_job=self.raw,
            status=Job.Status.POOL,
            original_link=self.raw.original_url,
            posted_by=self.admin,
        )
        flag_dead_raw_jobs_for_review([self.raw.id])

    def test_queue_page_requires_superuser(self):
        url = reverse("harvest-dead-link-review")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.client.force_login(self.admin)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Dead Link Review")
        self.assertContains(resp, "Analyst")

    def test_archive_action_closes_linked_job(self):
        self.client.force_login(self.admin)
        item = DeadLinkReviewItem.objects.get(raw_job=self.raw)
        resp = self.client.post(
            reverse("harvest-dead-link-review"),
            {"action": "archive", "item_ids": [str(item.id)]},
        )
        self.assertEqual(resp.status_code, 302)
        self.job.refresh_from_db()
        self.assertTrue(self.job.is_archived)
        self.assertEqual(self.job.status, Job.Status.CLOSED)

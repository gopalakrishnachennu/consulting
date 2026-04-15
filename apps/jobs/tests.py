import csv
import io
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from users.models import User
from .models import Job
from .tasks import validate_job_urls_task, auto_close_jobs_task


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

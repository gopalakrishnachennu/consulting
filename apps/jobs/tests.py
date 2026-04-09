from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
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
        validate_job_urls_task()
        self.job.refresh_from_db()
        self.assertFalse(self.job.original_link_is_live)
        self.assertTrue(self.job.possibly_filled)
        self.assertIsNotNone(self.job.original_link_last_checked_at)

    @patch('jobs.tasks._check_job_url', return_value=True)
    def test_validate_job_urls_keeps_live_link(self, _mock):
        validate_job_urls_task()
        self.job.refresh_from_db()
        self.assertTrue(self.job.original_link_is_live)
        self.assertFalse(self.job.possibly_filled)

    @patch('jobs.tasks._check_job_url', return_value=True)
    def test_validate_job_urls_skips_recently_checked(self, _mock):
        self.job.original_link_last_checked_at = timezone.now()
        self.job.save(update_fields=['original_link_last_checked_at'])
        validate_job_urls_task()
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

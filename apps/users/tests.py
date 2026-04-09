from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from jobs.models import Job
from submissions.models import ApplicationSubmission
from .models import User, ConsultantProfile, EmployeeProfile, Department
from .journey_utils import compute_consultant_readiness, at_risk_submissions_queryset


class ConsultantExportCSVTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username='admin1', password='testpass', role=User.Role.ADMIN
        )
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT,
            first_name='Jane', last_name='Doe'
        )
        ConsultantProfile.objects.create(user=self.consultant_user, bio='Bio', hourly_rate=100)

    def test_export_csv_admin_returns_csv(self):
        self.client.login(username='admin1', password='testpass')
        url = reverse('consultant-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get('Content-Type', '').startswith('text/csv'))
        self.assertIn(b'con1', resp.content)


class EmployeeExportCSVTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username='admin1', password='testpass', role=User.Role.ADMIN
        )
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE,
            first_name='John', last_name='Smith'
        )
        EmployeeProfile.objects.create(user=self.employee, company_name='Acme')

    def test_export_csv_admin_returns_csv(self):
        self.client.login(username='admin1', password='testpass')
        url = reverse('employee-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get('Content-Type', '').startswith('text/csv'))
        self.assertIn(b'emp1', resp.content)

    def test_export_csv_consultant_forbidden(self):
        consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        ConsultantProfile.objects.create(user=consultant_user, bio='')
        self.client.login(username='con1', password='testpass')
        url = reverse('employee-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)


class ConsultantJourneyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(
            user=self.consultant_user,
            bio='x' * 50,
        )
        self.profile.onboarding_completed_at = timezone.now()
        self.profile.save(update_fields=['bio', 'onboarding_completed_at'])

    def test_journey_page_requires_consultant(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.get(reverse('consultant-journey'))
        self.assertEqual(resp.status_code, 403)

    def test_journey_page_loads_for_consultant(self):
        self.client.login(username='con1', password='testpass')
        resp = self.client.get(reverse('consultant-journey'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Readiness')

    def test_readiness_score_increases_with_submission(self):
        job = Job.objects.create(
            title='J',
            company='C',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/j',
        )
        my_sub = ApplicationSubmission.objects.filter(consultant=self.profile)
        base = compute_consultant_readiness(self.profile, my_sub)
        ApplicationSubmission.objects.create(
            job=job,
            consultant=self.profile,
            status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee,
        )
        my_sub = ApplicationSubmission.objects.filter(consultant=self.profile)
        after = compute_consultant_readiness(self.profile, my_sub)
        self.assertGreaterEqual(after, base)

    def test_at_risk_queryset_finds_dead_link_job(self):
        job = Job.objects.create(
            title='J',
            company='C',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/j',
            original_link_is_live=False,
            possibly_filled=False,
        )
        ApplicationSubmission.objects.create(
            job=job,
            consultant=self.profile,
            status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee,
        )
        qs = at_risk_submissions_queryset(self.profile)
        self.assertEqual(qs.count(), 1)

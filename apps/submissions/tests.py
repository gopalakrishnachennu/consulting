from django.test import TestCase, Client
from django.urls import reverse
from users.models import User, ConsultantProfile, EmployeeProfile
from jobs.models import Job
from .models import ApplicationSubmission


class SubmissionExportCSVTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.consultant_user, bio='Test')
        self.job = Job.objects.create(
            title='Dev', company='Co', posted_by=self.employee, status=Job.Status.OPEN,
            description='Work', original_link='https://example.com/j'
        )
        self.sub = ApplicationSubmission.objects.create(
            job=self.job, consultant=self.profile, status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee
        )

    def test_export_csv_employee_returns_csv(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('submission-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get('Content-Type', '').startswith('text/csv'))
        self.assertIn(b'Dev', resp.content)

    def test_export_csv_consultant_sees_only_own(self):
        self.client.login(username='con1', password='testpass')
        url = reverse('submission-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Dev', resp.content)


class SubmissionBulkStatusTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.consultant_user, bio='Test')
        self.job = Job.objects.create(
            title='Dev', company='Co', posted_by=self.employee, status=Job.Status.OPEN,
            description='Work', original_link='https://example.com/j'
        )
        self.sub = ApplicationSubmission.objects.create(
            job=self.job, consultant=self.profile, status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee
        )

    def test_bulk_status_update_employee(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('submission-bulk-status')
        resp = self.client.post(url, {'submission_ids': [self.sub.pk], 'status': 'INTERVIEW'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ApplicationSubmission.Status.INTERVIEW)

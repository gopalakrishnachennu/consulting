from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from users.models import User, ConsultantProfile
from jobs.models import Job
from submissions.models import ApplicationSubmission
from .models import Interview


class InterviewExportCSVTests(TestCase):
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
            job=self.job, consultant=self.profile, status=ApplicationSubmission.Status.INTERVIEW,
            submitted_by=self.employee
        )
        self.interview = Interview.objects.create(
            submission=self.sub, consultant=self.profile,
            job_title='Dev', company='Co', scheduled_at=timezone.now(),
            status=Interview.Status.SCHEDULED
        )

    def test_export_csv_consultant_returns_csv(self):
        self.client.login(username='con1', password='testpass')
        url = reverse('interview-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get('Content-Type', '').startswith('text/csv'))
        self.assertIn(b'Dev', resp.content)

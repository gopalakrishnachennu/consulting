from django.test import TestCase, Client
from django.urls import reverse
from users.models import User
from .models import Job


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

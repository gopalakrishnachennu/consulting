from django.test import TestCase, Client
from django.urls import reverse
from .models import User, ConsultantProfile, EmployeeProfile, Department


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

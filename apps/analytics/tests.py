from django.test import TestCase, Client
from django.urls import reverse
from users.models import User


class AnalyticsDateRangeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )

    def test_analytics_dashboard_all_time(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('analytics-dashboard')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('date_range_label', resp.context)
        self.assertEqual(resp.context['date_range_label'], 'All time')

    def test_analytics_dashboard_last_7_days(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('analytics-dashboard') + '?range=7'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context.get('date_range_label'), 'Last 7 days')
        self.assertEqual(resp.context.get('date_range'), '7')

    def test_analytics_dashboard_last_30_days(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('analytics-dashboard') + '?range=30'
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context.get('date_range_label'), 'Last 30 days')
        self.assertEqual(resp.context.get('date_range'), '30')

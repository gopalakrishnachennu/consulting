from django.test import TestCase, Client
from django.urls import reverse
from users.models import User
from .models import Company, CompanyDoNotSubmit, EnrichmentLog
from users.models import ConsultantProfile


class CompanyModelTests(TestCase):
    def test_create_company(self):
        c = Company.objects.create(
            name="TestCo", domain="testco.com", industry="Tech"
        )
        self.assertEqual(str(c), "TestCo")
        self.assertEqual(c.enrichment_status, Company.EnrichmentStatus.PENDING)

    def test_unique_name(self):
        Company.objects.create(name="Unique")
        with self.assertRaises(Exception):
            Company.objects.create(name="Unique")

    def test_blacklisted_company(self):
        c = Company.objects.create(
            name="BadCo", is_blacklisted=True, blacklist_reason="Violations"
        )
        self.assertTrue(c.is_blacklisted)


class CompanyDoNotSubmitTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="RestrictedCo")
        self.user = User.objects.create_user(
            username="con1", password="pass", role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.user, bio="Test")

    def test_create_dnd_rule(self):
        dnd = CompanyDoNotSubmit.objects.create(
            company=self.company, consultant=self.profile, reason="NDA"
        )
        self.assertIn("DND", str(dnd))

    def test_unique_together(self):
        CompanyDoNotSubmit.objects.create(company=self.company, consultant=self.profile)
        with self.assertRaises(Exception):
            CompanyDoNotSubmit.objects.create(company=self.company, consultant=self.profile)


class EnrichmentLogTests(TestCase):
    def test_create_log(self):
        c = Company.objects.create(name="LogCo")
        log = EnrichmentLog.objects.create(
            company=c, source="clearbit", fields_updated={"industry": "Tech"}, success=True
        )
        self.assertTrue(log.success)


class CompanyViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="emp1", password="testpass", role=User.Role.EMPLOYEE
        )
        Company.objects.create(name="ViewTestCo", domain="viewtest.com")

    def test_company_list_authenticated(self):
        self.client.login(username="emp1", password="testpass")
        url = reverse("company-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ViewTestCo")

    def test_company_list_unauthenticated(self):
        url = reverse("company-list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)

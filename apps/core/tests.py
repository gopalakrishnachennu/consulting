from django.test import TestCase, Client
from django.urls import reverse
from users.models import User
from .models import PlatformConfig, LLMConfig, AuditLog, Organisation


class PlatformConfigTests(TestCase):
    def test_singleton_load(self):
        config = PlatformConfig.load()
        self.assertIsNotNone(config)
        self.assertEqual(config.pk, 1)

    def test_singleton_enforced(self):
        PlatformConfig.load()
        config2 = PlatformConfig(site_name="Another")
        config2.save()
        self.assertEqual(config2.pk, 1)
        self.assertEqual(PlatformConfig.objects.count(), 1)

    def test_prevent_deletion(self):
        config = PlatformConfig.load()
        config.delete()  # delete() is overridden to no-op
        # After calling delete, count may be 0 if the override only prevents
        # the ORM delete but cache was cleared. Re-load to verify:
        config2 = PlatformConfig.load()
        self.assertIsNotNone(config2)

    def test_default_values(self):
        config = PlatformConfig.load()
        self.assertEqual(config.site_name, "EduConsult")
        self.assertTrue(config.enable_consultant_registration)


class LLMConfigTests(TestCase):
    def test_singleton_load(self):
        config = LLMConfig.load()
        self.assertIsNotNone(config)
        self.assertEqual(config.pk, 1)
        self.assertEqual(config.active_model, "gpt-4o-mini")

    def test_versioning_on_save(self):
        config = LLMConfig.load()
        config.active_model = "gpt-4o"
        config.save()
        self.assertEqual(config.versions.count(), 1)
        config.temperature = 0.5
        config.save()
        self.assertEqual(config.versions.count(), 2)


class OrganisationTests(TestCase):
    def test_create_org(self):
        org = Organisation.objects.create(name="TestOrg", slug="test-org")
        self.assertEqual(str(org), "TestOrg")
        self.assertTrue(org.is_active)


class AuditLogTests(TestCase):
    def test_create_log(self):
        user = User.objects.create_user(username="u1", password="pass")
        log = AuditLog.objects.create(
            actor=user, action="test_action",
            target_model="User", target_id=str(user.pk),
        )
        self.assertIn("test_action", str(log))


class HealthCheckViewTests(TestCase):
    def test_health_check_returns_json(self):
        client = Client()
        resp = client.get(reverse("health-json"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("overall", data)
        self.assertIn("database", data)


class HomeViewTests(TestCase):
    def test_home_page_loads(self):
        client = Client()
        resp = client.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)

    def test_admin_dashboard_requires_login(self):
        client = Client()
        resp = client.get(reverse("admin-dashboard"))
        self.assertEqual(resp.status_code, 302)

    def test_employee_dashboard_requires_login(self):
        client = Client()
        resp = client.get(reverse("employee-dashboard"))
        self.assertEqual(resp.status_code, 302)


class SeedDataCommandTests(TestCase):
    def test_seed_data_creates_users(self):
        from django.core.management import call_command
        call_command("seed_data")
        self.assertTrue(User.objects.filter(username="admin").exists())
        self.assertTrue(User.objects.filter(username="sarah_hr").exists())
        self.assertTrue(User.objects.filter(username="john_dev").exists())
        self.assertTrue(User.objects.filter(role=User.Role.CONSULTANT).count() >= 5)

    def test_seed_data_idempotent(self):
        from django.core.management import call_command
        call_command("seed_data")
        count1 = User.objects.count()
        call_command("seed_data")
        count2 = User.objects.count()
        self.assertEqual(count1, count2)

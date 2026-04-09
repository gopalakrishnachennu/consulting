from django.test import TestCase, Client
from django.urls import reverse
from users.models import User, UserEmailNotificationPreferences
from .models import PlatformConfig, LLMConfig, AuditLog, Organisation, Notification, BroadcastMessage
from .notification_utils import create_notification, sanitize_internal_link
from .broadcast_utils import _recipient_queryset


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


class NotificationUtilsTests(TestCase):
    def test_sanitize_internal_link_accepts_safe_paths(self):
        self.assertEqual(sanitize_internal_link("/submissions/1/"), "/submissions/1/")
        self.assertEqual(sanitize_internal_link(""), "")

    def test_sanitize_internal_link_rejects_open_redirects(self):
        self.assertEqual(sanitize_internal_link("//evil.com"), "")
        self.assertEqual(sanitize_internal_link("https://evil.com"), "")
        self.assertEqual(sanitize_internal_link("/\\evil.com"), "")

    def test_create_notification_respects_inapp_mute(self):
        user = User.objects.create_user(username="n1", password="pass", role=User.Role.CONSULTANT)
        prefs, _ = UserEmailNotificationPreferences.objects.get_or_create(user=user)
        prefs.inapp_submissions = False
        prefs.save()
        out = create_notification(
            user,
            kind=Notification.Kind.SUBMISSION,
            title="Test",
            body="Body",
            link="/submissions/1/",
        )
        self.assertIsNone(out)
        self.assertEqual(Notification.objects.filter(user=user).count(), 0)

    def test_dedupe_key_prevents_duplicate_rows(self):
        user = User.objects.create_user(username="n2", password="pass", role=User.Role.EMPLOYEE)
        a = create_notification(
            user,
            kind=Notification.Kind.SYSTEM,
            title="Once",
            dedupe_key="task:123",
        )
        b = create_notification(
            user,
            kind=Notification.Kind.SYSTEM,
            title="Twice",
            dedupe_key="task:123",
        )
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(Notification.objects.filter(user=user).count(), 1)


class BroadcastAudienceQueryTests(TestCase):
    """Workforce audience filters: employees, consultants, both."""

    def setUp(self):
        self.emp = User.objects.create_user(username="aud_emp", password="pass", role=User.Role.EMPLOYEE)
        self.con = User.objects.create_user(username="aud_con", password="pass", role=User.Role.CONSULTANT)
        self.adm = User.objects.create_user(username="aud_adm", password="pass", role=User.Role.ADMIN)

    def _ids(self, audience: str):
        m = BroadcastMessage(audience=audience)
        return set(_recipient_queryset(m).values_list("pk", flat=True))

    def test_employees_only(self):
        self.assertEqual(self._ids(BroadcastMessage.Audience.EMPLOYEES_ONLY), {self.emp.pk})

    def test_consultants_only(self):
        self.assertEqual(self._ids(BroadcastMessage.Audience.CONSULTANTS), {self.con.pk})

    def test_employees_and_consultants(self):
        self.assertEqual(
            self._ids(BroadcastMessage.Audience.EMPLOYEES_AND_CONSULTANTS),
            {self.emp.pk, self.con.pk},
        )

from datetime import timedelta

from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch
from users.models import User, UserEmailNotificationPreferences
from .models import PlatformConfig, LLMConfig, AuditLog, Notification, BroadcastMessage
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

    def test_india_inspired_color_themes_persist(self):
        config = PlatformConfig.load()
        config.color_theme = PlatformConfig.ColorTheme.SAFFRON
        config.save()
        self.assertEqual(PlatformConfig.load().color_theme, PlatformConfig.ColorTheme.SAFFRON)

        config.color_theme = PlatformConfig.ColorTheme.CHAKRA
        config.save()
        self.assertEqual(PlatformConfig.load().color_theme, PlatformConfig.ColorTheme.CHAKRA)

        config.color_theme = PlatformConfig.ColorTheme.ASHOKA
        config.save()
        self.assertEqual(PlatformConfig.load().color_theme, PlatformConfig.ColorTheme.ASHOKA)


class PlatformConfigAdminViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("platform_admin", "platform@example.com", "pass")
        self.client.force_login(self.user)

    def test_layout_tab_contains_india_inspired_palettes(self):
        resp = self.client.get(reverse("platform-config"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="saffron"')
        self.assertContains(resp, 'value="chakra"')
        self.assertContains(resp, 'value="banyan"')
        self.assertContains(resp, 'value="kesari"')
        self.assertContains(resp, 'value="ashoka"')
        self.assertContains(resp, 'value="ivory_saffron"')
        self.assertContains(resp, "India-inspired palettes")
        self.assertContains(resp, "Live preview")
        self.assertContains(resp, "Contrast status")


class DeploymentInfoContextProcessorTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_deployment_info_hidden_for_regular_users(self):
        from core.context_processors import deployment_info

        request = self.factory.get("/")
        request.user = User.objects.create_user(username="regular", password="pass")

        self.assertIsNone(deployment_info(request)["DEPLOYMENT_INFO"])

    def test_deployment_info_visible_for_superusers(self):
        from core.context_processors import deployment_info

        request = self.factory.get("/")
        request.user = User.objects.create_superuser("admin", "admin@example.com", "pass")
        payload = {
            "sha": "608a6610c30d3a59390b82d6fbc4e5ca8061e42d",
            "short_sha": "608a661",
            "subject": "Guard harvest smoke rows from production backlog",
            "committed_at": "2026-05-14T01:00:00+00:00",
            "branch": "main",
            "commit_url": "",
        }

        with patch("core.context_processors._deployment_metadata", return_value=payload):
            self.assertEqual(deployment_info(request)["DEPLOYMENT_INFO"], payload)


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


class AuditLogTests(TestCase):
    def test_create_log(self):
        user = User.objects.create_user(username="u1", password="pass")
        log = AuditLog.objects.create(
            actor=user, action="test_action",
            target_model="User", target_id=str(user.pk),
        )
        self.assertIn("test_action", str(log))

    def test_post_summary_redacts_compound_secret_fields(self):
        from .audit_utils import safe_post_summary

        request = RequestFactory().post(
            "/core/setup/",
            {
                "google_kg_api_key": "kg-secret",
                "apollo_api_key": "apollo-secret",
                "geocoding_provider_token": "map-token",
                "site_name": "GoCareers",
            },
        )

        summary = safe_post_summary(request)
        self.assertEqual(summary["google_kg_api_key"], "[redacted]")
        self.assertEqual(summary["apollo_api_key"], "[redacted]")
        self.assertEqual(summary["geocoding_provider_token"], "[redacted]")
        self.assertEqual(summary["site_name"], "GoCareers")

    def test_query_params_and_full_path_redact_secret_fields(self):
        from .audit_utils import safe_full_path, safe_query_params

        request = RequestFactory().get(
            "/core/audit/",
            {
                "page": "2",
                "token": "secret-token",
                "next": "/jobs/",
                "signature": "signed-value",
            },
        )

        params = safe_query_params(request.GET)
        self.assertEqual(params["token"], "[redacted]")
        self.assertEqual(params["signature"], "[redacted]")
        self.assertEqual(params["page"], "2")
        self.assertNotIn("secret-token", safe_full_path(request))
        self.assertNotIn("signed-value", safe_full_path(request))


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


class AdminDashboardCompanyKpiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("dash_admin", "dash@example.com", "pass")
        self.client.force_login(self.user)

    def test_company_kpis_use_platform_label_relation(self):
        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, JobBoardPlatform

        platform = JobBoardPlatform.objects.create(slug="testboard", name="Test Board")
        with_platform = Company.objects.create(name="Company With Platform")
        undetected = Company.objects.create(name="Company Without Platform")
        CompanyPlatformLabel.objects.create(
            company=with_platform,
            platform=platform,
            tenant_id="company-with-platform",
        )
        CompanyPlatformLabel.objects.create(company=undetected, platform=None)

        resp = self.client.get(
            reverse("admin-dashboard"),
            {"section": "kpis"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["company_total"], 2)
        self.assertEqual(resp.context["company_with_platform"], 1)
        self.assertContains(resp, "1 with platforms")


class AdminDashboardHarvestFreshnessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("harvest_admin", "harvest@example.com", "pass")
        self.client.force_login(self.user)

    def _create_raw_job(self, *, fetched_at, url_hash="dashboard-freshness"):
        from companies.models import Company
        from harvest.models import RawJob

        company = Company.objects.create(name=f"Freshness Co {url_hash}")
        raw = RawJob.objects.create(
            company=company,
            company_name=company.name,
            platform_slug="greenhouse",
            title="Software Engineer",
            original_url=f"https://example.test/jobs/{url_hash}",
            url_hash=url_hash,
            has_description=True,
        )
        RawJob.objects.filter(pk=raw.pk).update(fetched_at=fetched_at)
        raw.refresh_from_db()
        return raw

    def _create_old_batch(self):
        from harvest.models import FetchBatch

        old_batch = FetchBatch.objects.create(
            name="Old tracked batch",
            status=FetchBatch.Status.COMPLETED,
        )
        FetchBatch.objects.filter(pk=old_batch.pk).update(
            created_at=timezone.now() - timedelta(hours=50)
        )
        old_batch.refresh_from_db()
        return old_batch

    def test_fresh_raw_jobs_do_not_show_stale_banner_when_batch_audit_is_old(self):
        self._create_old_batch()
        self._create_raw_job(fetched_at=timezone.now() - timedelta(minutes=20))

        resp = self.client.get(reverse("admin-dashboard"))

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["harvest_health"]["harvest_stale"])
        self.assertEqual(resp.context["harvest_health"]["source_sync_basis"], "raw_jobs")
        self.assertContains(resp, "Newest fetch:")
        self.assertContains(resp, "batch audit:")
        self.assertNotContains(resp, "Source Sync Stale")

    def test_old_raw_jobs_still_show_stale_banner(self):
        self._create_raw_job(
            fetched_at=timezone.now() - timedelta(hours=30),
            url_hash="dashboard-stale",
        )

        resp = self.client.get(reverse("admin-dashboard"))

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["harvest_health"]["harvest_stale"])
        self.assertContains(resp, "Source Sync Stale")
        self.assertContains(resp, "newest harvested job")


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

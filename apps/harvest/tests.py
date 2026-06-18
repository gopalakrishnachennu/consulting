"""Fast checks for career URLs, tenant extraction, harvester wiring, and smoke command."""

import json
from io import StringIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from harvest.career_url import build_career_url
from harvest.detectors import extract_tenant
from harvest.detectors.url_pattern import URLPatternDetector, pattern_matches_url
from harvest.harvesters import (
    TeamtailorHarvester,
    ZohoHarvester,
    get_harvester,
)
from harvest.jarvis import JobJarvis
from harvest.harvesters.oracle import OracleHCMHarvester
from harvest.platform_engine import ImplementationKind, dedicated_slugs, kind_for_slug


class HarvestUrlAndRegistryTests(SimpleTestCase):
    databases = {"default"}

    def test_build_career_url_zoho(self):
        self.assertEqual(
            build_career_url("zoho", "acme"),
            "https://jobs.zoho.com/portal/acme/careers",
        )
        self.assertEqual(
            build_career_url("zoho", "acme.zohorecruit.com"),
            "https://acme.zohorecruit.com/jobs/Careers",
        )

    def test_extract_tenant_subdomain_hosts(self):
        self.assertEqual(
            extract_tenant("teamtailor", "https://widgets.teamtailor.com/jobs"),
            "widgets",
        )
        self.assertEqual(
            extract_tenant("breezy", "https://foo.breezy.hr/p/1"),
            "foo",
        )
        self.assertEqual(
            extract_tenant("zoho", "https://jobs.zoho.com/portal/acme/careers"),
            "acme",
        )
        self.assertEqual(
            extract_tenant("zoho", "https://acme.zohorecruit.com/jobs/Careers"),
            "acme",
        )

    def test_get_harvester_and_platform_kind(self):
        self.assertIsInstance(get_harvester("zoho"), ZohoHarvester)
        self.assertIsInstance(get_harvester("teamtailor"), TeamtailorHarvester)
        self.assertEqual(kind_for_slug("zoho"), ImplementationKind.DEDICATED)
        self.assertEqual(kind_for_slug("teamtailor"), ImplementationKind.DEDICATED)
        self.assertIn("zoho", dedicated_slugs())
        self.assertIn("teamtailor", dedicated_slugs())

    def test_url_pattern_matching_respects_host_boundaries(self):
        self.assertTrue(pattern_matches_url("lever.co", "https://jobs.lever.co/acme"))
        self.assertFalse(pattern_matches_url("lever.co", "https://www.clever.com/careers"))

    def test_validate_job_domain_taxonomy_command(self):
        out = StringIO()
        call_command("validate_job_domain_taxonomy", stdout=out)
        self.assertIn("Marketing role taxonomy OK", out.getvalue())


class PlatformRegistryDetectionTests(TestCase):
    def test_url_pattern_detector_uses_enabled_db_registry_patterns(self):
        from harvest.models import JobBoardPlatform

        JobBoardPlatform.objects.create(
            name="Example ATS",
            slug="example_ats",
            url_patterns=["careers.example-ats.test/jobs"],
            is_enabled=True,
        )
        company = SimpleNamespace(
            career_site_url="https://careers.example-ats.test/jobs/acme",
            website="",
        )

        slug, confidence, method = URLPatternDetector().detect(company)

        self.assertEqual(slug, "example_ats")
        self.assertEqual(confidence, "HIGH")
        self.assertEqual(method, "URL_PATTERN")

    def test_url_pattern_detector_ignores_disabled_db_patterns(self):
        from harvest.models import JobBoardPlatform

        JobBoardPlatform.objects.create(
            name="Disabled ATS",
            slug="disabled_ats",
            url_patterns=["disabled-ats.example"],
            is_enabled=False,
        )
        company = SimpleNamespace(
            career_site_url="https://disabled-ats.example/jobs",
            website="",
        )

        slug, confidence, method = URLPatternDetector().detect(company)

        self.assertIsNone(slug)
        self.assertEqual(confidence, "UNKNOWN")
        self.assertEqual(method, "UNDETECTED")


class PlatformSettingsViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_superuser(
            "platform-admin@example.com",
            "platform-admin@example.com",
            "pw",
        )
        self.client.force_login(self.user)

    def test_platform_settings_show_content_and_runtime_controls(self):
        from harvest.models import JobBoardPlatform, PlatformEngineConfig

        platform = JobBoardPlatform.objects.create(
            name="Example Platform",
            slug="example-platform",
            url_patterns=["jobs.example.com"],
            api_type=JobBoardPlatform.ApiType.GREENHOUSE_API,
            support_tier=JobBoardPlatform.SupportTier.HEALTHY,
            title_in_list=True,
            list_has_description=True,
            rate_limit_per_min=17,
        )
        PlatformEngineConfig.objects.create(
            platform=platform,
            auto_backfill=True,
            backfill_priority=2,
            fetch_cadence_hours=12,
            inter_request_delay_ms=2400,
            min_quality_score=0.42,
            is_active=True,
        )

        list_response = self.client.get(reverse("harvest-platforms"))
        self.assertEqual(list_response.status_code, 200)
        list_html = list_response.content.decode()
        self.assertIn("Runtime Rules", list_html)
        self.assertIn("Title in list", list_html)
        self.assertIn("JD in list", list_html)
        self.assertIn("2400ms delay", list_html)
        self.assertIn("12h cadence", list_html)

        edit_response = self.client.get(reverse("harvest-platform-edit", args=[platform.pk]))
        self.assertEqual(edit_response.status_code, 200)
        edit_html = edit_response.content.decode()
        self.assertIn("List Payload Capability", edit_html)
        self.assertIn("JD in list payload", edit_html)
        self.assertIn("Per-Platform Runtime Controls", edit_html)
        self.assertIn('name="config_inter_request_delay_ms"', edit_html)

    def test_platform_edit_updates_runtime_config_and_list_description_flag(self):
        from harvest.models import JobBoardPlatform, PlatformEngineConfig

        platform = JobBoardPlatform.objects.create(
            name="Runtime Platform",
            slug="runtime-platform",
            url_patterns=["old.example.com"],
            api_type=JobBoardPlatform.ApiType.HTML_SCRAPE,
            support_tier=JobBoardPlatform.SupportTier.EXPERIMENTAL,
            list_has_description=False,
        )

        response = self.client.post(
            reverse("harvest-platform-edit", args=[platform.pk]),
            {
                "name": "Runtime Platform",
                "slug": "runtime-platform",
                "url_patterns": '["new.example.com", "NEW.example.com", ""]',
                "api_type": JobBoardPlatform.ApiType.GREENHOUSE_API,
                "fetch_endpoint_tmpl": "https://api.example.com/{tenant}/jobs",
                "headers_json": '{"Accept": "application/json"}',
                "rate_limit_per_min": "22",
                "unknown_jd_budget_per_run": "4",
                "support_tier": JobBoardPlatform.SupportTier.HEALTHY,
                "color_hex": "#123456",
                "notes": "Verified in GUI.",
                "is_enabled": "on",
                "title_in_list": "on",
                "list_has_description": "on",
                "config_auto_backfill": "on",
                "config_backfill_priority": "3",
                "config_fetch_cadence_hours": "18",
                "config_inter_request_delay_ms": "3100",
                "config_min_quality_score": "0.58",
                "config_is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        platform.refresh_from_db()
        self.assertEqual(platform.url_patterns, ["new.example.com"])
        self.assertTrue(platform.title_in_list)
        self.assertTrue(platform.list_has_description)
        self.assertEqual(platform.rate_limit_per_min, 22)

        cfg = PlatformEngineConfig.objects.get(platform=platform)
        self.assertTrue(cfg.auto_backfill)
        self.assertEqual(cfg.backfill_priority, 3)
        self.assertEqual(cfg.fetch_cadence_hours, 18)
        self.assertEqual(cfg.inter_request_delay_ms, 3100)
        self.assertAlmostEqual(cfg.min_quality_score, 0.58)
        self.assertTrue(cfg.is_active)

    def test_platform_form_rejects_non_array_url_patterns(self):
        from harvest.models import JobBoardPlatform

        response = self.client.post(
            reverse("harvest-platform-create"),
            {
                "name": "Bad Platform",
                "slug": "bad-platform",
                "url_patterns": '{"host": "jobs.example.com"}',
                "api_type": JobBoardPlatform.ApiType.UNKNOWN,
                "headers_json": "{}",
                "rate_limit_per_min": "10",
                "unknown_jd_budget_per_run": "2",
                "support_tier": JobBoardPlatform.SupportTier.EXPERIMENTAL,
                "color_hex": "#6B7280",
                "config_backfill_priority": "5",
                "config_fetch_cadence_hours": "24",
                "config_inter_request_delay_ms": "1500",
                "config_min_quality_score": "0.3",
                "config_is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a JSON array of URL pattern strings.")


class EngineConfigViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_superuser(
            "engine-admin@example.com",
            "engine-admin@example.com",
            "pw",
        )
        self.client.force_login(self.user)

    def test_engine_config_can_clear_hard_negative_phrases(self):
        from harvest.models import HarvestEngineConfig

        cfg = HarvestEngineConfig.get()
        cfg.hard_negative_phrases = ["registered nurse", "warehouse worker"]
        cfg.save()

        response = self.client.post(
            reverse("harvest-engine-config"),
            {"hard_negative_phrases": ""},
        )

        self.assertEqual(response.status_code, 302)
        cfg.refresh_from_db()
        self.assertEqual(cfg.hard_negative_phrases, [])


class HarvestUrlHashDedupeTests(SimpleTestCase):
    def test_tracking_query_params_do_not_change_hash(self):
        from harvest.normalizer import compute_url_hash

        a = "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE/jobs/6503?src=LinkedIn&utm_source=linkedin"
        b = "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE/jobs/6503"
        self.assertEqual(compute_url_hash(a), compute_url_hash(b))

    def test_identity_query_params_still_change_hash(self):
        from harvest.normalizer import compute_url_hash

        a = "https://example.com/jobs/view?jobId=123"
        b = "https://example.com/jobs/view?jobId=456"
        self.assertNotEqual(compute_url_hash(a), compute_url_hash(b))


class HarvestEngineHardeningTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, JobBoardPlatform

        self.company = Company.objects.create(name="Hardening Co")
        self.platform = JobBoardPlatform.objects.create(name="Hardening ATS", slug="hardening")
        self.label = CompanyPlatformLabel.objects.create(
            company=self.company,
            platform=self.platform,
            tenant_id="hardening",
        )

    def _raw_defaults(self, **overrides):
        defaults = {
            "company": self.company,
            "platform_label": self.label,
            "job_platform": self.platform,
            "platform_slug": self.platform.slug,
            "external_id": overrides.pop("external_id", ""),
            "original_url": overrides.pop("original_url", "https://hardening.example/jobs/new"),
            "title": overrides.pop("title", "Software Engineer"),
            "company_name": self.company.name,
            "content_hash": overrides.pop("content_hash", "content-same"),
            "sync_status": "PENDING",
            "is_active": True,
        }
        defaults.update(overrides)
        return defaults

    def test_atomic_rawjob_upsert_skips_content_duplicate(self):
        from harvest.models import RawJob
        from harvest.normalizer import compute_url_hash
        from harvest.services.rawjob_upsert import upsert_raw_job_with_dedupe

        RawJob.objects.create(
            **self._raw_defaults(
                url_hash=compute_url_hash("https://hardening.example/jobs/existing"),
                original_url="https://hardening.example/jobs/existing",
                content_hash="content-same",
            )
        )

        result = upsert_raw_job_with_dedupe(
            company=self.company,
            defaults=self._raw_defaults(
                original_url="https://hardening.example/jobs/new",
                content_hash="content-same",
            ),
            url_hash=compute_url_hash("https://hardening.example/jobs/new"),
            original_url="https://hardening.example/jobs/new",
            external_id="new-ext",
            platform_label=self.label,
            job_platform=self.platform,
            platform_slug=self.platform.slug,
        )

        self.assertEqual(result.action, "duplicate")
        self.assertEqual(result.reason, "content_hash_duplicate")
        self.assertEqual(RawJob.objects.count(), 1)

    def test_atomic_rawjob_upsert_updates_external_identity(self):
        from harvest.models import RawJob
        from harvest.normalizer import compute_url_hash
        from harvest.services.rawjob_upsert import upsert_raw_job_with_dedupe

        existing = RawJob.objects.create(
            **self._raw_defaults(
                url_hash=compute_url_hash("https://hardening.example/jobs/old"),
                original_url="https://hardening.example/jobs/old",
                external_id="REQ-1",
                content_hash="old-content",
            )
        )

        result = upsert_raw_job_with_dedupe(
            company=self.company,
            defaults=self._raw_defaults(
                original_url="https://hardening.example/jobs/new?utm=1",
                external_id="REQ-1",
                content_hash="new-content",
                title="Senior Software Engineer",
            ),
            url_hash=compute_url_hash("https://hardening.example/jobs/new?utm=1"),
            original_url="https://hardening.example/jobs/new?utm=1",
            external_id="REQ-1",
            platform_label=self.label,
            job_platform=self.platform,
            platform_slug=self.platform.slug,
        )

        self.assertEqual(result.action, "updated")
        self.assertEqual(result.raw_job.pk, existing.pk)
        self.assertEqual(RawJob.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.title, "Senior Software Engineer")

    def test_ready_stage_threshold_comes_from_engine_config(self):
        from django.core.cache import cache
        from harvest.models import HarvestEngineConfig, RawJob
        from harvest.services.rawjob_query import ready_stage_q

        cfg = HarvestEngineConfig.get()
        cfg.ready_stage_min_confidence = 0.70
        cfg.save()
        cache.delete("harvest:ready-stage-min-confidence:v1")

        low = RawJob.objects.create(
            **self._raw_defaults(
                url_hash="ready-low",
                description="A real job description",
                has_description=True,
                category_confidence=0.60,
                job_domain="devops-engineer",
            )
        )
        high = RawJob.objects.create(
            **self._raw_defaults(
                url_hash="ready-high",
                original_url="https://hardening.example/jobs/high",
                content_hash="content-high",
                description="A real job description",
                has_description=True,
                category_confidence=0.75,
                job_domain="devops-engineer",
            )
        )

        qs = RawJob.objects.filter(ready_stage_q())
        self.assertFalse(qs.filter(pk=low.pk).exists())
        self.assertTrue(qs.filter(pk=high.pk).exists())

    def test_scope_evaluation_uses_config_provider_when_requested(self):
        from harvest.location_resolver import LocationResolution, evaluate_rawjob_scope
        from harvest.models import HarvestEngineConfig, RawJob

        cfg = HarvestEngineConfig.get()
        cfg.geocoding_cache_enabled = False
        cfg.geocoding_provider_enabled = True
        cfg.geocoding_provider = "mapbox"
        cfg.target_countries = ["US"]
        cfg.save()
        raw = RawJob.objects.create(
            **self._raw_defaults(
                url_hash="scope-provider-auto",
                location_raw="Provider Only Place",
                content_hash="scope-provider-auto",
            )
        )

        with patch(
            "harvest.location_resolver._mapbox_geocode",
            return_value=LocationResolution(
                raw_text="Provider Only Place",
                normalized_text="provider only place",
                country_code="US",
                country_name="United States",
                confidence=0.95,
                source="mapbox",
                status="RESOLVED",
            ),
        ) as mocked:
            evaluate_rawjob_scope(raw, cfg=cfg, use_provider=None, save=True)

        raw.refresh_from_db()
        self.assertTrue(mocked.called)
        self.assertEqual(raw.country_code, "US")
        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)

    def test_backfill_stale_lock_window_comes_from_engine_config(self):
        from datetime import timedelta
        from django.utils import timezone
        from harvest.models import HarvestEngineConfig, RawJob
        from harvest.tasks import _backfill_eligible_queryset

        cfg = HarvestEngineConfig.get()
        cfg.jd_backfill_lock_stale_minutes = 5
        cfg.save()

        stale = RawJob.objects.create(
            **self._raw_defaults(
                url_hash="stale-lock",
                original_url="https://hardening.example/jobs/stale-lock",
                content_hash="stale-lock",
                is_priority=True,
                has_description=False,
                jd_backfill_locked_at=timezone.now() - timedelta(minutes=6),
            )
        )
        fresh = RawJob.objects.create(
            **self._raw_defaults(
                url_hash="fresh-lock",
                original_url="https://hardening.example/jobs/fresh-lock",
                content_hash="fresh-lock",
                is_priority=True,
                has_description=False,
                jd_backfill_locked_at=timezone.now() - timedelta(minutes=4),
            )
        )

        eligible = _backfill_eligible_queryset(None)
        self.assertTrue(eligible.filter(pk=stale.pk).exists())
        self.assertFalse(eligible.filter(pk=fresh.pk).exists())


class SelectiveHarvestEngineTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, HarvestEngineConfig, HarvestRoleCategory, JobBoardPlatform

        self.company = Company.objects.create(name="Selective Co")
        self.platform, _ = JobBoardPlatform.objects.update_or_create(
            slug="greenhouse",
            defaults={
                "name": "Greenhouse",
                "title_in_list": True,
                "unknown_jd_budget_per_run": 1,
            },
        )
        self.label = CompanyPlatformLabel.objects.create(
            company=self.company,
            platform=self.platform,
            tenant_id="selective",
        )
        HarvestRoleCategory.objects.update_or_create(
            slug="devops",
            defaults={
                "name": "DevOps",
                "include_phrases": ["devops engineer", "site reliability engineer"],
                "exclude_phrases": ["sales"],
                "is_active": True,
            },
        )
        cfg = HarvestEngineConfig.get()
        cfg.hard_negative_phrases = ["registered nurse", "warehouse associate"]
        cfg.save()

    def _raw_job(self, **overrides):
        from harvest.models import RawJob

        defaults = {
            "company": self.company,
            "platform_label": self.label,
            "job_platform": self.platform,
            "platform_slug": self.platform.slug,
            "company_name": self.company.name,
            "title": "Warehouse Associate",
            "url_hash": overrides.pop("url_hash", "selective-hash"),
            "original_url": overrides.pop("original_url", "https://selective.example/jobs/1"),
            "has_description": False,
            "is_priority": True,
        }
        defaults.update(overrides)
        return RawJob.objects.create(**defaults)

    def test_classifier_floors_to_possible_when_include_and_category_exclude_match(self):
        from harvest.models import HarvestFilterSnapshot
        from harvest.role_filter import classify_title

        snapshot = HarvestFilterSnapshot.create_snapshot()
        result = classify_title(
            title="DevOps Engineer Sales Tools",
            categories=snapshot.get_categories(),
            hard_negatives=["sales tools"],
            snapshot_id=str(snapshot.snapshot_id),
        )

        self.assertEqual(result.decision, "POSSIBLE")
        self.assertEqual(result.category, "devops")

    def test_hard_negative_without_include_is_no_match(self):
        from harvest.models import HarvestFilterSnapshot
        from harvest.role_filter import classify_title

        snapshot = HarvestFilterSnapshot.create_snapshot()
        result = classify_title(
            title="Registered Nurse",
            categories=snapshot.get_categories(),
            hard_negatives=snapshot.get_hard_negatives(),
            snapshot_id=str(snapshot.snapshot_id),
        )

        self.assertEqual(result.decision, "NO_MATCH")

    def test_backfill_eligible_excludes_cold_and_skipped_rows(self):
        from harvest.tasks import _backfill_eligible_queryset

        cold = self._raw_job(url_hash="cold", original_url="https://selective.example/jobs/cold", is_cold=True)
        skipped = self._raw_job(
            url_hash="skipped",
            original_url="https://selective.example/jobs/skipped",
            jd_fetch_skipped=True,
        )
        eligible = self._raw_job(
            url_hash="eligible",
            original_url="https://selective.example/jobs/eligible",
        )

        qs = _backfill_eligible_queryset(None)
        self.assertFalse(qs.filter(pk=cold.pk).exists())
        self.assertFalse(qs.filter(pk=skipped.pk).exists())
        self.assertTrue(qs.filter(pk=eligible.pk).exists())

    def test_ready_stage_excludes_test_filtered_and_weak_domain_rows(self):
        from harvest.models import RawJob
        from harvest.services.rawjob_query import ready_stage_q

        common = {
            "description": "Detailed description with enough content for the ready gate.",
            "category_confidence": 0.95,
            "word_count": 220,
        }
        ready = self._raw_job(
            url_hash="strict-ready",
            original_url="https://selective.example/jobs/strict-ready",
            title="DevOps Engineer",
            job_domain="devops-engineer",
            filter_decision="STRONG",
            **common,
        )
        weak_domain = self._raw_job(
            url_hash="strict-weak",
            original_url="https://selective.example/jobs/strict-weak",
            title="Sales Operations Manager",
            job_domain="general-business",
            filter_decision="STRONG",
            **common,
        )
        filtered = self._raw_job(
            url_hash="strict-filtered",
            original_url="https://selective.example/jobs/strict-filtered",
            title="Warehouse Associate",
            job_domain="devops-engineer",
            filter_decision="NO_MATCH",
            is_cold=True,
            jd_fetch_skipped=True,
            **common,
        )
        test_row = self._raw_job(
            url_hash="strict-test",
            original_url="https://selective.example/jobs/strict-test",
            title="DevOps Engineer",
            job_domain="devops-engineer",
            filter_decision="STRONG",
            is_test_run=True,
            **common,
        )

        qs = RawJob.objects.filter(ready_stage_q())
        self.assertTrue(qs.filter(pk=ready.pk).exists())
        self.assertFalse(qs.filter(pk=weak_domain.pk).exists())
        self.assertFalse(qs.filter(pk=filtered.pk).exists())
        self.assertFalse(qs.filter(pk=test_row.pk).exists())
        self.assertEqual(filtered.pipeline_stage_label(), "FILTERED OUT")

    def test_selective_gui_title_tester_requires_superuser_and_returns_decision(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser("admin@example.com", "admin@example.com", "pw")
        self.client.force_login(user)
        response = self.client.get(reverse("harvest-title-test-api"), {"title": "Site Reliability Engineer"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "STRONG")
        self.assertEqual(response.json()["category"], "devops")

    def test_commands_support_dry_run_paths(self):
        from harvest.models import HarvestFilterSnapshot, HarvestSkippedTitle

        raw = self._raw_job(filter_decision="NO_MATCH", jd_fetch_skipped=True, is_cold=True)
        HarvestSkippedTitle.objects.create(
            raw_job=raw,
            company_name=self.company.name,
            platform_slug=self.platform.slug,
            job_title=raw.title,
            filter_decision="NO_MATCH",
            filter_reason="test",
            is_sampled=True,
        )
        snapshot = HarvestFilterSnapshot.create_snapshot()

        out = StringIO()
        call_command("audit_cold_sample", "--dry-run", stdout=out)
        self.assertIn("NO_MATCH", out.getvalue())

        out = StringIO()
        call_command("export_filter_snapshot", "--snapshot-id", str(snapshot.snapshot_id), stdout=out)
        self.assertIn("phrase_hash", out.getvalue())

        out = StringIO()
        call_command("purge_skipped_titles", "--days", "1", "--dry-run", stdout=out)
        self.assertIn("dry_run=True", out.getvalue())

    def test_title_tester_does_not_create_persistent_snapshot(self):
        from django.contrib.auth import get_user_model
        from harvest.models import HarvestFilterSnapshot

        user = get_user_model().objects.create_superuser("snap@example.com", "snap@example.com", "pw")
        self.client.force_login(user)
        before = HarvestFilterSnapshot.objects.count()
        response = self.client.get(reverse("harvest-title-test-api"), {"title": "Site Reliability Engineer"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(HarvestFilterSnapshot.objects.count(), before)

    def test_classify_existing_does_not_mark_existing_jd_as_skipped(self):
        from harvest.models import RawJob

        raw = self._raw_job(
            url_hash="has-jd",
            original_url="https://selective.example/jobs/has-jd",
            title="Registered Nurse",
            description="Existing description was fetched before classification.",
            has_description=True,
        )
        call_command("classify_existing_rawjobs", "--limit", "1")

        raw.refresh_from_db()
        self.assertEqual(raw.filter_decision, "NO_MATCH")
        self.assertTrue(raw.is_cold)
        self.assertFalse(raw.jd_fetch_skipped)

    def test_reclassify_stale_can_include_unclassified_null_snapshot_rows(self):
        raw = self._raw_job(
            url_hash="null-snapshot",
            original_url="https://selective.example/jobs/null-snapshot",
            title="Registered Nurse",
            filter_decision=None,
            filter_snapshot_id=None,
        )
        call_command("reclassify_stale_rawjobs", "--include-unclassified", "--limit", "1")

        raw.refresh_from_db()
        self.assertEqual(raw.filter_decision, "NO_MATCH")
        self.assertIsNotNone(raw.filter_snapshot_id)

    def test_fetch_all_bypasses_selective_jd_skip(self):
        from harvest.models import HarvestEngineConfig, RawJob
        from harvest.tasks import fetch_raw_jobs_for_company_task

        cfg = HarvestEngineConfig.get()
        cfg.selective_filter_enabled = True
        cfg.filter_audit_mode = False
        cfg.hard_negative_phrases = ["registered nurse"]
        cfg.save()
        self.platform.title_in_list = True
        self.platform.save(update_fields=["title_in_list"])

        class _FakeHarvester:
            last_total_available = 1
            last_detail_fetched = 1

            def fetch_jobs(self, *args, **kwargs):
                return [{
                    "original_url": "https://selective.example/jobs/full-fetch",
                    "apply_url": "https://selective.example/jobs/full-fetch",
                    "external_id": "full-fetch",
                    "title": "Registered Nurse",
                    "company_name": "Selective Co",
                    "description": "Full fetch must keep this detail text.",
                }]

        with patch("harvest.harvesters.get_harvester", return_value=_FakeHarvester()):
            out = fetch_raw_jobs_for_company_task.apply(
                kwargs={"label_pk": self.label.pk, "fetch_all": True}
            ).get()

        raw = RawJob.objects.get(platform_label=self.label, external_id="full-fetch")
        self.assertEqual(raw.filter_decision, "NO_MATCH")
        self.assertFalse(raw.is_cold)
        self.assertFalse(raw.jd_fetch_skipped)
        self.assertIn("Full fetch", raw.description)
        self.assertTrue(out["filter"]["fetch_all_bypass"])

    def test_title_not_in_list_platform_classifies_after_detail_without_marking_jd_skipped(self):
        from harvest.models import HarvestEngineConfig, JobBoardPlatform, RawJob
        from harvest.tasks import fetch_raw_jobs_for_company_task

        platform = JobBoardPlatform.objects.create(
            name="Detail First ATS",
            slug="detail-first-ats",
            title_in_list=False,
            is_enabled=True,
        )
        self.label.platform = platform
        self.label.save(update_fields=["platform"])
        cfg = HarvestEngineConfig.get()
        cfg.selective_filter_enabled = True
        cfg.filter_audit_mode = False
        cfg.hard_negative_phrases = ["registered nurse"]
        cfg.save()

        class _FakeHarvester:
            last_total_available = 1
            last_detail_fetched = 1

            def fetch_jobs(self, *args, **kwargs):
                return [{
                    "original_url": "https://selective.example/jobs/detail-first",
                    "apply_url": "https://selective.example/jobs/detail-first",
                    "external_id": "detail-first",
                    "title": "Registered Nurse",
                    "company_name": "Selective Co",
                    "description": "Detail was already fetched on this platform.",
                }]

        with patch("harvest.harvesters.get_harvester", return_value=_FakeHarvester()):
            fetch_raw_jobs_for_company_task.apply(kwargs={"label_pk": self.label.pk}).get()

        raw = RawJob.objects.get(platform_label=self.label, external_id="detail-first")
        self.assertEqual(raw.filter_decision, "NO_MATCH")
        self.assertTrue(raw.is_cold)
        self.assertFalse(raw.jd_fetch_skipped)
        self.assertIn("Detail was already fetched", raw.description)

    def test_manual_recovery_single_fetch_queues_company_fallback_when_single_fetch_misses(self):
        from harvest.tasks import backfill_single_rawjob_description_task

        raw = self._raw_job(
            url_hash="recover-fallback",
            original_url="https://selective.example/jobs/recover-fallback",
            filter_decision="POSSIBLE",
            is_cold=False,
            jd_fetch_skipped=False,
        )
        with patch(
            "harvest.tasks._backfill_process_one_job",
            return_value=("skipped", {"status": "skipped", "reason": "No description"}),
        ), patch(
            "harvest.tasks.fetch_raw_jobs_for_company_task.apply_async",
            return_value=SimpleNamespace(id="fallback-task"),
        ) as mocked:
            result = backfill_single_rawjob_description_task(raw.pk)

        self.assertEqual(result["fallback_company_fetch_task_id"], "fallback-task")
        self.assertTrue(mocked.called)


class RawJobPayloadArchiveTests(TestCase):
    def _raw_job(self):
        from companies.models import Company
        from harvest.models import RawJob

        company = Company.objects.create(name="Payload Test Co")
        return RawJob.objects.create(
            company=company,
            company_name=company.name,
            title="Software Engineer",
            url_hash="payload-archive-test",
            original_url="https://jobs.example.com/role?token=secret-token",
            platform_slug="workday",
        )

    def test_snapshot_redacts_sensitive_values_and_compresses_html(self):
        from harvest.models import RawJobPayloadSnapshot
        from harvest.payload_archive import capture_rawjob_payload_snapshot

        raw = self._raw_job()
        snap = capture_rawjob_payload_snapshot(
            raw,
            payload={
                "title": "Software Engineer",
                "api_key": "abc123",
                "applyUrl": "https://jobs.example.com/apply?signature=abc&job=1",
                "contact": "recruiter@example.com",
            },
            raw_html="<html>Call 212-555-1212</html>",
            payload_kind=RawJobPayloadSnapshot.PayloadKind.DETAIL,
        )

        self.assertIsNotNone(snap)
        self.assertEqual(snap.payload["api_key"], "[REDACTED]")
        self.assertIn("signature=%5BREDACTED%5D", snap.payload["applyUrl"])
        self.assertEqual(snap.payload["contact"], "[REDACTED_EMAIL]")
        self.assertIn("[REDACTED_PHONE]", snap.raw_html)
        self.assertGreater(snap.raw_html_size_bytes, 0)

    def test_snapshot_dedupes_same_job_kind_and_content_hash(self):
        from harvest.models import RawJobPayloadSnapshot
        from harvest.payload_archive import capture_rawjob_payload_snapshot

        raw = self._raw_job()
        payload = {"id": "REQ-1", "location": "Indianapolis, IN"}
        first = capture_rawjob_payload_snapshot(
            raw,
            payload=payload,
            payload_kind=RawJobPayloadSnapshot.PayloadKind.LIST,
        )
        second = capture_rawjob_payload_snapshot(
            raw,
            payload=dict(payload),
            payload_kind=RawJobPayloadSnapshot.PayloadKind.LIST,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(raw.payload_snapshots.count(), 1)

    def test_source_payloads_capture_list_and_detail_before_fallback(self):
        from harvest.models import RawJobPayloadSnapshot
        from harvest.payload_archive import capture_rawjob_source_payloads

        raw = self._raw_job()
        snapshots = capture_rawjob_source_payloads(
            raw,
            {
                "raw_payload": {"normalized": True},
                "source_payloads": [
                    {
                        "kind": "list",
                        "payload": {"id": "REQ-1", "title": "List title"},
                        "metadata": {"source": "list_api"},
                    },
                    {
                        "kind": "detail",
                        "payload": {"id": "REQ-1", "description": "Detail body"},
                        "metadata": {"source": "detail_api"},
                    },
                ],
            },
            default_source_url="https://jobs.example.com/role",
            default_platform_slug="workday",
        )

        self.assertEqual(len(snapshots), 2)
        kinds = set(raw.payload_snapshots.values_list("payload_kind", flat=True))
        self.assertEqual(kinds, {RawJobPayloadSnapshot.PayloadKind.LIST, RawJobPayloadSnapshot.PayloadKind.DETAIL})
        self.assertFalse(raw.payload_snapshots.filter(payload__normalized=True).exists())

    def test_source_payloads_fallback_to_legacy_raw_payload(self):
        from harvest.models import RawJobPayloadSnapshot
        from harvest.payload_archive import capture_rawjob_source_payloads

        raw = self._raw_job()
        snapshots = capture_rawjob_source_payloads(
            raw,
            {"raw_payload": {"legacy": True}},
            default_payload_kind=RawJobPayloadSnapshot.PayloadKind.API_RESPONSE,
        )

        self.assertEqual(len(snapshots), 1)
        self.assertTrue(raw.payload_snapshots.filter(payload__legacy=True).exists())


class SmokeTestHarvestCommandTests(TestCase):
    """Dry-run must not require network or Celery."""

    def test_smoke_test_harvest_dry_run_exits_zero(self):
        out = StringIO()
        err = StringIO()
        try:
            call_command("smoke_test_harvest", "--dry-run", stdout=out, stderr=err)
        except SystemExit as e:
            self.fail(f"smoke_test_harvest --dry-run raised SystemExit({e.code})")
        self.assertIn("Dry run finished", out.getvalue())


class JarvisPlatformApiExtractionTests(SimpleTestCase):
    """Verify Jarvis _platform_api paths populate description (backfill relies on this)."""

    def test_workday_detail_api_maps_job_description(self):
        jarvis = JobJarvis()
        wd_url = (
            "https://acme.wd1.myworkdayjobs.com/en-US/Search/job/"
            "Remote-Engineer_R_99999"
        )
        detail_resp = {
            "jobPostingInfo": {
                "title": "Remote Engineer",
                "location": "Remote",
                "externalJobId": "R_99999",
                "jobDescription": "<p>Workday JD body</p>",
            },
        }
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = detail_resp
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._workday(wd_url)
        self.assertIsNotNone(out)
        self.assertIn("Workday JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Remote Engineer")

    def test_workday_search_fallback_when_detail_404s(self):
        jarvis = JobJarvis()
        wd_url = (
            "https://3m.wd1.myworkdayjobs.com/Search/job/"
            "US-MN/Engineer_R01049764"
        )
        # Detail returns 404
        detail_404 = MagicMock()
        detail_404.ok = False
        detail_404.status_code = 404

        # Search returns a result with description in search data
        search_resp = MagicMock()
        search_resp.ok = True
        search_resp.json.return_value = {
            "jobPostings": [{
                "title": "Manufacturing Engineer",
                "externalPath": "/job/US-MN/Engineer_R01049764",
                "locationsText": "Maplewood, MN",
                "bulletFields": ["R01049764"],
                "jobDescription": {"content": "<p>Workday search JD</p>"},
            }]
        }

        # Detail for correct path returns full JD
        detail_ok = MagicMock()
        detail_ok.ok = True
        detail_ok.json.return_value = {
            "jobPostingInfo": {
                "title": "Manufacturing Engineer",
                "location": "Maplewood, MN",
                "jobDescription": "<p>Full Workday JD from detail</p>",
            }
        }

        call_count = {"n": 0}
        def mock_get(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return detail_404  # first detail call fails
            return detail_ok  # second detail call succeeds

        with patch.object(jarvis._session, "get", side_effect=mock_get):
            with patch.object(jarvis._session, "post", return_value=search_resp):
                out = jarvis._workday(wd_url)
        self.assertIsNotNone(out)
        self.assertIn("Full Workday JD", out.get("description", ""))

    def test_smartrecruiters_normalize_posting_id_strips_seo_slug(self):
        from harvest.jarvis import _smartrecruiters_normalize_posting_id

        self.assertEqual(
            _smartrecruiters_normalize_posting_id(
                "744000121421842-mgr-strategic-rebids-930951-",
            ),
            "744000121421842",
        )
        self.assertEqual(
            _smartrecruiters_normalize_posting_id("111222333"),
            "111222333",
        )

    def test_smartrecruiters_accepts_rest_api_url(self):
        """Apply links sometimes store api.smartrecruiters.com/v1/companies/.../postings/id."""
        jarvis = JobJarvis()
        url = "https://api.smartrecruiters.com/v1/companies/WesternDigital/postings/744000112340137"
        captured = {}

        def fake_get(u, **kwargs):
            captured["u"] = u
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "id": "744000112340137",
                "name": "Engineer",
                "ref": "https://jobs.smartrecruiters.com/WesternDigital/744000112340137",
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>API body</p>"},
                    }
                },
            }
            return resp

        with patch.object(jarvis, "_http_get", side_effect=fake_get):
            out = jarvis._smartrecruiters(url)
        self.assertIsNotNone(out)
        self.assertIn("WesternDigital", captured.get("u", ""))
        self.assertIn("/postings/744000112340137", captured.get("u", ""))
        self.assertIn("API body", out.get("description", ""))

    def test_smartrecruiters_api_request_strips_seo_slug_from_url(self):
        """Detail API must receive numeric id only, not ``744...-title-slug``."""
        from harvest.jarvis import _smartrecruiters_normalize_posting_id

        jarvis = JobJarvis()
        url = "https://jobs.smartrecruiters.com/DemoCo/744000121421842-mgr-title-"
        captured = {}

        def fake_get(u, **kwargs):
            captured["detail_url"] = u
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "id": "744000121421842",
                "name": "Role",
                "ref": url,
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>Body</p>"},
                    }
                },
            }
            return resp

        with patch.object(jarvis, "_http_get", side_effect=fake_get):
            out = jarvis._smartrecruiters(url)
        self.assertIsNotNone(out)
        self.assertIn("/postings/744000121421842", captured.get("detail_url", ""))
        self.assertNotIn("mgr-title", captured.get("detail_url", ""))
        self.assertIn("Body", out.get("description", ""))
        self.assertEqual(
            _smartrecruiters_normalize_posting_id("744000121421842-mgr-title-"),
            "744000121421842",
        )

    def test_smartrecruiters_detail_maps_sections(self):
        jarvis = JobJarvis()
        url = "https://jobs.smartrecruiters.com/DemoCo/111222333"
        detail = {
            "name": "QA Role",
            "ref": url,
            "location": {"city": "Austin", "region": "TX", "country": "US"},
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": "<p>SR JD</p>"},
                    "qualifications": {"text": "<p>Reqs</p>"},
                }
            },
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = detail
        with patch.object(jarvis, "_http_get", return_value=mock_resp):
            out = jarvis._smartrecruiters(url)
        self.assertIsNotNone(out)
        self.assertIn("SR JD", out.get("description", ""))
        self.assertIn("Reqs", out.get("requirements", ""))

    def test_recruitee_offers_list_matches_slug(self):
        jarvis = JobJarvis()
        url = "https://widgets.recruitee.com/o/backend-engineer"
        offers = {
            "offers": [
                {
                    "id": 42,
                    "slug": "backend-engineer",
                    "title": "Backend Engineer",
                    "description": "<p>Recruitee JD</p>",
                    "requirements": "",
                    "city": "Berlin",
                    "country": "DE",
                    "careers_url": url,
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = offers
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._recruitee(url)
        self.assertIsNotNone(out)
        self.assertIn("Recruitee JD", out.get("description", ""))

    def test_bamboohr_detail_json_maps_description(self):
        jarvis = JobJarvis()
        url = "https://acme.bamboohr.com/careers/12345"
        payload = {
            "result": {
                "jobOpening": {
                    "description": "<p>Bamboo JD</p>",
                    "jobTitle": "Analyst",
                    "location": {"city": "NYC", "state": "NY", "addressCountry": "US"},
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = payload
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._bamboohr(url)
        self.assertIsNotNone(out)
        self.assertIn("Bamboo JD", out.get("description", ""))
        self.assertEqual(out.get("title"), "Analyst")

    def test_icims_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://careers-acme.icims.com/jobs/12345/job"
        html = (
            '<html><body>'
            '<h1 class="iCIMS_JobTitle">Software Engineer</h1>'
            '<div class="iCIMS_JobContent"><p>iCIMS JD body that is long enough to pass minimum threshold of 72 chars easily here.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._icims(url)
        self.assertIsNotNone(out)
        self.assertIn("iCIMS JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Software Engineer")

    def test_jobvite_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://jobs.jobvite.com/acmecorp/job/oABC123"
        html = (
            '<html><body>'
            '<h2 class="jv-header">QA Lead</h2>'
            '<p class="jv-job-detail-meta">'
            'Professional Staff<span class="jv-inline-separator"></span>Hybrid Remote<span>,</span>'
            'Seattle,<br>Washington<span class="jv-inline-separator"></span>'
            'Los Angeles,<br>California<span class="jv-inline-separator"></span>'
            'San Francisco,<br>California'
            '</p>'
            '<div class="jv-job-detail-description"><p>Jobvite JD body here with plenty of text to pass the minimum character threshold easily.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._jobvite(url)
        self.assertIsNotNone(out)
        self.assertIn("Jobvite JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "QA Lead")
        self.assertIn("Seattle, Washington", out.get("location_candidates", []))
        self.assertIn("Los Angeles, California", out.get("location_candidates", []))

    def test_taleo_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://aa224.taleo.net/careersection/ex/jobdetail.ftl?job=12345&lang=en"
        html = (
            '<html><body>'
            '<h1 id="requisitionDescriptionInterface.reqTitleLinkAction.row1">PM Role</h1>'
            '<div id="requisitionDescriptionInterface.ID1702.row1"><p>Taleo JD body with enough content to pass the seventy two character minimum threshold.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._taleo(url)
        self.assertIsNotNone(out)
        self.assertIn("Taleo JD body", out.get("description", ""))

    def test_ultipro_embedded_json_in_html(self):
        jarvis = JobJarvis()
        url = (
            "https://recruiting.ultipro.com/INT1043EXCUR/JobBoard/"
            "ad5e5978-552f-4ef7-90c8-70ebb0a57994/OpportunityDetail"
            "?opportunityId=c19385b5-7296-4f1d-88d8-3cbf7507693f"
        )
        html = (
            "<html><script>\n"
            'var opportunity = new US.Opportunity.CandidateOpportunityDetail('
            '{"Title":"Finance Intern",'
            '"Description":"<p>UKG UltiPro full JD body with enough text for tests.</p>",'
            '"Locations":[{"LocalizedDescription":"Scottsdale HQ",'
            '"Address":{"City":"Scottsdale","State":{"Code":"AZ"},"Country":{"Code":"USA"}}}]}'
            ");\n</script></html>"
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        mock_resp.url = url
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._ultipro(url)
        self.assertIsNotNone(out)
        self.assertIn("UKG UltiPro full JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Finance Intern")
        self.assertIn("Scottsdale", out.get("location_raw", ""))

    def test_workday_detail_returns_multi_location_candidates(self):
        from harvest.harvesters.workday import _fetch_workday_detail

        session = MagicMock()
        response = MagicMock()
        response.ok = True
        response.json.return_value = {
            "jobPostingInfo": {
                "jobDescription": "Workday JD body",
                "locationsText": "2 Locations",
                "postingLocations": [
                    {"locationName": "Seattle, Washington"},
                    {"locationName": "Toronto, Ontario, Canada"},
                ],
            }
        }
        session.get.return_value = response

        out = _fetch_workday_detail(session, "acme.wd5", "acme", "External", "/job/123")

        self.assertEqual(out.get("description"), "Workday JD body")
        self.assertIn("Seattle, Washington", out.get("location_candidates", []))
        self.assertIn("Toronto, Ontario, Canada", out.get("location_candidates", []))

    def test_oracle_ce_rest_api(self):
        jarvis = JobJarvis()
        url = "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/300001"
        api_resp = {
            "items": [{
                "Title": "Oracle Dev",
                "ExternalDescriptionStr": "<p>Oracle JD body</p>",
                "PrimaryLocation": "Redwood City, CA",
                "Organization": "Engineering",
            }]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = api_resp
        with patch.object(jarvis._session, "get", return_value=mock_resp) as mock_get:
            out = jarvis._oracle(url)
        self.assertIsNotNone(out)
        self.assertIn("Oracle JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Oracle Dev")
        finder = mock_get.call_args.kwargs.get("params", {}).get("finder", "")
        self.assertIn("ById;", finder)
        self.assertNotIn("findReqDetails", finder)

    def test_oracle_ce_rest_api_maps_detail_metrics(self):
        jarvis = JobJarvis()
        url = "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/300001"
        api_resp = {
            "items": [{
                "Title": "Oracle RN",
                "ExternalDescriptionStr": "<p>Oracle full JD body</p>",
                "ExternalQualificationsStr": "RN required",
                "ExternalResponsibilitiesStr": "Deliver patient care",
                "Organization": "Nursing",
                "JobCategory": "Nursing",
                "StudyLevel": "Associate Degree",
                "JobSchedule": "Full time",
                "JobShift": "Evening",
                "JobIdentification": "146728",
                "ExternalPostedStartDate": "2026-05-01T15:36:26+00:00",
                "workLocation": [{
                    "AddressLine1": "3100 Oak Grove Rd",
                    "TownOrCity": "Poplar Bluff",
                    "Region2": "MO",
                    "PostalCode": "63901",
                    "Country": "US",
                }],
                "PrimaryLocationCountry": "US",
            }]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = api_resp
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._oracle(url)
        self.assertIsNotNone(out)
        self.assertIn("Oracle full JD body", out.get("description", ""))
        self.assertEqual(out.get("requirements"), "RN required")
        self.assertEqual(out.get("responsibilities"), "Deliver patient care")
        self.assertEqual(out.get("vendor_job_identification"), "146728")
        self.assertEqual(out.get("vendor_job_category"), "Nursing")
        self.assertEqual(out.get("vendor_degree_level"), "Associate Degree")
        self.assertEqual(out.get("vendor_job_schedule"), "Full time")
        self.assertEqual(out.get("vendor_job_shift"), "Evening")
        self.assertIn("3100 Oak Grove Rd", out.get("vendor_location_block", ""))
        self.assertEqual(out.get("education_required"), "ASSOCIATE")
        self.assertEqual(out.get("city"), "Poplar Bluff")
        self.assertEqual(out.get("state"), "MO")
        self.assertEqual(out.get("country"), "United States")
        self.assertEqual(out.get("posted_date_raw"), "2026-05-01T15:36:26+00:00")


class OracleHarvesterTests(SimpleTestCase):
    def test_oracle_harvester_uses_detail_payload_for_full_metrics(self):
        harvester = OracleHCMHarvester()
        company = MagicMock(name="Demo Co")
        company.name = "Demo Co"
        list_resp = {
            "items": [{
                "TotalJobsCount": 1,
                "requisitionList": [{
                    "Id": "146728",
                    "Title": "ER RN Evening",
                    "primaryLocation": {
                        "City": "Poplar Bluff",
                        "State": "MO",
                        "Country": "US",
                    },
                }],
            }]
        }
        detail_resp = {
            "items": [{
                "Id": "146728",
                "Title": "ER RN Evening",
                "ExternalDescriptionStr": "<p>Job Summary</p><p>Full JD body</p>",
                "ExternalQualificationsStr": "RN license required",
                "ExternalResponsibilitiesStr": "Provide patient-centered care",
                "Organization": "Nursing",
                "JobIdentification": "146728",
                "JobCategory": "Nursing",
                "PostedDate": "2026-05-01T10:36:00Z",
                "StudyLevel": "Associate Degree",
                "JobSchedule": "Full time",
                "JobShift": "Evening",
                "ExternalPostedStartDate": "2026-05-01T15:36:26+00:00",
                "workLocation": [{
                    "AddressLine1": "3100 Oak Grove Rd",
                    "TownOrCity": "Poplar Bluff",
                    "Region2": "MO",
                    "PostalCode": "63901",
                    "Country": "US",
                }],
                "PrimaryLocationCountry": "US",
            }]
        }
        with patch.object(harvester, "_get", side_effect=[list_resp, detail_resp]):
            jobs = harvester.fetch_jobs(company, "eeho.fa.us2|CX", fetch_all=True)

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertIn("Full JD body", job["description"])
        self.assertEqual(job["requirements"], "RN license required")
        self.assertEqual(job["responsibilities"], "Provide patient-centered care")
        self.assertEqual(job["job_category"], "Nursing")
        self.assertEqual(job["education_required"], "ASSOCIATE")
        self.assertEqual(job["schedule_type"], "Full time")
        self.assertEqual(job["shift_schedule"], "Evening")
        self.assertEqual(job["vendor_job_identification"], "146728")
        self.assertEqual(job["vendor_job_category"], "Nursing")
        self.assertEqual(job["vendor_degree_level"], "Associate Degree")
        self.assertEqual(job["vendor_job_schedule"], "Full time")
        self.assertEqual(job["vendor_job_shift"], "Evening")
        self.assertEqual(job["vendor_location_block"], "3100 Oak Grove Rd, Poplar Bluff, MO, 63901, United States")
        self.assertEqual(job["postal_code"], "63901")
        self.assertEqual(job["country"], "United States")
        self.assertEqual(job["posted_date_raw"], "2026-05-01T15:36:26+00:00")
        self.assertEqual(job["raw_payload"]["source"], "oracle_hcm")
        self.assertIn("detail", job["raw_payload"])

    def test_dayforce_job_detail_api(self):
        jarvis = JobJarvis()
        url = "https://jobs.dayforcehcm.com/en-US/corpay/CANDIDATEPORTAL/jobs/12345"
        api_resp = {
            "JobTitle": "Payroll Analyst",
            "Description": "<p>Dayforce JD body</p>",
            "JobLocation": "Tampa, FL",
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = api_resp
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._dayforce(url)
        self.assertIsNotNone(out)
        self.assertIn("Dayforce JD body", out.get("description", ""))
        self.assertEqual(out.get("title"), "Payroll Analyst")

    def test_dayforce_next_data_fallback_for_modern_board_urls(self):
        jarvis = JobJarvis()
        url = "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE/jobs/6503?src=LinkedIn"
        next_data = {
            "props": {
                "pageProps": {
                    "jobData": {
                        "jobTitle": "Platform Engineer",
                        "jobReqId": "6503",
                        "postingLocations": [
                            {"formattedAddress": "Austin, Texas, United States of America"},
                            {"formattedAddress": "Tempe, Arizona, United States of America"},
                        ],
                        "jobPostingAttributes": [{"name": "JobFamily", "value": "Technology"}],
                        "postingStartTimestampUTC": "2026-04-02T03:00:00Z",
                        "jobPostingContent": {
                            "jobDescription": "<p>Lead and build secure cloud platforms.</p>",
                            "jobDescriptionFooter": "<p>Benefits package and growth opportunities.</p>",
                        },
                    }
                }
            },
            "query": {"clientNamespace": "kestra"},
        }
        html = (
            '<html><head></head><body>'
            '<script id="__NEXT_DATA__" type="application/json">'
            f"{json.dumps(next_data)}"
            "</script></body></html>"
        )

        detail_404 = requests.HTTPError("404")
        html_resp = MagicMock()
        html_resp.raise_for_status = MagicMock()
        html_resp.text = html
        html_resp.url = url

        def fake_get(target_url, *args, **kwargs):
            if "/api/geo/" in target_url:
                raise detail_404
            return html_resp

        with patch.object(jarvis, "_http_get", side_effect=fake_get):
            out = jarvis._dayforce(url)

        self.assertIsNotNone(out)
        self.assertEqual(out.get("title"), "Platform Engineer")
        self.assertEqual(out.get("company_name"), "Kestra")
        self.assertEqual(out.get("department"), "Technology")
        self.assertEqual(out.get("external_id"), "6503")
        self.assertIn("Austin, Texas", out.get("location_raw", ""))
        self.assertIn("Lead and build secure cloud platforms", out.get("description", ""))
        self.assertIn("Benefits package", out.get("description", ""))

    def test_breezy_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://acme.breezy.hr/p/abc123-software-dev"
        html = (
            '<html><body>'
            '<h1>Software Dev</h1>'
            '<div class="description"><p>Breezy JD body with enough words to clear the seventy two character minimum check easily.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._breezy(url)
        self.assertIsNotNone(out)
        self.assertIn("Breezy JD body", out.get("description", ""))

    def test_teamtailor_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://career.teamtailor.com/jobs/12345-qa-engineer"
        html = (
            '<html><body>'
            '<h1>QA Engineer</h1>'
            '<div class="job-description"><p>Teamtailor JD body with plenty of characters to easily clear the minimum threshold.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._teamtailor(url)
        self.assertIsNotNone(out)
        self.assertIn("Teamtailor JD body", out.get("description", ""))

    def test_zoho_detail_page_scrape(self):
        jarvis = JobJarvis()
        url = "https://jobs.zoho.com/portal/acme/apply/123"
        html = (
            '<html><body>'
            '<h1>Data Analyst</h1>'
            '<div class="job-description"><p>Zoho JD body with sufficient text content to pass the seventy-two character minimum check.</p></div>'
            '</body></html>'
        )
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = html
        with patch.object(jarvis._session, "get", return_value=mock_resp):
            out = jarvis._zoho(url)
        self.assertIsNotNone(out)
        self.assertIn("Zoho JD body", out.get("description", ""))


class JarvisFetchGateTests(SimpleTestCase):
    """JarvisFetchGate: retries and concurrency wrapper for outbound HTTP."""

    def test_retries_502_then_success(self):
        from unittest.mock import MagicMock, patch

        from harvest.http_limits import JarvisFetchGate

        gate = JarvisFetchGate(50, 10, 3, 0.01)
        session = MagicMock()
        bad = MagicMock()
        bad.status_code = 502
        good = MagicMock()
        good.status_code = 200
        session.get.side_effect = [bad, good]
        with patch("harvest.http_limits.time.sleep"):
            r = gate.request(session, "GET", "https://example.com/job/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(session.get.call_count, 2)

    def test_no_retry_on_404(self):
        from unittest.mock import MagicMock, patch

        from harvest.http_limits import JarvisFetchGate

        gate = JarvisFetchGate(50, 10, 3, 0.01)
        session = MagicMock()
        nf = MagicMock()
        nf.status_code = 404
        session.get.return_value = nf
        with patch("harvest.http_limits.time.sleep"):
            r = gate.request(session, "GET", "https://example.com/missing")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(session.get.call_count, 1)


class SmartRecruitersSupportTests(SimpleTestCase):
    """Canonical API URLs from list payload — avoids case-sensitive slug mismatches."""

    def test_backfill_fetch_url_uses_company_identifier_from_payload(self):
        from types import SimpleNamespace

        from harvest.smartrecruiters_support import backfill_fetch_url_for_raw_job

        job = SimpleNamespace(
            original_url="https://jobs.smartrecruiters.com/wrongslug/744000112340137",
            external_id="744000112340137",
            raw_payload={
                "company": {"identifier": "WesternDigital", "name": "WD"},
                "id": "744000112340137",
            },
        )
        u = backfill_fetch_url_for_raw_job(job)
        self.assertTrue(u.startswith("https://api.smartrecruiters.com/v1/companies/"))
        self.assertIn("WesternDigital", u)
        self.assertIn("/postings/744000112340137", u)


class BackfillJdEligibilityTests(TestCase):
    """Regression: skipped/failed backfill uses description=' ' — must stay eligible."""

    def test_space_placeholder_description_remains_in_backfill_queue(self):
        import hashlib

        from companies.models import Company

        from harvest.models import RawJob
        from harvest.tasks import _backfill_eligible_queryset

        c = Company.objects.create(name="BackfillEligTestCo")
        url = "https://example.com/job/backfill-elig-1"
        h = hashlib.sha256(url.encode()).hexdigest()
        j = RawJob.objects.create(
            company=c,
            title="Test",
            url_hash=h,
            original_url=url,
            description=" ",
            is_priority=True,
        )
        self.assertTrue(_backfill_eligible_queryset(None).filter(pk=j.pk).exists())


class SyncRawJobsToPoolTests(TestCase):
    """Phase 5: sync_harvested_to_pool_task now reads RawJob directly."""

    def setUp(self):
        from companies.models import Company
        from core.models import PlatformConfig
        from harvest.models import JobBoardPlatform
        from users.models import User

        self.user = User.objects.create_user(
            username="sync_mirror_admin",
            email="sync_mirror@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.company = Company.objects.create(name="SyncMirrorCo")
        self.platform = JobBoardPlatform.objects.create(
            name="Sync Mirror Plat",
            slug="sync-mirror-plat",
        )
        config = PlatformConfig.load()
        config.dual_classification_require_approval_for_sync = False
        config.save()

    def test_pool_sync_creates_job_from_raw_job(self):
        import hashlib
        from harvest.models import RawJob
        from harvest.tasks import sync_harvested_to_pool_task
        from jobs.models import Job

        url = "https://example.com/careers/sync-mirror-unique-99"
        h = hashlib.sha256(url.strip().encode()).hexdigest()
        raw = RawJob.objects.create(
            company=self.company,
            title="Engineer",
            url_hash=h,
            original_url=url,
            description=(
                "We are hiring a platform engineer to design, build, and maintain "
                "reliable cloud infrastructure, automation pipelines, observability "
                "dashboards, incident response processes, and secure deployment "
                "patterns across distributed systems. You will collaborate with "
                "engineering and operations teams, improve CI/CD workflows, enforce "
                "best practices, and support production services with clear runbooks "
                "and operational excellence. The role includes Linux operations, "
                "infrastructure as code, monitoring, alert tuning, service-level "
                "objectives, incident retrospectives, secure networking, and "
                "change management. Candidates should demonstrate scripting ability, "
                "cloud platform experience, strong communication, and ownership of "
                "production reliability initiatives across multiple environments."
            ),
            sync_status="PENDING",
            is_priority=True,
            scope_status=RawJob.ScopeStatus.PRIORITY_TARGET,
            country_code="US",
            country_codes=["US"],
        )
        sync_harvested_to_pool_task.apply(kwargs={"max_jobs": 10}).get()
        raw.refresh_from_db()
        self.assertEqual(raw.sync_status, "SYNCED")
        self.assertTrue(Job.objects.filter(url_hash=h).exists())

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    def test_pool_sync_uses_clean_rawjob_description(self, mock_gate, _mock_apply):
        import hashlib
        from harvest.models import RawJob
        from harvest.tasks import sync_harvested_to_pool_task
        from jobs.models import Job

        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )

        url = "https://example.com/careers/sync-clean-description"
        h = hashlib.sha256(url.strip().encode()).hexdigest()
        clean_text = (
            "This posting will be open until June 30, 2026.\n\n"
            "About the Role\nLead clean cloud networking work."
        )
        raw = RawJob.objects.create(
            company=self.company,
            title="Cloud Network Engineer",
            url_hash=h,
            original_url=url,
            description="<p><b>Dirty &amp; noisy</b></p><p>Should not reach Job.</p>",
            description_clean=clean_text,
            sync_status="PENDING",
            is_priority=True,
            scope_status=RawJob.ScopeStatus.PRIORITY_TARGET,
            country_code="US",
            country_codes=["US"],
        )

        sync_harvested_to_pool_task.apply(kwargs={"max_jobs": 10}).get()
        raw.refresh_from_db()
        job = Job.objects.get(url_hash=h)

        self.assertEqual(raw.sync_status, "SYNCED")
        self.assertEqual(job.description, clean_text)
        self.assertNotIn("<p>", job.description)
        self.assertNotIn("&amp;", job.description)

    def test_pool_sync_skipped_duplicate(self):
        import hashlib
        from harvest.models import RawJob
        from harvest.tasks import sync_harvested_to_pool_task
        from jobs.models import Job

        url = "https://example.com/careers/sync-mirror-dup-88"
        h = hashlib.sha256(url.strip().encode()).hexdigest()
        raw = RawJob.objects.create(
            company=self.company,
            title="Engineer",
            url_hash=h,
            original_url=url,
            sync_status="PENDING",
            is_priority=True,
            scope_status=RawJob.ScopeStatus.PRIORITY_TARGET,
            country_code="US",
            country_codes=["US"],
        )
        Job.objects.create(
            title="Already here",
            company=self.company.name,
            company_obj=self.company,
            description="Existing pool job",
            original_link=url,
            posted_by=self.user,
        )
        sync_harvested_to_pool_task.apply(kwargs={"max_jobs": 10}).get()
        raw.refresh_from_db()
        self.assertEqual(raw.sync_status, "SKIPPED")

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    def test_pool_sync_assigns_marketing_role_from_title_when_domain_blank(self, mock_gate, _mock_apply):
        import hashlib
        from harvest.models import RawJob
        from harvest.tasks import sync_harvested_to_pool_task
        from jobs.models import Job
        from jobs.marketing_role_routing import clear_marketing_role_cache

        clear_marketing_role_cache()
        mock_gate.return_value = type(
            "GateResult",
            (),
            {
                "passed": True,
                "lane": "READY",
                "status": "eligible",
                "reason_code": "",
                "reasons": [],
                "checks": {},
                "data_quality_score": 0.9,
                "trust_score": 0.9,
                "candidate_fit_score": 0.9,
                "vet_priority_score": 0.9,
            },
        )()
        url = "https://example.com/careers/sync-mirror-servicenow-77"
        h = hashlib.sha256(url.strip().encode()).hexdigest()
        raw = RawJob.objects.create(
            company=self.company,
            title="ServiceNow Developer",
            url_hash=h,
            original_url=url,
            description=(
                "You will own platform integrations, release workflows, environment "
                "support, stakeholder communication, testing coordination, incident "
                "follow-up, delivery reporting, documentation upkeep, and continuous "
                "process improvements across a busy enterprise team with strong written "
                "communication, backlog ownership, and operational discipline."
            ),
            job_domain="",
            sync_status="PENDING",
            is_priority=True,
            scope_status=RawJob.ScopeStatus.PRIORITY_TARGET,
            country_code="US",
            country_codes=["US"],
        )

        sync_harvested_to_pool_task.apply(kwargs={"max_jobs": 10}).get()
        raw.refresh_from_db()
        self.assertEqual(raw.sync_status, "SYNCED")
        job = Job.objects.get(url_hash=h)
        self.assertIn(
            "servicenow-developer",
            list(job.marketing_roles.values_list("slug", flat=True)),
        )

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    def test_pool_sync_prefers_approved_classification_for_country_department_and_role(self, mock_gate, _mock_apply):
        import hashlib
        from harvest.models import RawJob
        from harvest.tasks import sync_harvested_to_pool_task
        from jobs.models import Job, RawJobClassificationSnapshot
        from jobs.marketing_role_routing import clear_marketing_role_cache

        clear_marketing_role_cache()
        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )

        url = "https://example.com/careers/sync-approved-classification"
        h = hashlib.sha256(url.strip().encode()).hexdigest()
        raw = RawJob.objects.create(
            company=self.company,
            title="Platform Specialist",
            url_hash=h,
            original_url=url,
            description="Strong enterprise platform delivery and workflow ownership.",
            job_domain="",
            department_normalized="",
            country="",
            country_codes=[],
            sync_status="PENDING",
            is_priority=True,
            scope_status=RawJob.ScopeStatus.PRIORITY_TARGET,
            country_code="US",
        )
        RawJobClassificationSnapshot.objects.create(
            raw_job=raw,
            status=RawJobClassificationSnapshot.Status.MERGED,
            approval_state=RawJobClassificationSnapshot.ApprovalState.APPROVED,
            approved_source="SECONDARY",
            approved_output={
                "identity": {"title": "Platform Specialist"},
                "classification": {
                    "job_domain": "servicenow-developer",
                    "department_normalized": "Information Technology",
                },
                "location": {
                    "country": "Canada",
                    "country_codes": ["CA"],
                },
            },
        )

        sync_harvested_to_pool_task.apply(kwargs={"max_jobs": 10}).get()
        raw.refresh_from_db()
        job = Job.objects.get(url_hash=h)

        self.assertEqual(raw.sync_status, "SYNCED")
        self.assertEqual(job.country, "Canada")
        self.assertEqual(job.department, Job.Department.IT_MANAGEMENT)
        self.assertIn(
            "servicenow-developer",
            list(job.marketing_roles.values_list("slug", flat=True)),
        )
        self.assertEqual(
            (raw.raw_payload or {}).get("vet_gate", {}).get("classification_source"),
            "SECONDARY",
        )
        dual_meta = (job.validation_result or {}).get("dual_classification") or {}
        self.assertEqual(dual_meta.get("approved_source"), "SECONDARY")
        self.assertEqual(dual_meta.get("approved_values", {}).get("job_domain"), "servicenow-developer")
        self.assertEqual(dual_meta.get("approved_values", {}).get("country"), "Canada")
        self.assertIn("field_provenance", dual_meta)


class ManualRawJobSyncRoleTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from users.models import User

        self.user = User.objects.create_user(
            username="manual_sync_admin",
            email="manual_sync@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.company = Company.objects.create(name="Manual Sync Co")

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.is_definitive_inactive", return_value=False)
    @patch("harvest.url_health.check_job_posting_live")
    def test_manual_sync_assigns_marketing_role_from_title(
        self,
        mock_live,
        _mock_definitive,
        mock_gate,
        _mock_apply,
    ):
        import hashlib
        from harvest.models import RawJob
        from harvest.views import _sync_rawjob_to_pool
        from jobs.marketing_role_routing import clear_marketing_role_cache

        clear_marketing_role_cache()
        mock_gate.return_value = type(
            "GateResult",
            (),
            {
                "passed": True,
                "lane": "READY",
                "status": "eligible",
                "reason_code": "",
                "reasons": [],
                "checks": {},
                "data_quality_score": 0.9,
                "trust_score": 0.9,
                "candidate_fit_score": 0.9,
                "vet_priority_score": 0.9,
            },
        )()
        mock_live.return_value = type(
            "LiveResult",
            (),
            {
                "is_live": True,
                "reason": "",
                "status_code": 200,
                "final_url": "https://example.com/jobs/manual-servicenow",
            },
        )()

        url = "https://example.com/jobs/manual-servicenow"
        raw = RawJob.objects.create(
            company=self.company,
            title="ServiceNow Developer",
            url_hash=hashlib.sha256(url.encode()).hexdigest(),
            original_url=url,
            description=(
                "Coordinate releases, support platform changes, document workflows, "
                "handle incidents, and partner with business teams across a large "
                "enterprise environment with strong delivery and process ownership."
            ),
            sync_status="PENDING",
        )

        job, created = _sync_rawjob_to_pool(raw, posted_by=self.user)
        self.assertTrue(created)
        self.assertIn(
            "servicenow-developer",
            list(job.marketing_roles.values_list("slug", flat=True)),
        )

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.is_definitive_inactive", return_value=False)
    @patch("harvest.url_health.check_job_posting_live")
    def test_manual_sync_uses_clean_rawjob_description(
        self,
        mock_live,
        _mock_definitive,
        mock_gate,
        _mock_apply,
    ):
        import hashlib
        from harvest.models import RawJob
        from harvest.views import _sync_rawjob_to_pool

        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(
            is_live=True,
            reason="",
            status_code=200,
            final_url="https://example.com/jobs/manual-clean-description",
        )

        url = "https://example.com/jobs/manual-clean-description"
        clean_text = "Clean manual sync description.\n\nSecond paragraph."
        raw = RawJob.objects.create(
            company=self.company,
            title="ServiceNow Developer",
            url_hash=hashlib.sha256(url.encode()).hexdigest(),
            original_url=url,
            description="<p>Dirty &amp; raw HTML</p>",
            description_clean=clean_text,
            sync_status="PENDING",
        )

        job, created = _sync_rawjob_to_pool(raw, posted_by=self.user)

        self.assertTrue(created)
        self.assertEqual(job.description, clean_text)
        self.assertNotIn("<p>", job.description)

    @patch("jobs.gating.apply_gate_result_to_job")
    @patch("jobs.gating.evaluate_raw_job_gate")
    @patch("harvest.url_health.is_definitive_inactive", return_value=False)
    @patch("harvest.url_health.check_job_posting_live")
    def test_manual_sync_prefers_approved_classification_values(
        self,
        mock_live,
        _mock_definitive,
        mock_gate,
        _mock_apply,
    ):
        import hashlib
        from harvest.models import RawJob
        from harvest.views import _sync_rawjob_to_pool
        from jobs.models import Job, RawJobClassificationSnapshot
        from jobs.marketing_role_routing import clear_marketing_role_cache

        clear_marketing_role_cache()
        mock_gate.return_value = SimpleNamespace(
            passed=True,
            lane="READY",
            status="eligible",
            reason_code="",
            reasons=[],
            checks={},
            data_quality_score=0.9,
            trust_score=0.9,
            candidate_fit_score=0.9,
            vet_priority_score=0.9,
        )
        mock_live.return_value = SimpleNamespace(
            is_live=True,
            reason="",
            status_code=200,
            final_url="https://example.com/jobs/manual-approved-classification",
        )

        url = "https://example.com/jobs/manual-approved-classification"
        raw = RawJob.objects.create(
            company=self.company,
            title="Platform Specialist",
            url_hash=hashlib.sha256(url.encode()).hexdigest(),
            original_url=url,
            description="Coordinate releases and enterprise platform operations.",
            job_domain="",
            department_normalized="",
            country="",
            country_codes=[],
            sync_status="PENDING",
        )
        RawJobClassificationSnapshot.objects.create(
            raw_job=raw,
            status=RawJobClassificationSnapshot.Status.MERGED,
            approval_state=RawJobClassificationSnapshot.ApprovalState.APPROVED,
            approved_source="SECONDARY",
            approved_output={
                "classification": {
                    "job_domain": "servicenow-developer",
                    "department_normalized": "Information Technology",
                },
                "location": {
                    "country": "Canada",
                    "country_codes": ["CA"],
                },
            },
        )

        job, created = _sync_rawjob_to_pool(raw, posted_by=self.user)

        self.assertTrue(created)
        self.assertEqual(job.country, "Canada")
        self.assertEqual(job.department, Job.Department.IT_MANAGEMENT)
        self.assertIn(
            "servicenow-developer",
            list(job.marketing_roles.values_list("slug", flat=True)),
        )
        raw.refresh_from_db()
        self.assertEqual(
            (raw.raw_payload or {}).get("vet_gate", {}).get("classification_source"),
            "SECONDARY",
        )


class RepairSyncedJobDescriptionsCommandTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from users.models import User

        self.user = User.objects.create_user(
            username="repair_desc_admin",
            email="repair_desc@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.company = Company.objects.create(name="RepairDescCo")

    def _raw_job(self, suffix: str, *, clean_text: str):
        import hashlib
        from harvest.models import RawJob

        url = f"https://example.com/jobs/repair-description-{suffix}"
        return RawJob.objects.create(
            company=self.company,
            company_name=self.company.name,
            title=f"Repair role {suffix}",
            url_hash=hashlib.sha256(url.encode()).hexdigest(),
            original_url=url,
            description="<p><b>Dirty &amp; raw</b></p>",
            description_clean=clean_text,
            sync_status=RawJob.SyncStatus.SYNCED,
        )

    def test_repair_command_updates_htmlish_synced_jobs_but_skips_manual_edits(self):
        from django.core.management import call_command
        from jobs.models import Job

        raw_dirty = self._raw_job("dirty", clean_text="Clean linked description.")
        dirty_job = Job.objects.create(
            title=raw_dirty.title,
            company=raw_dirty.company_name,
            company_obj=self.company,
            description="<p>Dirty &amp; stored HTML</p>",
            original_link=raw_dirty.original_url,
            posted_by=self.user,
            source_raw_job=raw_dirty,
            url_hash=raw_dirty.url_hash,
        )

        raw_manual = self._raw_job("manual", clean_text="Should not overwrite manual edit.")
        manual_job = Job.objects.create(
            title=raw_manual.title,
            company=raw_manual.company_name,
            company_obj=self.company,
            description="<p>Manual dirty edit</p>",
            original_link=raw_manual.original_url,
            posted_by=self.user,
            last_edited_by=self.user,
            source_raw_job=raw_manual,
            url_hash=raw_manual.url_hash,
        )

        out = StringIO()
        call_command("repair_synced_job_descriptions", stdout=out)

        dirty_job.refresh_from_db()
        manual_job.refresh_from_db()
        self.assertEqual(dirty_job.description, "Clean linked description.")
        self.assertEqual(manual_job.description, "<p>Manual dirty edit</p>")
        self.assertIn("Repaired 1 synced Job description", out.getvalue())


class BackfillJobMarketingRolesCommandTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from users.models import User

        self.user = User.objects.create_user(
            username="role_backfill_admin",
            email="role_backfill@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.company = Company.objects.create(name="Role Backfill Co")

    def test_backfill_command_assigns_title_only_roles_to_synced_jobs(self):
        import hashlib
        from harvest.models import RawJob
        from jobs.models import Job
        from jobs.marketing_role_routing import clear_marketing_role_cache

        clear_marketing_role_cache()
        url = "https://example.com/jobs/backfill-servicenow"
        raw = RawJob.objects.create(
            company=self.company,
            title="ServiceNow Developer",
            url_hash=hashlib.sha256(url.encode()).hexdigest(),
            original_url=url,
            description="",
            sync_status="SYNCED",
            job_domain="",
        )
        job = Job.objects.create(
            title=raw.title,
            company=self.company.name,
            company_obj=self.company,
            description=raw.title,
            original_link=url,
            url_hash=raw.url_hash,
            posted_by=self.user,
            source_raw_job=raw,
        )

        call_command("backfill_job_marketing_roles")
        job.refresh_from_db()

        self.assertIn(
            "servicenow-developer",
            list(job.marketing_roles.values_list("slug", flat=True)),
        )


class ClassifyJobTaxonomyCommandTests(TestCase):
    def setUp(self):
        from companies.models import Company
        self.company = Company.objects.create(name="Taxonomy Co")

    def test_classify_job_taxonomy_backfills_category_and_domain(self):
        from harvest.models import RawJob

        raw = RawJob.objects.create(
            company=self.company,
            title="ServiceNow Developer",
            description="",
            is_active=True,
            job_category="",
            job_domain="",
            department_normalized="",
        )

        call_command("classify_job_taxonomy", reclassify_all=True)
        raw.refresh_from_db()

        self.assertEqual(raw.job_domain, "servicenow-developer")
        self.assertEqual(raw.job_category, "Engineering")
        self.assertTrue(raw.job_domain_candidates)


class JobDomainRoutingRuleAdminTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from users.models import User

        self.user = User.objects.create_user(
            username="routing_rule_admin",
            email="routing_rule_admin@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.company = Company.objects.create(name="Routing Rule Co")
        self.client.force_login(self.user)

    def test_saving_job_domain_creates_matching_marketing_role(self):
        from harvest.models import JobDomain
        from users.models import MarketingRole

        rule = JobDomain.objects.create(
            name="Routing Test Specialist",
            slug="routing-test-specialist",
            regex_pattern=r"\broutingtest\b|\brouting\s*specialist\b",
            top_category=JobDomain.TopCategory.NON_IT,
            priority=321,
        )

        role = MarketingRole.objects.get(slug=rule.slug)
        self.assertEqual(role.name, "Routing Test Specialist")
        self.assertEqual(role.top_category, JobDomain.TopCategory.NON_IT)
        self.assertTrue(role.is_active)
        self.assertIn("routingtest", role.match_keywords)
        self.assertIn("routing test specialist", role.match_keywords)

    def test_active_rule_reactivates_inactive_matching_marketing_role(self):
        from harvest.models import JobDomain
        from users.models import MarketingRole

        role = MarketingRole.objects.create(
            name="Dormant Routing Role",
            slug="dormant-routing-role",
            is_active=False,
        )
        JobDomain.objects.create(
            name="Dormant Routing Role",
            slug="dormant-routing-role",
            regex_pattern=r"\bdormantrouting\b",
            top_category=JobDomain.TopCategory.IT,
            priority=333,
        )

        role.refresh_from_db()
        self.assertTrue(role.is_active)
        self.assertEqual(role.top_category, JobDomain.TopCategory.IT)

    def test_impact_endpoint_reports_records_that_need_attention(self):
        import hashlib
        from harvest.models import RawJob
        from jobs.models import Job

        missing_url = "https://example.com/routing/missing"
        inactive_url = "https://example.com/routing/inactive-domain"
        synced_url = "https://example.com/routing/synced"
        raw_missing = RawJob.objects.create(
            company=self.company,
            title="Unknown Routing Job",
            url_hash=hashlib.sha256(missing_url.encode()).hexdigest(),
            original_url=missing_url,
            description="",
            job_domain="",
            is_active=True,
        )
        RawJob.objects.create(
            company=self.company,
            title="Old Domain Job",
            url_hash=hashlib.sha256(inactive_url.encode()).hexdigest(),
            original_url=inactive_url,
            description="",
            job_domain="no-longer-active-domain",
            is_active=True,
        )
        raw_synced = RawJob.objects.create(
            company=self.company,
            title="Synced Routing Job",
            url_hash=hashlib.sha256(synced_url.encode()).hexdigest(),
            original_url=synced_url,
            description="",
            job_domain="",
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
        )
        Job.objects.create(
            title=raw_missing.title,
            company=self.company.name,
            company_obj=self.company,
            description="Needs routing",
            original_link=missing_url,
            url_hash=raw_synced.url_hash,
            source_raw_job=raw_synced,
            posted_by=self.user,
            status=Job.Status.POOL,
        )

        response = self.client.get(reverse("harvest-job-domain-impact"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["raw_jobs_without_current_rule"], 2)
        self.assertGreaterEqual(payload["synced_raw_jobs_to_refresh"], 1)
        self.assertGreaterEqual(payload["open_or_pool_jobs_without_roles"], 1)
        self.assertTrue(payload["samples"])

    def test_role_routing_rules_page_renders(self):
        from harvest.models import JobDomain

        JobDomain.objects.create(
            name="Render Routing Rule",
            slug="render-routing-rule",
            regex_pattern=r"\brenderrouting\b",
            top_category=JobDomain.TopCategory.IT,
            priority=345,
        )

        response = self.client.get(reverse("harvest-job-domains"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Role Routing Rules")
        self.assertContains(response, "Render Routing Rule")
        self.assertContains(response, "Synced")

    @patch("jobs.tasks.classify_jobs_task.apply_async")
    def test_apply_existing_jobs_queues_force_reclassify_task(self, mock_apply):
        from django.core.cache import cache
        from jobs.tasks import CLASSIFY_ACTIVE_TASK_KEY, CLASSIFY_LOCK_KEY

        cache.delete(CLASSIFY_ACTIVE_TASK_KEY)
        cache.delete(CLASSIFY_LOCK_KEY)
        mock_apply.return_value = SimpleNamespace(id="routing-apply-task")

        response = self.client.post(reverse("harvest-job-domain-apply"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("harvest-job-domains"))
        mock_apply.assert_called_once_with(kwargs={"force_reclassify": True, "active_only": True})

    def test_pausing_domain_propagates_to_marketing_role(self):
        """Edge case B: pausing a domain must deactivate its MarketingRole so it
        stops being offered/auto-assigned — but keep the row (assignments survive)."""
        from harvest.models import JobDomain
        from users.models import MarketingRole

        rule = JobDomain.objects.create(
            name="Propagation Specialist",
            slug="propagation-specialist",
            regex_pattern=r"\bpropagation\b",
            top_category=JobDomain.TopCategory.NON_IT,
            priority=410,
        )
        self.assertTrue(MarketingRole.objects.get(slug=rule.slug).is_active)

        rule.is_active = False
        rule.save()
        self.assertFalse(MarketingRole.objects.get(slug=rule.slug).is_active)

        # Re-activating the domain re-activates the role.
        rule.is_active = True
        rule.save()
        self.assertTrue(MarketingRole.objects.get(slug=rule.slug).is_active)

    def test_quick_update_endpoint_toggles_and_reprioritizes(self):
        """Inline list controls flip active state and set priority via JSON."""
        from harvest.models import JobDomain
        from users.models import MarketingRole

        rule = JobDomain.objects.create(
            name="Quick Update Specialist",
            slug="quick-update-specialist",
            regex_pattern=r"\bquickupdate\b",
            top_category=JobDomain.TopCategory.IT,
            priority=500,
        )
        url = reverse("harvest-job-domain-quick", kwargs={"pk": rule.pk})

        # Toggle → paused, role follows
        resp = self.client.post(url, {"action": "toggle"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["is_active"])
        self.assertFalse(data["role_active"])
        rule.refresh_from_db()
        self.assertFalse(rule.is_active)
        self.assertFalse(MarketingRole.objects.get(slug=rule.slug).is_active)

        # Priority update
        resp = self.client.post(url, {"priority": "120"})
        self.assertEqual(resp.json()["priority"], 120)
        rule.refresh_from_db()
        self.assertEqual(rule.priority, 120)

        # Bad priority is rejected
        resp = self.client.post(url, {"priority": "abc"})
        self.assertEqual(resp.status_code, 400)

    def test_quick_add_autogenerates_slug_from_name(self):
        """Edge case: slug can be left blank — generated from the name."""
        from harvest.models import JobDomain

        response = self.client.post(
            reverse("harvest-job-domain-create"),
            {
                "name": "Cloud Security Architect",
                "slug": "",
                "regex_pattern": r"\bcloud\s*security\b",
                "top_category": JobDomain.TopCategory.IT,
                "priority": "500",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        rule = JobDomain.objects.get(name="Cloud Security Architect")
        self.assertEqual(rule.slug, "cloud-security-architect")


class JarvisCompanyFallbackTests(SimpleTestCase):
    def test_extract_company_from_dayforce_url_uses_tenant(self):
        from harvest.tasks import _extract_company_from_url

        url = "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE/jobs/6503?src=LinkedIn"
        self.assertEqual(_extract_company_from_url(url), "Kestra")

    def test_jarvis_company_jobs_url_dayforce_board_root(self):
        from harvest.tasks import _jarvis_company_jobs_url

        self.assertEqual(
            _jarvis_company_jobs_url("dayforce", "kestra|KESTRACAREERSITE"),
            "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE",
        )


class JarvisPlatformLabelRepairTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, JobBoardPlatform

        self.company = Company.objects.create(name="Appliedsystems")
        self.greenhouse, _ = JobBoardPlatform.objects.get_or_create(
            slug="greenhouse",
            defaults={"name": "Greenhouse", "is_enabled": True},
        )
        self.icims, _ = JobBoardPlatform.objects.get_or_create(
            slug="icims",
            defaults={"name": "iCIMS", "is_enabled": True},
        )
        self.label = CompanyPlatformLabel.objects.create(
            company=self.company,
            platform=self.greenhouse,
            tenant_id="appliedsystems",
            detection_method=CompanyPlatformLabel.DetectionMethod.URL_PATTERN,
            confidence=CompanyPlatformLabel.Confidence.MEDIUM,
        )

    def test_jarvis_can_repair_stale_platform_label_when_not_manual(self):
        from harvest.tasks import _jarvis_ensure_company_platform_label

        source_url = "https://careers-appliedsystems.icims.com/jobs/search"
        label, board_ctx = _jarvis_ensure_company_platform_label(
            company=self.company,
            detected_ats="icims",
            source_url=source_url,
            job_platform=self.icims,
        )
        label.refresh_from_db()
        self.assertIsNotNone(label)
        self.assertEqual(label.platform.slug, "icims")
        self.assertEqual(label.tenant_id, "careers-appliedsystems")
        self.assertEqual(board_ctx.get("platform_slug"), "icims")
        self.assertEqual(board_ctx.get("tenant_id"), "careers-appliedsystems")
        self.assertEqual(
            board_ctx.get("company_jobs_url"),
            "https://careers-appliedsystems.icims.com/jobs/search",
        )
        self.assertTrue(board_ctx.get("fetch_all_supported"))

    def test_manual_verified_label_is_not_overridden(self):
        from harvest.models import CompanyPlatformLabel
        from harvest.tasks import _jarvis_ensure_company_platform_label

        self.label.detection_method = CompanyPlatformLabel.DetectionMethod.MANUAL
        self.label.is_verified = True
        self.label.save(update_fields=["detection_method", "is_verified"])

        label, board_ctx = _jarvis_ensure_company_platform_label(
            company=self.company,
            detected_ats="icims",
            source_url="https://careers-appliedsystems.icims.com/jobs/search",
            job_platform=self.icims,
        )
        label.refresh_from_db()
        self.assertEqual(label.platform.slug, "greenhouse")
        self.assertEqual(board_ctx.get("platform_slug"), "greenhouse")
        self.assertEqual(board_ctx.get("tenant_id"), "appliedsystems")
        self.assertEqual(
            board_ctx.get("company_jobs_url"),
            "https://boards.greenhouse.io/appliedsystems",
        )

    def test_prefers_existing_label_for_detected_platform_when_multiple_labels_exist(self):
        from harvest.models import CompanyPlatformLabel
        from harvest.tasks import _jarvis_ensure_company_platform_label

        # CompanyPlatformLabel is OneToOne(company). Re-point the existing row to
        # iCIMS and verify Jarvis reuses it instead of creating anything new.
        self.label.platform = self.icims
        self.label.tenant_id = "careers-appliedsystems"
        self.label.detection_method = CompanyPlatformLabel.DetectionMethod.URL_PATTERN
        self.label.confidence = CompanyPlatformLabel.Confidence.HIGH
        self.label.save(
            update_fields=["platform", "tenant_id", "detection_method", "confidence"]
        )

        label, board_ctx = _jarvis_ensure_company_platform_label(
            company=self.company,
            detected_ats="icims",
            source_url="https://careers-appliedsystems.icims.com/jobs/search",
            job_platform=self.icims,
        )
        self.assertEqual(label.pk, self.label.pk)
        self.assertEqual(label.platform.slug, "icims")
        self.assertEqual(board_ctx.get("platform_slug"), "icims")


class JarvisCompanyAndRawJobDedupeTests(TestCase):
    def test_company_resolution_reuses_normalized_existing_company(self):
        from companies.models import Company
        from harvest.tasks import _jarvis_resolve_company

        existing = Company.objects.create(name="Applied Systems")
        resolved = _jarvis_resolve_company(
            "Appliedsystems",
            "https://careers-appliedsystems.icims.com/jobs/6419",
        )
        self.assertEqual(resolved.pk, existing.pk)

    def test_fetch_task_dedupes_same_external_id_with_different_urls(self):
        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, JobBoardPlatform, RawJob
        from harvest.tasks import fetch_raw_jobs_for_company_task

        company = Company.objects.create(name="Acme")
        platform, _ = JobBoardPlatform.objects.get_or_create(
            slug="greenhouse",
            defaults={"name": "Greenhouse", "is_enabled": True},
        )
        label = CompanyPlatformLabel.objects.create(
            company=company,
            platform=platform,
            tenant_id="acme",
            confidence=CompanyPlatformLabel.Confidence.HIGH,
            detection_method=CompanyPlatformLabel.DetectionMethod.URL_PATTERN,
        )

        class _FakeHarvester:
            last_total_available = 2

            def fetch_jobs(self, *args, **kwargs):
                return [
                    {
                        "original_url": "https://example.com/jobs/view?jobId=123",
                        "apply_url": "https://example.com/jobs/view?jobId=123",
                        "external_id": "job-123",
                        "title": "Platform Engineer",
                        "company_name": "Acme",
                    },
                    {
                        "original_url": "https://example.com/jobs/123",
                        "apply_url": "https://example.com/jobs/123",
                        "external_id": "job-123",
                        "title": "Platform Engineer",
                        "company_name": "Acme",
                    },
                ]

        with patch("harvest.harvesters.get_harvester", return_value=_FakeHarvester()):
            out = fetch_raw_jobs_for_company_task.apply(
                kwargs={"label_pk": label.pk, "fetch_all": True}
            ).get()

        self.assertEqual(RawJob.objects.filter(platform_label=label).count(), 1)
        self.assertEqual(out["jobs_found"], 2)
        self.assertEqual(out["jobs_new"], 1)
        self.assertEqual(out["jobs_updated"], 1)

    def test_fetch_task_dedupes_query_variant_without_external_id(self):
        import hashlib

        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, JobBoardPlatform, RawJob
        from harvest.tasks import fetch_raw_jobs_for_company_task

        company = Company.objects.create(name="Acme Query Variant")
        platform = JobBoardPlatform.objects.create(name="iCIMS Query", slug="icims-query-temp", is_enabled=True)
        label = CompanyPlatformLabel.objects.create(
            company=company,
            platform=platform,
            tenant_id="acme-query",
            confidence=CompanyPlatformLabel.Confidence.HIGH,
            detection_method=CompanyPlatformLabel.DetectionMethod.URL_PATTERN,
        )

        old_url = "https://example.com/jobs/6503?src=LinkedIn"
        old_hash = hashlib.sha256(old_url.encode("utf-8")).hexdigest()
        RawJob.objects.create(
            company=company,
            platform_label=label,
            job_platform=platform,
            title="Query Variant Role",
            original_url=old_url,
            apply_url=old_url,
            url_hash=old_hash,
            platform_slug="icims-query-temp",
            company_name=company.name,
        )

        class _FakeHarvester:
            last_total_available = 1

            def fetch_jobs(self, *args, **kwargs):
                return [
                    {
                        "original_url": "https://example.com/jobs/6503",
                        "apply_url": "https://example.com/jobs/6503",
                        "title": "Query Variant Role",
                        "company_name": company.name,
                    }
                ]

        with patch("harvest.harvesters.get_harvester", return_value=_FakeHarvester()):
            out = fetch_raw_jobs_for_company_task.apply(
                kwargs={"label_pk": label.pk, "fetch_all": True}
            ).get()

        self.assertEqual(RawJob.objects.filter(platform_label=label).count(), 1)
        self.assertEqual(out["jobs_found"], 1)
        self.assertEqual(out["jobs_new"], 0)
        self.assertEqual(out["jobs_updated"], 1)


class JarvisIngestDayforceIntegrationTests(TestCase):
    def test_ingest_auto_creates_dayforce_platform_and_company(self):
        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, JobBoardPlatform, RawJob
        from harvest.tasks import jarvis_ingest_task

        JobBoardPlatform.objects.filter(slug="dayforce").delete()
        Company.objects.filter(name="Kestra").delete()

        source_url = "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE/jobs/6503?src=LinkedIn"
        mock_ingest = {
            "error": "",
            "platform_slug": "dayforce",
            "strategy": "api:dayforce",
            "title": "Platform Engineer",
            "company_name": "",
            "description": "Lead cloud platform engineering initiatives across secure Azure workloads.",
            "original_url": source_url,
            "apply_url": source_url,
            "raw_payload": {"source": "test"},
        }

        with patch("harvest.jarvis.JobJarvis.ingest", return_value=mock_ingest):
            result = jarvis_ingest_task.apply(kwargs={"url": source_url, "user_id": None}).get()

        self.assertTrue(result.get("ok"))
        self.assertTrue(JobBoardPlatform.objects.filter(slug="dayforce").exists())
        company = Company.objects.get(name="Kestra")
        raw_job = RawJob.objects.get(pk=result["raw_job_id"])
        self.assertEqual(raw_job.company_id, company.pk)
        self.assertIsNotNone(raw_job.job_platform)
        self.assertEqual(raw_job.job_platform.slug, "dayforce")
        self.assertIsNotNone(raw_job.platform_label)
        self.assertEqual(raw_job.platform_label.tenant_id, "kestra|KESTRACAREERSITE")
        self.assertEqual(raw_job.platform_label.platform.slug, "dayforce")
        self.assertEqual((raw_job.raw_payload or {}).get("jarvis_tenant_id"), "kestra|KESTRACAREERSITE")
        self.assertEqual(
            (raw_job.raw_payload or {}).get("jarvis_company_jobs_url"),
            "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE",
        )
        self.assertTrue((raw_job.raw_payload or {}).get("jarvis_fetch_all_supported"))
        self.assertEqual((raw_job.raw_payload or {}).get("jarvis_detected_ats"), "dayforce")
        self.assertTrue(CompanyPlatformLabel.objects.filter(company=company, platform__slug="dayforce").exists())


class JarvisFetchAllCompanyViewTests(TestCase):
    def setUp(self):
        import hashlib

        from companies.models import Company
        from harvest.models import JobBoardPlatform, RawJob
        from users.models import User

        self.user = User.objects.create_user(
            username="jarvis_fetch_admin",
            email="jarvis_fetch_admin@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Kestra")
        self.platform, _ = JobBoardPlatform.objects.get_or_create(
            slug="dayforce",
            defaults={"name": "Dayforce", "is_enabled": True},
        )
        self.source_url = "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE/jobs/6503?src=LinkedIn"
        h = hashlib.sha256(self.source_url.encode()).hexdigest()
        self.raw_job = RawJob.objects.create(
            company=self.company,
            job_platform=self.platform,
            title="Platform Engineer",
            url_hash=h,
            original_url=self.source_url,
            apply_url=self.source_url,
            description="JD text long enough for task wiring.",
            platform_slug="jarvis",
            raw_payload={"jarvis_detected_ats": "dayforce"},
        )

    def test_fetch_all_endpoint_queues_company_task_and_updates_payload(self):
        from types import SimpleNamespace

        from harvest.models import CompanyPlatformLabel

        with patch("harvest.tasks.fetch_raw_jobs_for_company_task.apply_async", return_value=SimpleNamespace(id="task-123")) as mocked_apply:
            resp = self.client.post(
                reverse("harvest-jarvis-fetch-all"),
                {"raw_job_id": str(self.raw_job.pk)},
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("task_id"), "task-123")
        self.assertEqual(body.get("tenant_id"), "kestra|KESTRACAREERSITE")
        self.assertEqual(
            body.get("company_jobs_url"),
            "https://jobs.dayforcehcm.com/en-US/kestra/KESTRACAREERSITE",
        )
        self.assertTrue(body.get("progress_url"))
        parsed = urlparse(body["progress_url"])
        self.assertEqual(parsed.path, reverse("harvest-jarvis-fetch-all-progress"))
        qs = parse_qs(parsed.query)
        self.assertEqual(qs.get("task_id"), ["task-123"])
        self.assertEqual(qs.get("label_pk"), [str(body["label_pk"])])
        mocked_apply.assert_called_once()

        self.raw_job.refresh_from_db()
        self.assertIsNotNone(self.raw_job.platform_label)
        self.assertEqual(self.raw_job.platform_label.tenant_id, "kestra|KESTRACAREERSITE")
        self.assertTrue((self.raw_job.raw_payload or {}).get("jarvis_fetch_all_supported"))
        self.assertTrue(
            CompanyPlatformLabel.objects.filter(company=self.company, platform__slug="dayforce").exists()
        )

    def test_progress_api_returns_live_counts_and_recent_jobs(self):
        from django.utils import timezone

        from harvest.models import CompanyFetchRun, CompanyPlatformLabel

        label = CompanyPlatformLabel.objects.create(
            company=self.company,
            platform=self.platform,
            tenant_id="kestra|KESTRACAREERSITE",
            confidence=CompanyPlatformLabel.Confidence.HIGH,
        )
        self.raw_job.platform_label = label
        self.raw_job.save(update_fields=["platform_label", "updated_at"])

        run = CompanyFetchRun.objects.create(
            label=label,
            status=CompanyFetchRun.Status.RUNNING,
            task_id="task-live-1",
            started_at=timezone.now(),
            jobs_found=5,
            jobs_new=1,
            jobs_updated=1,
            jobs_duplicate=0,
            jobs_failed=0,
            triggered_by="JARVIS",
        )

        with patch("celery.result.AsyncResult") as mocked:
            mocked.return_value.state = "PROGRESS"
            mocked.return_value.info = {"percent": 44, "message": "Processing…"}
            resp = self.client.get(
                reverse("harvest-jarvis-fetch-all-progress-api"),
                {"task_id": run.task_id},
            )

        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("state"), "RUNNING")
        self.assertTrue(body.get("running"))
        self.assertFalse(body.get("done"))
        self.assertGreaterEqual(body.get("percent", 0), 1)
        self.assertEqual(body.get("counts", {}).get("found"), 5)
        self.assertEqual(body.get("counts", {}).get("new"), 1)
        self.assertTrue(body.get("rawjobs_url"))
        self.assertIn("recent_jobs", body)
        parsed = urlparse(body["rawjobs_url"])
        qs = parse_qs(parsed.query)
        self.assertEqual(qs.get("tab"), ["raw"])
        self.assertEqual(qs.get("platform"), ["dayforce"])
        self.assertEqual(qs.get("company_id"), [str(self.company.pk)])
        self.assertEqual(qs.get("label_pk"), [str(label.pk)])


class RawJobPipelineUnificationTests(TestCase):
    def setUp(self):
        import hashlib

        from companies.models import Company
        from harvest.models import RawJob
        from users.models import User

        self.user = User.objects.create_user(
            username="raw_unify_admin",
            email="raw_unify_admin@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="UnifyCo")

        def _mk(
            suffix: str,
            *,
            desc: str = "",
            sync_status: str = RawJob.SyncStatus.PENDING,
            quality_score=None,
            jd_quality_score=None,
            category_confidence=None,
            classification_confidence=None,
            is_active: bool = True,
            word_count: int = 0,
            job_domain: str = "",
            is_test_run: bool = False,
        ) -> RawJob:
            url = f"https://example.com/jobs/{suffix}"
            return RawJob.objects.create(
                company=self.company,
                company_name="UnifyCo",
                title=f"Role {suffix}",
                url_hash=hashlib.sha256(url.encode()).hexdigest(),
                original_url=url,
                description=desc,
                sync_status=sync_status,
                quality_score=quality_score,
                jd_quality_score=jd_quality_score,
                category_confidence=category_confidence,
                classification_confidence=classification_confidence,
                is_active=is_active,
                word_count=word_count,
                job_domain=job_domain,
                is_test_run=is_test_run,
            )

        _mk("fetched", desc="")
        _mk("parsed", desc="Parsed description text")
        _mk("enriched", desc="Enriched text", quality_score=0.71)
        _mk("classified", desc="Classified text", quality_score=0.81, category_confidence=0.24)
        _mk(
            "ready",
            desc="Ready text",
            quality_score=0.92,
            category_confidence=0.84,
            word_count=220,
            job_domain="devops-engineer",
        )
        _mk(
            "synced",
            desc="Synced text",
            sync_status=RawJob.SyncStatus.SYNCED,
            quality_score=0.95,
            category_confidence=0.90,
            word_count=260,
            job_domain="devops-engineer",
        )

    def test_funnel_counts_match_stage_filters(self):
        from harvest.models import RawJob
        from harvest.services.pipeline_snapshot import raw_jobs_workflow_insights
        from harvest.services.rawjob_query import apply_rawjob_filters

        insights = raw_jobs_workflow_insights(stale_pending_hours=6)
        funnel = insights["funnel"]
        stage_to_key = {
            "FETCHED": "fetched",
            "PARSED": "parsed",
            "ENRICHED": "enriched",
            "CLASSIFIED": "classified",
            "READY": "ready",
            "SYNCED": "synced",
        }
        for stage, key in stage_to_key.items():
            expected = apply_rawjob_filters(RawJob.objects.all(), {"stage": stage}).count()
            self.assertEqual(
                funnel[key],
                expected,
                msg=f"Funnel mismatch for stage={stage}: {funnel[key]} != {expected}",
            )

    def test_rawjobs_stage_page_count_matches_shared_filter(self):
        response = self.client.get(reverse("harvest-rawjobs"), {"stage": "CLASSIFIED"})
        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response["Location"])
        self.assertEqual(parsed.path, reverse("jobs-pipeline"))
        qs = parse_qs(parsed.query)
        self.assertEqual(qs.get("tab"), ["raw"])
        self.assertEqual(qs.get("stage"), ["CLASSIFIED"])

    def test_jobs_pipeline_uses_shared_raw_total_snapshot(self):
        from harvest.services.pipeline_snapshot import load_rawjobs_dashboard_stats

        response = self.client.get(reverse("jobs-pipeline"), {"tab": "raw"})
        self.assertEqual(response.status_code, 200)
        stats = load_rawjobs_dashboard_stats(force_refresh=False)
        self.assertEqual(response.context["raw_total"], stats["total"])

    def test_classification_bucket_low_filter(self):
        from harvest.models import RawJob
        from harvest.services.rawjob_query import apply_rawjob_filters

        low_qs = apply_rawjob_filters(
            RawJob.objects.all(),
            {"classification_bucket": "low"},
        )
        self.assertEqual(low_qs.count(), 1)
        self.assertEqual(low_qs.first().title, "Role classified")

    def test_jobs_pipeline_raw_gate_summary_counts(self):
        response = self.client.get(reverse("jobs-pipeline"), {"tab": "raw"})
        self.assertEqual(response.status_code, 200)
        summary = response.context["raw_gate_summary"]
        self.assertEqual(summary["pending_total"], 5)
        self.assertEqual(summary["qualified_pending"], 1)
        self.assertEqual(summary["qualified_synced"], 1)
        self.assertEqual(summary["blocked_missing_jd"], 1)
        self.assertEqual(summary["blocked_inactive"], 0)
        self.assertEqual(summary["blocked_low_conf"], 1)

    def test_jobs_pipeline_hides_test_rows_by_default(self):
        from harvest.models import RawJob

        RawJob.objects.create(
            company=self.company,
            company_name="UnifyCo",
            title="Smoke only",
            url_hash="smoke-hidden",
            original_url="https://example.com/jobs/smoke-hidden",
            is_test_run=True,
        )

        response = self.client.get(reverse("jobs-pipeline"), {"tab": "raw"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["raw_test_total"], 1)
        self.assertEqual(response.context["raw_total"], 6)
        ids = {row.id for row in response.context["tab_raw"]}
        self.assertFalse(RawJob.objects.get(url_hash="smoke-hidden").id in ids)

        response = self.client.get(reverse("jobs-pipeline"), {"tab": "raw", "include_test": "only"})
        self.assertEqual(response.status_code, 200)
        ids = {row.id for row in response.context["tab_raw"]}
        self.assertTrue(RawJob.objects.get(url_hash="smoke-hidden").id in ids)

    def test_jobs_pipeline_raw_stage_filter_uses_shared_filter(self):
        from harvest.models import RawJob
        from harvest.services.rawjob_query import apply_rawjob_filters

        response = self.client.get(reverse("jobs-pipeline"), {"tab": "raw", "stage": "CLASSIFIED"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["raw_selected_stage"], "CLASSIFIED")

        expected_ids = list(
            apply_rawjob_filters(RawJob.objects.all(), {"stage": "CLASSIFIED"})
            .order_by("-fetched_at")
            .values_list("id", flat=True)[:200]
        )
        actual_ids = [row.id for row in response.context["tab_raw"]]
        self.assertEqual(actual_ids, expected_ids)

        html = response.content.decode("utf-8")
        self.assertIn("?tab=raw&amp;stage=CLASSIFIED#raw-jobs-table", html)
        self.assertIn("?tab=raw&amp;sync_status=PENDING&amp;has_jd=0#raw-jobs-table", html)

    def test_jobs_pipeline_raw_json_includes_domain_and_category(self):
        from harvest.models import RawJob

        raw = RawJob.objects.order_by("-fetched_at").first()
        raw.job_category = "Engineering"
        raw.job_domain = "software-developer"
        raw.save(update_fields=["job_category", "job_domain"])

        response = self.client.get(
            reverse("jobs-pipeline"),
            {"tab": "raw", "raw_json": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        matching = next(item for item in payload["jobs"] if item["id"] == raw.id)
        self.assertEqual(matching["job_category"], "Engineering")
        self.assertEqual(matching["job_domain"], "software-developer")

    def test_jobs_pipeline_raw_json_prefers_approved_classification(self):
        from harvest.models import RawJob
        from jobs.models import RawJobClassificationSnapshot

        raw = RawJob.objects.order_by("-fetched_at").first()
        raw.job_category = ""
        raw.job_domain = ""
        raw.country = ""
        raw.country_code = ""
        raw.country_codes = []
        raw.save(update_fields=["job_category", "job_domain", "country", "country_code", "country_codes"])
        RawJobClassificationSnapshot.objects.create(
            raw_job=raw,
            status=RawJobClassificationSnapshot.Status.MERGED,
            approval_state=RawJobClassificationSnapshot.ApprovalState.APPROVED,
            approved_source="SECONDARY",
            final_confidence=0.88,
            approved_output={
                "classification": {
                    "job_category": "Engineering",
                    "job_domain": "platform-engineer",
                    "department_normalized": "Information Technology",
                },
                "location": {
                    "country": "Canada",
                    "country_codes": ["CA"],
                },
            },
        )

        response = self.client.get(
            reverse("jobs-pipeline"),
            {"tab": "raw", "raw_json": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        matching = next(item for item in payload["jobs"] if item["id"] == raw.id)
        self.assertEqual(matching["job_category"], "Engineering")
        self.assertEqual(matching["job_domain"], "platform-engineer")
        self.assertEqual(matching["classification_source"], "SECONDARY")
        self.assertIn("Canada", matching["country"])

    def test_jobs_pipeline_raw_page_prefers_approved_classification(self):
        from harvest.models import RawJob
        from jobs.models import RawJobClassificationSnapshot

        raw = RawJob.objects.order_by("-fetched_at").first()
        raw.job_category = ""
        raw.job_domain = ""
        raw.save(update_fields=["job_category", "job_domain"])
        RawJobClassificationSnapshot.objects.create(
            raw_job=raw,
            status=RawJobClassificationSnapshot.Status.MERGED,
            approval_state=RawJobClassificationSnapshot.ApprovalState.APPROVED,
            approved_source="CLAUDE",
            final_confidence=0.91,
            approved_output={
                "classification": {
                    "job_category": "Engineering",
                    "job_domain": "platform-engineer",
                },
            },
        )

        response = self.client.get(reverse("jobs-pipeline"), {"tab": "raw"})
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.context["tab_raw"] if item.pk == raw.pk)
        self.assertEqual(row.pipeline_row["job_category"], "Engineering")
        self.assertEqual(row.pipeline_row["job_domain"], "platform-engineer")
        self.assertEqual(row.pipeline_row["classification_source"], "CLAUDE")
        html = response.content.decode("utf-8")
        self.assertIn("platform-engineer", html)
        self.assertIn("claude", html.lower())

    def test_jobs_pipeline_raw_stage_links_preserve_current_raw_filters(self):
        response = self.client.get(
            reverse("jobs-pipeline"),
            {"tab": "raw", "sync_status": "PENDING", "is_active": "1", "stage": "PARSED"},
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("?tab=raw&amp;stage=CLASSIFIED#raw-jobs-table", html)
        self.assertIn("?tab=raw&amp;sync_status=PENDING&amp;has_jd=0#raw-jobs-table", html)

    def test_jobs_pipeline_supports_legacy_subtab_raw_links(self):
        response = self.client.get(
            reverse("jobs-pipeline"),
            {"_subtab": "jobs", "stage": "CLASSIFIED"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tab"], "raw")
        self.assertEqual(response.context["raw_selected_stage"], "CLASSIFIED")

    def test_jobs_pipeline_pool_gate_filter(self):
        from jobs.models import Job

        posted_by = self.user
        Job.objects.create(
            title="Pool eligible",
            company="UnifyCo",
            description="good",
            status=Job.Status.POOL,
            posted_by=posted_by,
            gate_status=Job.GateStatus.ELIGIBLE,
            vet_lane=Job.VetLane.AUTO,
        )
        Job.objects.create(
            title="Pool blocked",
            company="UnifyCo",
            description="bad",
            status=Job.Status.POOL,
            posted_by=posted_by,
            gate_status=Job.GateStatus.BLOCKED,
            vet_lane=Job.VetLane.BLOCKED,
        )
        response = self.client.get(reverse("jobs-pipeline"), {"tab": "pool", "gate": "BLOCKED"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["gate_tab"], "BLOCKED")
        rows = list(response.context["tab_jobs"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Pool blocked")


class HarvestPhase3NavigationTests(TestCase):
    def setUp(self):
        from users.models import User

        self.user = User.objects.create_user(
            username="phase3_nav_admin",
            email="phase3_nav_admin@example.com",
            password="testpass123",
            is_superuser=True,
        )
        self.client.force_login(self.user)

    @patch("harvest.tasks.sync_harvested_to_pool_task.delay")
    def test_run_sync_redirects_back_to_pipeline_raw_tab(self, mock_delay):
        mock_delay.return_value = MagicMock(id="task-1234-abcd")
        response = self.client.post(
            reverse("harvest-run-sync"),
            {
                "qualified_only": "1",
                "max_jobs": "0",
                "chunk_size": "500",
                "return_to": "jobs-pipeline",
                "return_tab": "raw",
            },
        )
        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response["Location"])
        self.assertEqual(parsed.path, reverse("jobs-pipeline"))
        qs = parse_qs(parsed.query)
        self.assertEqual(qs.get("tab"), ["raw"])
        self.assertEqual(qs.get("tp"), ["task-1234-abcd"])
        self.assertEqual(qs.get("tpl"), ["Sync Qualified to Vet Queue"])

    def test_rawjobs_batches_html_redirects_to_rawjobs_subtab(self):
        response = self.client.get(reverse("harvest-rawjobs-batches"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('jobs-pipeline')}?tab=raw")

    def test_rawjobs_company_status_html_redirects_to_rawjobs_subtab(self):
        response = self.client.get(reverse("harvest-rawjobs-company-status"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('jobs-pipeline')}?tab=raw")

    def test_rawjobs_batches_xhr_returns_json_payload(self):
        response = self.client.get(
            reverse("harvest-rawjobs-batches"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        payload = response.json()
        self.assertIn("batches", payload)
        self.assertIsInstance(payload["batches"], list)

    def test_rawjobs_company_status_xhr_returns_json_payload(self):
        response = self.client.get(
            reverse("harvest-rawjobs-company-status"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        payload = response.json()
        self.assertIn("runs", payload)
        self.assertIsInstance(payload["runs"], list)


class BoardAnalyticsServiceTests(TestCase):
    def test_board_analytics_exposes_expanded_rawjob_metrics(self):
        from companies.models import Company
        from harvest.board_analytics import get_board_analytics
        from harvest.models import JobBoardPlatform, RawJob

        company = Company.objects.create(name="Metrics Co")
        platform = JobBoardPlatform.objects.create(
            name="Metrics Platform",
            slug="metrics-platform",
            support_tier=JobBoardPlatform.SupportTier.HEALTHY,
            is_enabled=True,
        )

        RawJob.objects.create(
            company=company,
            job_platform=platform,
            platform_slug=platform.slug,
            url_hash="metrics-rich-1",
            title="Senior DevOps Engineer",
            normalized_title="devops engineer",
            company_name="Metrics Co",
            department="Infrastructure",
            department_normalized="DevOps / SRE",
            location_raw="Austin, TX",
            city="Austin",
            state="Texas",
            country="United States",
            postal_code="78701",
            employment_type=RawJob.EmploymentType.FULL_TIME,
            experience_level=RawJob.ExperienceLevel.SENIOR,
            salary_raw="$150k - $180k",
            description="Strong Kubernetes and Terraform background required.",
            requirements="Terraform, Kubernetes",
            responsibilities="Build CI/CD systems",
            benefits="Health, 401k",
            posted_date="2026-05-01",
            vendor_job_identification="REQ-100",
            vendor_job_category="Engineering",
            vendor_degree_level="BS",
            vendor_job_schedule="Full time",
            vendor_job_shift="Day",
            vendor_location_block="Austin, TX, United States",
            skills=["terraform", "kubernetes"],
            tech_stack=["terraform", "kubernetes"],
            job_category="IT",
            job_domain="devops-engineer",
            job_domain_candidates=["devops-engineer", "cloud-engineer"],
            domain_version="d2",
            years_required=5,
            education_required="BS",
            visa_sponsorship=False,
            work_authorization="US work authorization",
            clearance_level="Secret",
            travel_required="up to 10%",
            schedule_type="Full time",
            shift_schedule="Day",
            certifications=["AWS"],
            licenses_required=["Driver License"],
            languages_required=["English"],
            job_keywords=["devops", "kubernetes"],
            title_keywords=["devops", "engineer"],
            company_industry="Software",
            company_size="1000+",
            company_stage="Public",
            quality_score=0.92,
            jd_quality_score=0.88,
            classification_confidence=0.93,
            category_confidence=0.89,
            resume_ready_score=0.87,
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
        )
        RawJob.objects.create(
            company=company,
            job_platform=platform,
            platform_slug=platform.slug,
            url_hash="metrics-thin-2",
            title="General Analyst",
            company_name="Metrics Co",
            location_raw="Remote",
            sync_status=RawJob.SyncStatus.PENDING,
            is_active=True,
        )

        data = get_board_analytics(window_days=30)
        self.assertIn("rawjob_field_groups", data)
        self.assertIn("rawjob_score_group", data)
        self.assertIn("rawjob_blocker_group", data)

        row = next(p for p in data["platforms"] if p["slug"] == platform.slug)
        self.assertEqual(row["total_jobs"], 2)
        self.assertEqual(row["rawjob_metrics"]["job_domain"]["count"], 1)
        self.assertEqual(row["rawjob_metrics"]["job_category"]["count"], 1)
        self.assertEqual(row["rawjob_metrics"]["requirements"]["count"], 1)
        self.assertEqual(row["rawjob_metrics"]["current_enrichment_version"]["count"], 2)
        self.assertEqual(row["rawjob_metrics"]["current_domain_version"]["count"], 1)
        self.assertEqual(row["rawjob_metrics"]["parsed"]["count"], 1)
        self.assertEqual(row["rawjob_metrics"]["classified"]["count"], 1)
        self.assertEqual(row["rawjob_metrics"]["ready"]["count"], 1)
        self.assertEqual(row["rawjob_metrics"]["synced"]["count"], 1)
        self.assertEqual(row["score_metrics"]["quality_score"]["count"], 1)


class LocationResolverScopeTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from harvest.models import JobBoardPlatform

        self.company = Company.objects.create(name="Scope Co")
        self.platform = JobBoardPlatform.objects.create(name="Scope Platform", slug="scope-platform")

    def _raw(self, **kwargs):
        from harvest.models import RawJob

        defaults = {
            "company": self.company,
            "job_platform": self.platform,
            "platform_slug": self.platform.slug,
            "url_hash": kwargs.pop("url_hash", f"scope-{RawJob.objects.count()}"),
            "title": "Software Engineer",
            "company_name": "Scope Co",
            "sync_status": RawJob.SyncStatus.PENDING,
        }
        defaults.update(kwargs)
        return RawJob.objects.create(**defaults)

    def test_location_resolver_handles_ca_ambiguity(self):
        from harvest.location_resolver import resolve_location

        sf = resolve_location(location_raw="San Francisco, CA")
        toronto = resolve_location(location_raw="Toronto, CA")
        vancouver = resolve_location(location_raw="Vancouver, BC, CA")
        bangalore = resolve_location(location_raw="Bangalore, India")
        state_only = resolve_location(country="ON")

        self.assertEqual(sf.country_code, "US")
        self.assertEqual(sf.region_code, "CA")
        self.assertEqual(toronto.country_code, "CA")
        self.assertEqual(vancouver.country_code, "CA")
        self.assertEqual(bangalore.country_code, "IN")
        self.assertNotEqual(state_only.country_code, "ON")

    def test_scope_evaluator_marks_target_cold_and_unknown(self):
        from harvest.location_resolver import evaluate_rawjob_scope
        from harvest.models import HarvestEngineConfig, RawJob

        cfg = HarvestEngineConfig.get()
        cfg.target_countries = ["US", "IN", "GB", "AU"]
        cfg.process_unknown_country_with_target_domain = False
        cfg.save()

        us_job = self._raw(url_hash="scope-us", location_raw="Austin, TX")
        de_job = self._raw(url_hash="scope-de", location_raw="Berlin, Germany")
        unknown_job = self._raw(url_hash="scope-unknown", location_raw="")

        evaluate_rawjob_scope(us_job, cfg=cfg, save=True)
        evaluate_rawjob_scope(de_job, cfg=cfg, save=True)
        evaluate_rawjob_scope(unknown_job, cfg=cfg, save=True)

        us_job.refresh_from_db()
        de_job.refresh_from_db()
        unknown_job.refresh_from_db()

        self.assertEqual(us_job.country_code, "US")
        self.assertEqual(us_job.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertTrue(us_job.is_priority)
        self.assertEqual(de_job.country_code, "DE")
        self.assertEqual(de_job.scope_status, RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY)
        self.assertFalse(de_job.is_priority)
        self.assertEqual(unknown_job.scope_status, RawJob.ScopeStatus.COLD_NO_LOCATION)
        self.assertFalse(unknown_job.is_priority)

    def test_ambiguous_multi_location_is_review_and_cleans_fake_geo(self):
        from harvest.location_resolver import evaluate_rawjob_scope
        from harvest.models import HarvestEngineConfig, RawJob

        cfg = HarvestEngineConfig.get()
        cfg.process_unknown_country_with_target_domain = False
        cfg.save()

        raw = self._raw(
            url_hash="scope-hybrid-4-locations",
            title="Contracts Manager",
            location_raw="Hybrid Remote, 4 Locations",
            city="Hybrid Remote",
            state="4 Locations",
            country="Hybrid",
        )

        evaluate_rawjob_scope(raw, cfg=cfg, save=True)
        raw.refresh_from_db()

        self.assertEqual(raw.country_code, "")
        self.assertEqual(raw.country_source, "ambiguous_multi_location")
        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
        self.assertEqual(raw.scope_reason, "ambiguous_multi_location")
        self.assertFalse(raw.is_priority)
        self.assertEqual(raw.city, "")
        self.assertEqual(raw.state, "")
        self.assertEqual(raw.country, "")

    def test_unknown_remote_target_domain_stays_priority_review(self):
        from harvest.location_resolver import evaluate_rawjob_scope
        from harvest.models import HarvestEngineConfig, RawJob

        cfg = HarvestEngineConfig.get()
        cfg.process_unknown_country_with_target_domain = True
        cfg.save()

        raw = self._raw(
            url_hash="scope-remote-target-domain",
            title="Software Engineer",
            location_raw="Remote",
            city="Remote",
            country="Remote",
        )

        evaluate_rawjob_scope(raw, cfg=cfg, save=True)
        raw.refresh_from_db()

        self.assertEqual(raw.country_code, "")
        self.assertEqual(raw.country_source, "ambiguous_multi_location")
        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
        self.assertEqual(raw.scope_reason, "ambiguous_multi_location_target_domain")
        self.assertTrue(raw.is_priority)

    def test_provider_quota_counts_failed_attempts(self):
        from harvest.location_resolver import provider_requests_this_month, resolve_location
        from harvest.models import HarvestEngineConfig, LocationCache

        cfg = HarvestEngineConfig.get()
        cfg.geocoding_cache_enabled = True
        cfg.geocoding_provider_enabled = True
        cfg.geocoding_provider = "mapbox"
        cfg.geocoding_provider_token = "pk.test-token"
        cfg.geocoding_monthly_limit = 80000
        cfg.save()

        with patch("harvest.location_resolver.urllib.request.urlopen", side_effect=Exception("boom")):
            result = resolve_location(
                location_raw="zzzz-no-place-12345",
                cfg=cfg,
                use_provider=True,
            )

        self.assertEqual(result.status, LocationCache.Status.FAILED)
        self.assertEqual(provider_requests_this_month("mapbox"), 1)

    def test_multi_location_candidates_make_target_country_priority(self):
        from harvest.location_resolver import evaluate_rawjob_scope
        from harvest.models import HarvestEngineConfig, RawJob

        cfg = HarvestEngineConfig.get()
        cfg.target_countries = ["US", "IN", "CA", "GB", "AU"]
        cfg.process_unknown_country_with_target_domain = False
        cfg.save()

        raw = self._raw(
            url_hash="scope-multi-location-target",
            title="Practice Manager",
            location_raw="Hybrid Remote, 4 Locations",
            location_candidates=["Seattle, WA", "Toronto, ON"],
            city="Hybrid Remote",
            state="4 Locations",
            country="Hybrid",
        )

        evaluate_rawjob_scope(raw, cfg=cfg, save=True)
        raw.refresh_from_db()

        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertTrue(raw.is_priority)
        self.assertEqual(raw.country_code, "US")
        self.assertEqual(raw.country_source, "multi_location")
        self.assertIn("US", raw.country_codes)
        self.assertIn("CA", raw.country_codes)
        self.assertIn("Seattle, WA", raw.location_raw)

    def test_state_prefix_locations_resolve_as_us_or_canada(self):
        from harvest.location_resolver import extract_location_candidates, resolve_location

        us = resolve_location(location_raw="PA - Duquesne")
        ca = resolve_location(location_raw="ON - Toronto")
        candidates = extract_location_candidates(
            raw_payload={
                "Locations": [
                    {"LocalizedName": "PA - Duquesne"},
                    {"LocalizedName": "ON - Toronto"},
                ]
            }
        )

        self.assertEqual(us.country_code, "US")
        self.assertEqual(us.region_code, "PA")
        self.assertEqual(ca.country_code, "CA")
        self.assertEqual(ca.region_code, "ON")
        self.assertIn("PA - Duquesne", candidates)
        self.assertIn("ON - Toronto", candidates)

    def test_state_signal_overrides_stale_country_field(self):
        from harvest.location_resolver import evaluate_rawjob_scope, resolve_location
        from harvest.models import RawJob

        resolved = resolve_location(location_raw="Mountain View, CA", country="Canada")
        self.assertEqual(resolved.country_code, "US")
        self.assertEqual(resolved.region_code, "CA")

        raw = self._raw(
            url_hash="scope-stale-canada-country",
            title="Senior Fullstack Engineer",
            location_raw="Mountain View, CA",
            city="Mountain View",
            state="CA",
            country="Canada",
            country_code="CA",
        )
        evaluate_rawjob_scope(raw, save=True)
        raw.refresh_from_db()

        self.assertEqual(raw.country_code, "US")
        self.assertEqual(raw.country, "United States")
        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)

    def test_nested_payload_location_candidates_make_target_country_priority(self):
        from harvest.location_resolver import evaluate_rawjob_scope
        from harvest.models import HarvestEngineConfig, RawJob

        cfg = HarvestEngineConfig.get()
        cfg.target_countries = ["US", "IN", "CA", "GB", "AU"]
        cfg.process_unknown_country_with_target_domain = False
        cfg.save()

        raw = self._raw(
            url_hash="scope-nested-payload-locations",
            title="Practice Manager",
            location_raw="Hybrid Remote, 4 Locations",
            city="Hybrid Remote",
            state="4 Locations",
            country="Hybrid",
            raw_payload={
                "jobPostingInfo": {
                    "postingLocations": [
                        {"locationName": "Seattle, Washington"},
                        {"locationName": "Los Angeles, California"},
                    ],
                },
            },
        )

        evaluate_rawjob_scope(raw, cfg=cfg, save=True)
        raw.refresh_from_db()

        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertTrue(raw.is_priority)
        self.assertEqual(raw.country_code, "US")
        self.assertIn("Seattle, Washington", raw.location_candidates)
        self.assertIn("Los Angeles, California", raw.location_candidates)

    def test_location_resolver_does_not_infer_country_from_title(self):
        from harvest.location_resolver import resolve_location

        resolved = resolve_location(
            location_raw="",
            title="Investment Consultant - Rancho Bernardo, CA",
            description="Must be authorized to work in the United States.",
        )

        self.assertEqual(resolved.country_code, "")
        self.assertEqual(resolved.status, "UNKNOWN")

    def test_office_labeled_locations_resolve_without_becoming_country_names(self):
        from harvest.location_resolver import evaluate_rawjob_scope, resolve_location
        from harvest.models import RawJob

        denver = resolve_location(location_raw="Denver Office")
        boise = resolve_location(location_raw="Office - Boise")

        self.assertEqual(denver.country_code, "US")
        self.assertEqual(boise.country_code, "US")

        raw = self._raw(
            url_hash="scope-denver-office-country",
            title="Software Engineer",
            location_raw="Denver Office",
            country="Denver Office",
        )
        evaluate_rawjob_scope(raw, save=True)
        raw.refresh_from_db()

        self.assertEqual(raw.country_code, "US")
        self.assertEqual(raw.country, "United States")
        self.assertEqual(raw.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)

    def test_payload_location_scan_ignores_non_location_text_lists(self):
        from harvest.location_resolver import extract_location_candidates

        candidates = extract_location_candidates(
            raw_payload={
                "titleFragments": ["Client Relationship Specialist - Greenwich, CT"],
                "officeBlurb": ["where natural wonders are our playground"],
                "jobPostingInfo": {
                    "postingLocations": [{"locationName": "Indianapolis, IN"}, {"locationName": "Chicago, IL"}],
                },
            }
        )

        self.assertIn("Indianapolis, IN", candidates)
        self.assertIn("Chicago, IL", candidates)
        self.assertNotIn("Client Relationship Specialist - Greenwich, CT", candidates)
        self.assertNotIn("where natural wonders are our playground", candidates)

    def test_backfill_eligible_excludes_non_priority(self):
        """Slice 2 gate: JD backfill must skip non-priority jobs."""
        import hashlib
        from harvest.models import RawJob
        from harvest.tasks import _backfill_eligible_queryset

        url_p = "https://example.com/job/priority-1"
        url_c = "https://example.com/job/cold-1"
        priority = RawJob.objects.create(
            company=self.company, title="Eng", platform_slug=self.platform.slug,
            url_hash=hashlib.sha256(url_p.encode()).hexdigest(), original_url=url_p,
            description="", is_priority=True,
        )
        cold = RawJob.objects.create(
            company=self.company, title="Eng", platform_slug=self.platform.slug,
            url_hash=hashlib.sha256(url_c.encode()).hexdigest(), original_url=url_c,
            description="", is_priority=False,
        )
        eligible = _backfill_eligible_queryset(None)
        self.assertTrue(eligible.filter(pk=priority.pk).exists())
        self.assertFalse(eligible.filter(pk=cold.pk).exists())

    def test_pool_sync_excludes_non_priority(self):
        """Slice 2 gate: sync_harvested_to_pool_task must skip non-priority RawJobs."""
        import hashlib
        from harvest.models import RawJob
        from harvest.tasks import sync_harvested_to_pool_task
        from jobs.models import Job

        url = "https://example.com/job/cold-no-sync"
        h = hashlib.sha256(url.encode()).hexdigest()
        cold = RawJob.objects.create(
            company=self.company, title="Engineer", platform_slug=self.platform.slug,
            url_hash=h, original_url=url,
            description="A long description that would otherwise pass JD gates "
                        "for a cold non-priority job that should not sync.",
            sync_status="PENDING", is_priority=False, is_active=True,
        )
        sync_harvested_to_pool_task.apply(kwargs={"max_jobs": 10}).get()
        cold.refresh_from_db()
        # Non-priority must remain PENDING — sync_harvested_to_pool never picks it up.
        self.assertEqual(cold.sync_status, "PENDING")
        self.assertFalse(Job.objects.filter(url_hash=h).exists())


class HarvestEngineGuardrailTests(TestCase):
    def test_build_enrichment_input_keeps_vendor_and_location_hints(self):
        from harvest.services.enrichment_input import build_enrichment_input

        payload = {
            "vendor_job_shift": "Flexible",
            "vendor_job_identification": "R0123",
        }
        result = build_enrichment_input(
            {
                "title": "Data Engineer",
                "location_raw": "Indianapolis, IN | Chicago, IL",
                "country_codes": ["US"],
                "location_candidates": ["Indianapolis, IN", "Chicago, IL"],
                "raw_payload": payload,
            },
            overrides={"description": "Build data pipelines.", "vendor_job_schedule": "Full time"},
            company_name="Acme",
        )

        self.assertEqual(result["location_candidates"], ["Indianapolis, IN", "Chicago, IL"])
        self.assertEqual(result["country_codes"], ["US"])
        self.assertEqual(result["vendor_job_schedule"], "Full time")
        self.assertEqual(result["vendor_job_shift"], "Flexible")
        self.assertEqual(result["vendor_job_identification"], "R0123")
        self.assertEqual(result["company_name"], "Acme")

    def test_portal_health_requires_consecutive_failures_before_marking_down(self):
        from companies.models import Company
        from harvest.models import CompanyPlatformLabel, HarvestEngineConfig, JobBoardPlatform
        from harvest.tasks import check_portal_health_task

        cfg = HarvestEngineConfig.get()
        cfg.portal_health_failure_threshold = 2
        cfg.save()
        company = Company.objects.create(name="Portal Retry Co")
        platform, _ = JobBoardPlatform.objects.get_or_create(
            slug="greenhouse",
            defaults={"name": "Greenhouse Retry", "is_enabled": True},
        )
        label = CompanyPlatformLabel.objects.create(
            company=company,
            platform=platform,
            tenant_id="retryco",
            confidence=CompanyPlatformLabel.Confidence.HIGH,
            detection_method=CompanyPlatformLabel.DetectionMethod.URL_PATTERN,
        )

        head_response = MagicMock(status_code=500)
        get_response = MagicMock(status_code=500)
        get_response.close = MagicMock()
        with patch("requests.head", return_value=head_response), patch("requests.get", return_value=get_response):
            check_portal_health_task.apply(args=[label.pk]).get()
            label.refresh_from_db()
            self.assertIsNone(label.portal_alive)
            self.assertEqual(label.portal_consecutive_failures, 1)

            check_portal_health_task.apply(args=[label.pk]).get()
            label.refresh_from_db()
            self.assertFalse(label.portal_alive)
            self.assertEqual(label.portal_consecutive_failures, 2)

    def test_geocoding_quota_reports_hourly_warning(self):
        from django.utils import timezone
        from harvest.location_resolver import provider_quota_status
        from harvest.models import HarvestEngineConfig, LocationCache

        cfg = HarvestEngineConfig.get()
        cfg.geocoding_provider_enabled = True
        cfg.geocoding_provider = "mapbox"
        cfg.geocoding_monthly_limit = 10
        cfg.geocoding_hourly_limit = 2
        cfg.geocoding_warning_pct = 50
        cfg.save()

        cache = LocationCache.objects.create(
            raw_text="Boise, ID",
            normalized_text="boise id guardrail",
            source="provider",
            provider="mapbox",
            request_count=1,
        )
        LocationCache.objects.filter(pk=cache.pk).update(looked_up_at=timezone.now())

        status = provider_quota_status(cfg)
        self.assertEqual(status["monthly_used"], 1)
        self.assertEqual(status["hourly_used"], 1)
        self.assertTrue(status["hourly_warning"])
        self.assertTrue(status["available"])


class SelectiveHarvestRoleFilterTests(SimpleTestCase):
    def test_strips_seniority_and_matches_whole_phrase(self):
        from harvest.role_filter import STRONG, classify_title

        result = classify_title(
            title="Distinguished Staff Senior Principal DevOps Engineer III",
            categories=[
                {
                    "slug": "devops",
                    "name": "DevOps",
                    "include_phrases": ["devops engineer"],
                    "exclude_phrases": [],
                }
            ],
            hard_negatives=[],
            snapshot_id="snap",
        )

        self.assertEqual(result.decision, STRONG)
        self.assertEqual(result.category, "devops")

    def test_hard_negative_blocks_without_include_phrase(self):
        from harvest.role_filter import NO_MATCH, classify_title

        result = classify_title(
            title="Registered Nurse",
            categories=[],
            hard_negatives=["registered nurse"],
            snapshot_id="snap",
        )

        self.assertEqual(result.decision, NO_MATCH)
        self.assertEqual(result.matched_negative, "registered nurse")

    def test_department_floor_preserves_possible(self):
        from harvest.role_filter import POSSIBLE, classify_title

        result = classify_title(
            title="Analyst II",
            department="Engineering",
            categories=[],
            hard_negatives=[],
            snapshot_id="snap",
        )

        self.assertEqual(result.decision, POSSIBLE)

    def test_generic_title_in_non_tech_department_goes_cold(self):
        from harvest.role_filter import COLD, classify_title

        result = classify_title(
            title="Quality Engineer",
            department="Manufacturing / Warehouse Operations",
            categories=[],
            hard_negatives=[],
            snapshot_id="snap",
        )

        self.assertEqual(result.decision, COLD)


class HarvestLLMResolveTests(TestCase):
    """Safety: with the flag off (default), harvest LLM routing is unchanged."""

    def test_flag_off_uses_caller_defaults(self):
        from harvest.llm_classifier import _resolve_llm
        key, base_url, model, do_log = _resolve_llm("explicit-key", "gpt-4o-mini")
        self.assertEqual(key, "explicit-key")
        self.assertIsNone(base_url)              # OpenAI default endpoint
        self.assertEqual(model, "gpt-4o-mini")   # caller's model preserved
        self.assertFalse(do_log)                 # no logging when flag off

    def test_flag_on_uses_central_config(self):
        from core.models import FeatureFlag, LLMConfig
        FeatureFlag.objects.update_or_create(key="harvest_use_central_llm", defaults={"is_enabled": True})
        cfg = LLMConfig.load()
        cfg.provider, cfg.base_url, cfg.active_model = "deepseek", "", "deepseek-chat"
        cfg.validation_model = ""
        cfg.save()
        from harvest.llm_classifier import _resolve_llm
        key, base_url, model, do_log = _resolve_llm(None, "gpt-4o-mini")
        self.assertEqual(base_url, "https://api.deepseek.com")
        self.assertEqual(model, "deepseek-chat")  # central model overrides caller default
        self.assertTrue(do_log)


class JobLocationReconcileTests(TestCase):
    """Country drift propagation: RawJob country resolved post-sync → pool Job updated."""

    def _make_pair(self, raw_country, job_country):
        from companies.models import Company
        from harvest.models import RawJob
        from jobs.models import Job
        from users.models import User
        emp = User.objects.create_user(username=f"emp_{raw_country}_{job_country}",
                                       password="p", role=User.Role.EMPLOYEE)
        company = Company.objects.create(name=f"Co {raw_country}{job_country}")
        rj = RawJob.objects.create(
            company=company,
            title="T", company_name="C", country=raw_country,
            original_url=f"https://x.example/{raw_country}-{job_country}",
            url_hash=f"h-{raw_country}-{job_country}",
        )
        job = Job.objects.create(
            title="T", company="C", description="d",
            original_link=f"https://x.example/{raw_country}-{job_country}",
            posted_by=emp, country=job_country, source_raw_job=rj,
        )
        return rj, job

    def test_propagates_resolved_country(self):
        from harvest.tasks import _reconcile_synced_job_locations
        _rj, job = self._make_pair("United States", "")
        out = _reconcile_synced_job_locations()
        job.refresh_from_db()
        self.assertEqual(job.country, "United States")
        self.assertGreaterEqual(out["updated"], 1)

    def test_flag_off_skips(self):
        from core.models import FeatureFlag
        from harvest.tasks import _reconcile_synced_job_locations
        FeatureFlag.objects.update_or_create(key="job_location_sync", defaults={"is_enabled": False})
        _rj, job = self._make_pair("Canada", "")
        out = _reconcile_synced_job_locations()
        job.refresh_from_db()
        self.assertEqual(job.country, "")
        self.assertEqual(out, {"enabled": False, "updated": 0})


class WorkdayMultiLocationExtractionTests(SimpleTestCase):
    """The 'N Locations' placeholder must resolve to the real location list."""

    def test_jarvis_reads_additional_locations(self):
        from harvest.jarvis import _workday_location

        info = {
            "locationsText": "2 Locations",
            "location": "Madrid, Spain",
            "additionalLocations": ["London, United Kingdom", "Paris, France"],
        }
        result = _workday_location({"jobPostingInfo": info}, info)
        self.assertIn("Madrid, Spain", result)
        self.assertIn("London, United Kingdom", result)
        self.assertIn("Paris, France", result)
        self.assertNotIn("Locations", result)

    def test_jarvis_handles_dict_shaped_additional_locations(self):
        from harvest.jarvis import _workday_location

        info = {
            "locationsText": "3 Locations",
            "location": {"descriptor": "Austin, TX, USA"},
            "additionalLocations": [{"descriptor": "Denver, CO, USA"}],
        }
        result = _workday_location(info, info)
        self.assertIn("Austin, TX, USA", result)
        self.assertIn("Denver, CO, USA", result)

    def test_harvester_candidates_drop_count_placeholder(self):
        from harvest.harvesters.workday import _workday_location_candidates

        data = {"jobPostingInfo": {
            "locationsText": "2 Locations",
            "location": "Toronto, Canada",
            "additionalLocations": ["Vancouver, Canada"],
        }}
        candidates = _workday_location_candidates(data)
        joined = " | ".join(candidates).lower()
        self.assertIn("toronto", joined)
        self.assertIn("vancouver", joined)
        self.assertFalse(any("locations" in c.lower() and c.lower().split()[0].isdigit() for c in candidates))


class LocationLabelStripTests(SimpleTestCase):
    """Office/HQ/campus labels must be stripped so the city resolves."""

    def test_strips_hq_office_campus_suffixes(self):
        from harvest.location_resolver import _strip_location_label_noise

        cases = {
            "San Francisco-HQ": "San Francisco",
            "NYC Global HQ": "NYC",
            "Madrid Office": "Madrid",
            "Pune Research Campus": "Pune",
            "Austin Corporate HQ": "Austin",
        }
        for raw, expected in cases.items():
            self.assertEqual(_strip_location_label_noise(raw), expected)


class UnknownCountryReviewActionTests(TestCase):
    def setUp(self):
        from users.models import User
        self.user = User.objects.create_user(
            username="loc_review_admin", email="loc@example.com",
            password="testpass123", is_superuser=True,
        )
        from companies.models import Company
        self.company = Company.objects.create(name="Acme Loc Co")
        self.client.force_login(self.user)

    def _make_job(self, location_raw="2 Locations", platform="workday", title="Engineer", company_name="Acme"):
        from harvest.models import RawJob
        return RawJob.objects.create(
            company=self.company, company_name=company_name, platform_slug=platform, title=title,
            location_raw=location_raw, original_url=f"https://x/{location_raw}/{platform}",
            url_hash=f"h{RawJob.objects.count()}", scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY,
        )

    def test_assign_target_country_marks_priority_target(self):
        from harvest.models import RawJob
        job = self._make_job()
        resp = self.client.post(reverse("harvest-unknown-country-review"), {
            "action": "assign_country", "country_code": "US", "ids": [job.pk],
        })
        self.assertEqual(resp.status_code, 302)
        job.refresh_from_db()
        self.assertEqual(job.country_code, "US")
        self.assertEqual(job.country, "United States")
        self.assertEqual(job.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertTrue(job.is_priority)

    def test_assign_non_target_country_marks_cold(self):
        from harvest.models import RawJob
        job = self._make_job()
        self.client.post(reverse("harvest-unknown-country-review"), {
            "action": "assign_country", "country_code": "DE", "ids": [job.pk],
        })
        job.refresh_from_db()
        self.assertEqual(job.country_code, "DE")
        self.assertEqual(job.scope_status, RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY)
        self.assertFalse(job.is_priority)

    def test_bulk_by_location_string_actions_every_matching_row(self):
        from harvest.models import RawJob
        a = self._make_job(location_raw="Madrid Office")
        b = self._make_job(location_raw="Madrid Office")
        c = self._make_job(location_raw="Remote")
        # No ids — selected purely by the location string.
        self.client.post(reverse("harvest-unknown-country-review"), {
            "action": "mark_target", "bulk_location": "Madrid Office",
        })
        a.refresh_from_db(); b.refresh_from_db(); c.refresh_from_db()
        self.assertEqual(a.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertEqual(b.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)
        # The non-matching string is untouched.
        self.assertEqual(c.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)

    def test_queue_hides_delisted_jobs_by_default(self):
        live = self._make_job(location_raw="2 Locations")
        dead = self._make_job(location_raw="3 Locations")
        dead.is_active = False
        dead.save(update_fields=["is_active"])

        # Default view: delisted job hidden, banner reports it.
        resp = self.client.get(reverse("harvest-unknown-country-review"))
        self.assertEqual(resp.context["total"], 1)
        self.assertEqual(resp.context["hidden_inactive"], 1)
        ids = [j.pk for j in resp.context["page_obj"].object_list]
        self.assertIn(live.pk, ids)
        self.assertNotIn(dead.pk, ids)

        # include_inactive=1 shows everything.
        resp2 = self.client.get(reverse("harvest-unknown-country-review"), {"include_inactive": "1"})
        self.assertEqual(resp2.context["total"], 2)

    def test_unsafe_bulk_location_string_is_blocked(self):
        from harvest.models import RawJob
        a = self._make_job(location_raw="Remote")
        b = self._make_job(location_raw="Remote")

        resp = self.client.post(reverse("harvest-unknown-country-review"), {
            "action": "mark_target", "bulk_location": "Remote",
        }, follow=True)

        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
        self.assertEqual(b.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
        self.assertContains(resp, "Bulk action blocked")

    def test_country_only_bulk_can_assign_country_but_not_direct_target(self):
        from harvest.models import RawJob
        job = self._make_job(location_raw="United States")

        self.client.post(reverse("harvest-unknown-country-review"), {
            "action": "mark_target", "bulk_location": "United States",
        })
        job.refresh_from_db()
        self.assertEqual(job.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)

        self.client.post(reverse("harvest-unknown-country-review"), {
            "action": "assign_country", "bulk_location": "United States", "country_code": "US",
        })
        job.refresh_from_db()
        self.assertEqual(job.country_code, "US")
        self.assertEqual(job.scope_status, RawJob.ScopeStatus.PRIORITY_TARGET)

    def test_include_inactive_and_filters_are_preserved_in_links(self):
        live = self._make_job(location_raw="Madrid Office", platform="workday", title="Python Engineer")
        dead = self._make_job(location_raw="Remote", platform="greenhouse", title="Python Manager")
        dead.is_active = False
        dead.save(update_fields=["is_active"])

        resp = self.client.get(reverse("harvest-unknown-country-review"), {
            "include_inactive": "1",
            "platform": "workday",
            "q": "Python",
            "bucket": "clear",
        })

        self.assertEqual(resp.context["total"], 1)
        self.assertIn(live.pk, [j.pk for j in resp.context["page_obj"].object_list])
        self.assertContains(resp, "include_inactive=1")
        self.assertContains(resp, "q=Python")
        self.assertContains(resp, "bucket=clear")

    def test_search_filters_review_queue(self):
        match = self._make_job(location_raw="Berlin Office", title="Salesforce Developer")
        miss = self._make_job(location_raw="Madrid Office", title="SAP Consultant")

        resp = self.client.get(reverse("harvest-unknown-country-review"), {"q": "Salesforce"})

        ids = [j.pk for j in resp.context["page_obj"].object_list]
        self.assertEqual(resp.context["total"], 1)
        self.assertIn(match.pk, ids)
        self.assertNotIn(miss.pk, ids)

    def test_review_page_exposes_raw_and_live_job_links(self):
        job = self._make_job(location_raw="Remote", title="Cloud Engineer")

        resp = self.client.get(reverse("harvest-unknown-country-review"))

        self.assertContains(resp, "Review raw")
        self.assertContains(resp, "Live posting")
        self.assertContains(resp, reverse("harvest-rawjob-detail", args=[job.pk]))
        self.assertContains(resp, job.original_url)


class LocationLadderPhaseTests(TestCase):
    """Phases A–E of the location resolution ladder."""

    def test_phase_a_global_gazetteer_resolves_international_cities(self):
        from harvest.location_resolver import _city_country_code
        cases = {
            "Madrid Office": "ES", "Tijuana": "MX", "Krakow": "PL",
            "Tel Aviv": "IL", "San Francisco-HQ": "US", "NYC Global HQ": "US",
        }
        for raw, code in cases.items():
            self.assertEqual(_city_country_code(raw), code, raw)

    def test_phase_b_remote_with_country_clue_resolves(self):
        from harvest.location_resolver import _resolve_remote
        self.assertEqual(_resolve_remote("Remote - US", "", "", "", normalized="").country_code, "US")
        self.assertEqual(_resolve_remote("Remote", "", "", "", normalized="", title="Engineer (Remote, UK)").country_code, "GB")
        # Bare remote → no country
        self.assertIsNone(_resolve_remote("Remote", "", "", "", normalized=""))

    def test_phase_b_bare_remote_policy_target(self):
        from harvest.models import HarvestEngineConfig, RawJob
        from harvest.location_resolver import evaluate_rawjob_scope
        from companies.models import Company
        cfg = HarvestEngineConfig.get()
        cfg.remote_unknown_policy = "target"
        cfg.save(update_fields=["remote_unknown_policy"])
        company = Company.objects.create(name="Remote Co")
        job = RawJob.objects.create(
            company=company, company_name="Remote Co", platform_slug="greenhouse",
            title="Engineer", location_raw="Remote", original_url="https://x/remote1",
            url_hash="rh1",
        )
        updates = evaluate_rawjob_scope(job, cfg=cfg, save=False)
        self.assertEqual(updates["scope_status"], RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertEqual(updates["scope_reason"], "remote_no_country_policy_target")

    def test_phase_c_force_provider_enables_quota_without_global_flag(self):
        from harvest.models import HarvestEngineConfig
        from harvest.location_resolver import provider_quota_status
        cfg = HarvestEngineConfig.get()
        cfg.geocoding_provider_enabled = False
        cfg.geocoding_provider = "mapbox"
        cfg.save(update_fields=["geocoding_provider_enabled", "geocoding_provider"])
        # auto path: not enabled
        self.assertFalse(provider_quota_status(cfg)["enabled"])
        # forced (on-demand button/sweep): enabled, caps still computed
        self.assertTrue(provider_quota_status(cfg, force=True)["enabled"])

    def test_phase_d_sweep_clears_resolvable_backlog(self):
        from harvest.models import RawJob
        from companies.models import Company
        from django.core.management import call_command
        from io import StringIO
        company = Company.objects.create(name="Sweep Co")
        # Madrid resolves via the expanded gazetteer (was unknown before)
        good = RawJob.objects.create(
            company=company, company_name="Sweep Co", platform_slug="workday",
            title="Engineer", location_raw="Madrid", original_url="https://x/madrid",
            url_hash="sw1", scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY, is_active=True,
        )
        out = StringIO()
        call_command("sweep_unknown_country_locations", "--limit", "50", stdout=out)
        good.refresh_from_db()
        # Madrid → Spain (non-target by default) → no longer in review
        self.assertNotEqual(good.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
        self.assertEqual(good.country_code, "ES")

    def test_phase_e_failure_bucket_classifier(self):
        from harvest.views import _classify_location_bucket
        self.assertEqual(_classify_location_bucket("2 Locations"), "multi_placeholder")
        self.assertEqual(_classify_location_bucket("Remote"), "remote")
        self.assertEqual(_classify_location_bucket("Madrid Office"), "label_office")
        self.assertEqual(_classify_location_bucket("Madrid"), "city_unresolved")
        self.assertEqual(_classify_location_bucket(""), "no_location")

    def test_review_page_renders_with_failure_buckets(self):
        from users.models import User
        from companies.models import Company
        from harvest.models import RawJob
        admin = User.objects.create_user(username="lad_admin", email="lad@x.com", password="p", is_superuser=True)
        self.client.force_login(admin)
        company = Company.objects.create(name="Render Co")
        RawJob.objects.create(
            company=company, company_name="Render Co", platform_slug="workday",
            title="Eng", location_raw="2 Locations", original_url="https://x/r1",
            url_hash="rr1", scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY, is_active=True,
        )
        resp = self.client.get(reverse("harvest-unknown-country-review"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Why unresolved", resp.content.decode())


class RemoteLocationPolicyTests(TestCase):
    """Remote jobs: JD-LLM location scan → use it, else default-USA policy."""

    def _cfg(self, **kw):
        from harvest.models import HarvestEngineConfig
        cfg = HarvestEngineConfig.get()
        for k, v in kw.items():
            setattr(cfg, k, v)
        cfg.save()
        return cfg

    def _job(self, location_raw="Remote", title="Engineer", description="Work anywhere."):
        from harvest.models import RawJob
        from companies.models import Company
        company = Company.objects.create(name=f"RC{RawJob.objects.count()}")
        return RawJob.objects.create(
            company=company, company_name="RC", platform_slug="greenhouse",
            title=title, location_raw=location_raw, description=description,
            original_url=f"https://x/{RawJob.objects.count()}", url_hash=f"rp{RawJob.objects.count()}",
        )

    def test_us_policy_defaults_bare_remote_to_usa(self):
        from harvest.location_resolver import resolve_remote_scope
        cfg = self._cfg(remote_unknown_policy="us", remote_llm_jd_scan=False)
        upd = resolve_remote_scope(self._job(), cfg, {"US", "IN", "CA", "GB", "AU"})
        self.assertEqual(upd["country_code"], "US")
        self.assertEqual(upd["scope_status"], __import__("harvest.models", fromlist=["RawJob"]).RawJob.ScopeStatus.PRIORITY_TARGET)
        self.assertEqual(upd["scope_reason"], "remote_default_us")

    def test_review_policy_falls_through(self):
        from harvest.location_resolver import resolve_remote_scope
        cfg = self._cfg(remote_unknown_policy="review", remote_llm_jd_scan=False)
        self.assertIsNone(resolve_remote_scope(self._job(), cfg, {"US"}))

    def test_non_remote_returns_none(self):
        from harvest.location_resolver import resolve_remote_scope
        cfg = self._cfg(remote_unknown_policy="us")
        job = self._job(location_raw="Springfield", title="Engineer")
        self.assertIsNone(resolve_remote_scope(job, cfg, {"US"}))

    def test_llm_jd_scan_resolves_location(self):
        from unittest.mock import patch
        from harvest.location_resolver import resolve_remote_scope
        from harvest.models import RawJob
        cfg = self._cfg(remote_unknown_policy="us", remote_llm_jd_scan=True)
        job = self._job(description="You will join our Berlin hub.")
        with patch("harvest.llm_classifier.extract_remote_location", return_value="Berlin, Germany"):
            upd = resolve_remote_scope(job, cfg, {"US", "IN", "CA", "GB", "AU"})
        self.assertEqual(upd["country_code"], "DE")
        self.assertEqual(upd["scope_status"], RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY)
        self.assertEqual(upd["scope_reason"], "remote_jd_llm:DE")

    def test_llm_scan_empty_falls_back_to_policy(self):
        from unittest.mock import patch
        from harvest.location_resolver import resolve_remote_scope
        cfg = self._cfg(remote_unknown_policy="us", remote_llm_jd_scan=True)
        job = self._job()
        with patch("harvest.llm_classifier.extract_remote_location", return_value=""):
            upd = resolve_remote_scope(job, cfg, {"US"})
        self.assertEqual(upd["country_code"], "US")
        self.assertEqual(upd["scope_reason"], "remote_default_us")


class ReEvaluateActionFixTests(TestCase):
    """Re-evaluate must load full location fields (was half-blind) and report real outcomes."""

    def setUp(self):
        from users.models import User
        from companies.models import Company
        self.user = User.objects.create_user(
            username="reeval_admin", email="re@x.com", password="p", is_superuser=True)
        self.company = Company.objects.create(name="ReEval Co")
        self.client.force_login(self.user)

    def test_re_evaluate_resolves_named_city(self):
        from harvest.models import RawJob
        job = RawJob.objects.create(
            company=self.company, company_name="ReEval Co", platform_slug="workday",
            title="Engineer", location_raw="Madrid", description="JD body here.",
            original_url="https://x/madrid-re", url_hash="re1",
            scope_status=RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY, is_active=True,
        )
        resp = self.client.post(reverse("harvest-unknown-country-review"), {
            "action": "re_evaluate", "ids": [job.pk],
        })
        self.assertEqual(resp.status_code, 302)
        job.refresh_from_db()
        # Madrid → Spain (non-target) → Cold, left the review queue (was stuck before the fix).
        self.assertNotEqual(job.scope_status, RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY)
        self.assertEqual(job.country_code, "ES")


class VetGateConfigViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_superuser(
            "vet-admin@example.com",
            "vet-admin@example.com",
            "pw",
        )
        self.client.force_login(self.user)

    def test_vet_gate_page_shows_harvest_to_vet_controls(self):
        response = self.client.get(reverse("harvest-vet-gate-config"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Intake Filtering", content)
        self.assertIn("Location Scope", content)
        self.assertIn("LLM JD Content Gate", content)
        self.assertIn("Resume / Ready / Recheck Rules", content)
        self.assertIn("Pre-storage filter", content)
        self.assertIn("Remote/no-country policy", content)

    def test_vet_gate_post_updates_vet_and_engine_config(self):
        from harvest.models import HarvestEngineConfig, VetGateConfig

        response = self.client.post(
            reverse("harvest-vet-gate-config"),
            {
                "allow_unknown_country": "on",
                "allow_possible_filter": "on",
                "require_description": "on",
                "min_word_count": "120",
                "min_char_count": "700",
                "auto_lane_min_vet_priority": "0.81",
                "auto_lane_min_data_quality": "0.76",
                "auto_lane_min_trust": "0.74",
                "blocked_domains_json": '["sales", "finance-accounting"]',
                "default_chunk_size": "750",
                "auto_sync_after_harvest": "on",
                "auto_enrich": "on",
                "auto_backfill_jd": "on",
                "auto_sync_to_pool": "on",
                "selective_filter_enabled": "on",
                "pre_storage_filter_enabled": "on",
                "filter_full_crawl": "on",
                "title_hard_yes_confidence": "0.86",
                "zero_tech_threshold": "9",
                "zero_tech_skip_ttl_days": "45",
                "cold_no_match_sample_rate_pct": "7",
                "hard_negative_phrases": "registered nurse\nwarehouse worker\n",
                "target_countries_csv": "us, in, ca, us",
                "process_unknown_country_with_target_domain": "on",
                "rescope_on_target_country_change": "on",
                "remote_unknown_policy": "target",
                "remote_llm_jd_scan": "on",
                "geocoding_cache_enabled": "on",
                "geocoding_provider_enabled": "on",
                "geocoding_provider": "mapbox",
                "geocoding_monthly_limit": "90000",
                "geocoding_hourly_limit": "1500",
                "jd_gate_enabled": "on",
                "jd_gate_model": "gpt-4o-mini",
                "jd_gate_scope": "all_possible",
                "jd_gate_confidence_threshold": "0.72",
                "jd_gate_batch_size": "30",
                "jd_gate_snippet_chars": "1000",
                "resume_jd_min_words": "100",
                "resume_jd_min_chars": "650",
                "resume_jd_min_classification_confidence": "0.44",
                "ready_stage_min_confidence": "0.61",
                "backfill_jd_include_cold": "on",
                "validate_links_include_synced": "on",
                "validate_links_recent_hours": "240",
            },
        )

        self.assertEqual(response.status_code, 302)
        vet_cfg = VetGateConfig.get()
        engine_cfg = HarvestEngineConfig.get()

        self.assertTrue(vet_cfg.allow_unknown_country)
        self.assertEqual(vet_cfg.min_word_count, 120)
        self.assertEqual(vet_cfg.min_char_count, 700)
        self.assertEqual(vet_cfg.default_chunk_size, 750)
        self.assertEqual(vet_cfg.blocked_domains, ["sales", "finance-accounting"])
        self.assertAlmostEqual(vet_cfg.auto_lane_min_vet_priority, 0.81)

        self.assertTrue(engine_cfg.selective_filter_enabled)
        self.assertFalse(engine_cfg.filter_audit_mode)
        self.assertTrue(engine_cfg.pre_storage_filter_enabled)
        self.assertTrue(engine_cfg.filter_full_crawl)
        self.assertEqual(engine_cfg.target_countries, ["US", "IN", "CA"])
        self.assertEqual(engine_cfg.remote_unknown_policy, "target")
        self.assertTrue(engine_cfg.remote_llm_jd_scan)
        self.assertEqual(engine_cfg.geocoding_provider, "mapbox")
        self.assertTrue(engine_cfg.jd_gate_enabled)
        self.assertFalse(engine_cfg.jd_gate_audit_mode)
        self.assertEqual(engine_cfg.jd_gate_scope, "all_possible")
        self.assertEqual(engine_cfg.jd_gate_batch_size, 30)
        self.assertEqual(engine_cfg.jd_gate_snippet_chars, 1000)
        self.assertEqual(engine_cfg.hard_negative_phrases, ["registered nurse", "warehouse worker"])
        self.assertEqual(engine_cfg.resume_jd_min_words, 100)
        self.assertEqual(engine_cfg.resume_jd_min_chars, 650)
        self.assertAlmostEqual(engine_cfg.resume_jd_min_classification_confidence, 0.44)
        self.assertAlmostEqual(engine_cfg.ready_stage_min_confidence, 0.61)
        self.assertTrue(engine_cfg.backfill_jd_include_cold)
        self.assertTrue(engine_cfg.validate_links_include_synced)
        self.assertEqual(engine_cfg.validate_links_recent_hours, 240)


class RawJobManualRecheckPropagationTests(TestCase):
    def setUp(self):
        from companies.models import Company
        from django.contrib.auth import get_user_model
        from harvest.models import RawJob
        from jobs.models import Job

        self.user = get_user_model().objects.create_superuser(
            "raw-link-admin@example.com",
            "raw-link-admin@example.com",
            "pw",
        )
        self.client.force_login(self.user)
        self.company = Company.objects.create(name="Link Health Co")
        self.raw = RawJob.objects.create(
            company=self.company,
            company_name=self.company.name,
            title="Platform Engineer",
            original_url="https://example.com/jobs/platform-engineer",
            url_hash="raw-recheck-propagates",
            platform_slug="workday",
            sync_status=RawJob.SyncStatus.SYNCED,
            is_active=True,
        )
        self.job = Job.objects.create(
            title=self.raw.title,
            company=self.company.name,
            company_obj=self.company,
            description="JD",
            original_link=self.raw.original_url,
            posted_by=self.user,
            status=Job.Status.OPEN,
            source_raw_job=self.raw,
            url_hash=self.raw.url_hash,
            original_link_is_live=True,
            original_link_health=Job.LinkHealthState.LIVE,
        )

    @patch(
        "harvest.url_health.check_job_posting_live",
        return_value=SimpleNamespace(
            is_live=False,
            status_code=404,
            reason="http_404",
            final_url="https://example.com/jobs/platform-engineer",
        ),
    )
    def test_single_raw_recheck_updates_linked_job(self, _mock_check):
        response = self.client.post(reverse("harvest-rawjob-recheck-live", args=[self.raw.pk]))

        self.assertEqual(response.status_code, 302)
        self.raw.refresh_from_db()
        self.job.refresh_from_db()
        self.assertFalse(self.raw.is_active)
        self.assertFalse(self.job.original_link_is_live)
        self.assertEqual(self.job.original_link_health, "DEAD")
        self.assertEqual(self.job.original_link_reason, "http_404")

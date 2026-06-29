from django.test import TestCase, Client
from django.urls import reverse
from users.models import User, ConsultantProfile
from jobs.models import Job
from .models import ResumeDraft, LLMInputPreference, MasterPrompt
from .engine import (
    DEFAULT_INPUT_SECTIONS,
    build_candidate_input,
    merge_input_sections,
    validate_input_sections,
)


class ResumeDraftModelTests(TestCase):
    def setUp(self):
        self.employee = User.objects.create_user(
            username="emp1", password="testpass", role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username="con1", password="testpass", role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(
            user=self.consultant_user, bio="Test bio", skills=["Python"]
        )
        self.job = Job.objects.create(
            title="Dev", company="Co", posted_by=self.employee,
            description="Work", original_link="https://example.com/j",
        )

    def test_auto_increment_version(self):
        d1 = ResumeDraft.objects.create(
            consultant=self.profile, job=self.job, content="v1"
        )
        self.assertEqual(d1.version, 1)
        d2 = ResumeDraft.objects.create(
            consultant=self.profile, job=self.job, content="v2"
        )
        self.assertEqual(d2.version, 2)

    def test_skip_version_flag(self):
        d1 = ResumeDraft.objects.create(
            consultant=self.profile, job=self.job, content="v1"
        )
        d2 = ResumeDraft(consultant=self.profile, job=self.job, content="manual", version=99)
        d2.save(skip_version=True)
        self.assertEqual(d2.version, 99)

    def test_generation_id_unique(self):
        d1 = ResumeDraft.objects.create(consultant=self.profile, job=self.job, content="a")
        d2 = ResumeDraft.objects.create(consultant=self.profile, job=self.job, content="b")
        self.assertNotEqual(d1.generation_id, d2.generation_id)

    def test_str_representation(self):
        d = ResumeDraft.objects.create(
            consultant=self.profile, job=self.job, content="test"
        )
        self.assertIn("con1", str(d))
        self.assertIn("Dev", str(d))

    def test_find_reusable_draft_by_idempotency_key(self):
        from resumes.services import find_reusable_resume_draft

        draft = ResumeDraft.objects.create(
            consultant=self.profile,
            job=self.job,
            content="same draft",
            idempotency_key="same-key",
        )
        reused = find_reusable_resume_draft(
            consultant=self.profile,
            job=self.job,
            idempotency_key="same-key",
        )
        self.assertEqual(reused.pk, draft.pk)

    def test_resume_generation_source_state_blocks_stale_snapshot(self):
        from companies.models import Company
        from harvest.models import RawJob
        from jobs.models import RawJobClassificationSnapshot
        from resumes.services import resume_generation_source_state

        company = Company.objects.create(name="Snapshot Co")
        raw = RawJob.objects.create(
            company=company,
            company_name="Snapshot Co",
            title="Platform Engineer",
            description="Operate systems.",
            original_url="https://example.com/raw/platform",
        )
        self.job.source_raw_job = raw
        self.job.save(update_fields=["source_raw_job"])
        RawJobClassificationSnapshot.objects.create(
            raw_job=raw,
            approved_output={"classification": {"job_domain": "devops-cloud"}},
            approval_input_hash="abc123",
            approval_is_stale=True,
            ready_for_vetting=True,
        )

        state = resume_generation_source_state(self.job)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["reason"], "stale_approved_classification")

    def test_build_idempotency_key_is_stable_for_same_snapshot(self):
        from resumes.services import build_resume_draft_idempotency_key

        key_a = build_resume_draft_idempotency_key(
            consultant=self.profile,
            job=self.job,
            input_sections={"personal": True, "experience": True},
            coaching_keywords=["Python", "AWS"],
            generation_mode="manual",
            generation_reason="manual_generate",
            source_state={
                "snapshot_hash": "hash-1",
                "primary_role_slug": "devops-cloud",
                "prompt_version": "runtime_v1",
            },
        )
        key_b = build_resume_draft_idempotency_key(
            consultant=self.profile,
            job=self.job,
            input_sections={"personal": True, "experience": True},
            coaching_keywords=["AWS", "Python"],
            generation_mode="manual",
            generation_reason="manual_generate",
            source_state={
                "snapshot_hash": "hash-1",
                "primary_role_slug": "devops-cloud",
                "prompt_version": "runtime_v1",
            },
        )
        self.assertEqual(key_a, key_b)


class LLMInputPreferenceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass")

    def test_create_preference(self):
        pref = LLMInputPreference.objects.create(
            user=self.user, sections=["name", "email", "skills"]
        )
        self.assertEqual(pref.sections, ["name", "email", "skills"])

    def test_str(self):
        pref = LLMInputPreference.objects.create(user=self.user)
        self.assertIn("u1", str(pref))


class ResumeViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username="emp1", password="testpass", role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username="con1", password="testpass", role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(
            user=self.consultant_user, bio="Test", skills=["Python"]
        )
        self.job = Job.objects.create(
            title="Dev", company="Co", posted_by=self.employee,
            description="Work", original_link="https://example.com/j",
        )
        self.draft = ResumeDraft.objects.create(
            consultant=self.profile, job=self.job, content="# Resume",
            status=ResumeDraft.Status.DRAFT, ats_score=85,
        )

    def test_draft_detail_authenticated(self):
        self.client.login(username="emp1", password="testpass")
        url = reverse("draft-detail", args=[self.draft.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

    def test_draft_detail_unauthenticated(self):
        url = reverse("draft-detail", args=[self.draft.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)


class EngineInputSectionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="c1", password="pass", first_name="A", last_name="B", email="a@b.com"
        )
        self.profile = ConsultantProfile.objects.create(
            user=self.user, phone="555", skills=["Python"], base_resume_text="BASE"
        )

    def test_merge_defaults_without_master(self):
        m = merge_input_sections(None, None)
        self.assertEqual(m, DEFAULT_INPUT_SECTIONS)

    def test_merge_master_overrides(self):
        mp = MasterPrompt(
            name="t",
            system_prompt="x",
            default_input_sections={"experience": False, "base_resume": False},
        )
        m = merge_input_sections(mp, None)
        self.assertFalse(m["experience"])
        self.assertFalse(m["base_resume"])
        self.assertTrue(m["personal"])

    def test_build_candidate_input_omits_sections(self):
        text = build_candidate_input(
            self.profile,
            sections={
                "personal": True,
                "experience": False,
                "education": False,
                "certifications": False,
                "skills": False,
                "total_years": False,
                "base_resume": False,
            },
            master=None,
        )
        self.assertIn("PERSONAL DETAILS", text)
        self.assertNotIn("PROFESSIONAL EXPERIENCE", text)
        self.assertNotIn("BASE RESUME TEXT", text)
        self.assertNotIn("MASTER TECHNOLOGY POOL", text)

    def test_validate_requires_content_source(self):
        err = validate_input_sections(
            {
                "personal": True,
                "experience": False,
                "education": False,
                "certifications": True,
                "skills": False,
                "total_years": True,
                "base_resume": False,
            }
        )
        self.assertIsNotNone(err)


class GuardrailsTests(TestCase):
    """Deterministic truth-guardrails: fabrication blocks, clean passes."""

    HDR = ("Gopala\nNV\nPROFESSIONAL SUMMARY\nData engineer.\n"
           "PROFESSIONAL EXPERIENCE\n")

    def setUp(self):
        from datetime import date
        from users.models import Experience
        self.cu = User.objects.create_user(
            username="g_con", password="p", role=User.Role.CONSULTANT)
        self.profile = ConsultantProfile.objects.create(
            user=self.cu, bio="b", skills=["Python"])
        Experience.objects.create(
            consultant_profile=self.profile, title="Database Engineer",
            company="ExxonMobil", start_date=date(2025, 7, 1), is_current=True)

    def _run(self, content, jd_company="BrightHorizons"):
        from resumes.pipeline.guardrails import run_guardrails
        return run_guardrails(content, self.profile, {"company": jd_company})

    def test_clean_passes(self):
        r = self._run(self.HDR + "Database Engineer | ExxonMobil | Jul 2025 - Present\n- Built pipelines.")
        self.assertEqual(r["status"], "pass")

    def test_fabricated_employer_blocks(self):
        r = self._run(self.HDR + "Engineer | Google | 2025\n- x.")
        self.assertEqual(r["status"], "block")

    def test_jd_company_as_employer_blocks(self):
        r = self._run(self.HDR + "Engineer | BrightHorizons | 2025\n- x.")
        self.assertEqual(r["status"], "block")

    def test_fake_metric_flags_review(self):
        r = self._run(self.HDR + "Database Engineer | ExxonMobil | 2025\n- Boosted throughput by 42%.")
        self.assertEqual(r["status"], "review")


class LLMConfigProviderTests(TestCase):
    """Multi-provider base_url resolution."""

    def test_effective_base_url(self):
        from core.models import LLMConfig
        cfg = LLMConfig.load()
        cfg.provider, cfg.base_url = "openai", ""
        self.assertIsNone(cfg.effective_base_url())
        cfg.provider, cfg.base_url = "deepseek", ""
        self.assertEqual(cfg.effective_base_url(), "https://api.deepseek.com")
        cfg.provider, cfg.base_url = "custom", "https://x.y/v1"
        self.assertEqual(cfg.effective_base_url(), "https://x.y/v1")


# ── Resume Engine V4 P1a — JD Extraction Engine ──────────────────────────────
from unittest.mock import patch, MagicMock
import json as _json
from resumes.pipeline import jd_extractor_schemas as _S


def _valid_parsed_jd():
    return {
        "job_metadata": {"company_name": "Acme", "job_title": "Data Engineer"},
        "role_classification": {
            "primary_role_family": "data_engineer", "sub_role": None,
            "secondary_role_families": [], "role_blend_summary": "",
            "seniority": "mid", "resume_positioning_hint": "Data Engineer"},
        "requirements": {
            "screen_out_requirements": [],
            "must_have_skills": [
                {"raw_term": "Apache Spark", "normalized_term": "Spark", "category": "data",
                 "importance": "must_have", "source_section": "Minimum Qualifications",
                 "evidence_text": "Experience with Apache Spark required", "confidence": 0.95}],
            "nice_to_have_skills": [],
            "alternative_requirement_groups": [
                {"group_label": "language", "type": "one_of", "minimum_required": 1,
                 "options": [{"raw_term": "Python", "normalized_term": "Python", "category": "lang"},
                             {"raw_term": "Scala", "normalized_term": "Scala", "category": "lang"}],
                 "importance": "must_have", "evidence_text": "Python or Scala", "confidence": 0.9}]},
        "skill_categories": {"data_tools": [{"raw_term": "Airflow", "normalized_term": "Airflow"}]},
        "responsibility_themes": [{"theme": "build pipelines", "depth": "develop",
            "importance": "high", "evidence_text": "build batch pipelines", "confidence": 0.9}],
        "domain": {"primary_domain": "healthcare", "domain_keywords": ["claims"]},
        "ats_keywords": [], "soft_skills": [],
        "special_resume_requirements": [],
        "ignored_sections": [], "exact_phrase_controls": [], "hidden_priorities": [],
        "extraction_quality": {"overall_extraction_confidence": 0.9, "needs_human_review": False,
            "low_confidence_items": [], "extraction_warnings": []},
    }


class _FakeConfig:
    validation_model = "deepseek/deepseek-chat"
    max_output_tokens = 4000


class _FakeLLM:
    """Mock PipelineLLMClient — returns a queued list of (content) responses."""
    def __init__(self, *a, **k):
        self.config = _FakeConfig()
        self.default_model = "gpt-4o-mini"
        self.default_temperature = 0.1
    def is_available(self):
        return True, ""
    def check_token_cap(self):
        return True, ""


class JDExtractorValidationTests(TestCase):
    def test_valid_passes(self):
        v = _S.validate_parsed_jd(_valid_parsed_jd())
        self.assertTrue(v["ok"], v["errors"])

    def test_missing_role_family_fails(self):
        d = _valid_parsed_jd(); d["role_classification"]["primary_role_family"] = ""
        v = _S.validate_parsed_jd(d)
        self.assertFalse(v["ok"])
        self.assertTrue(any("VAL_002" in e for e in v["errors"]))

    def test_noise_misfiled_fails(self):
        d = _valid_parsed_jd()
        d["requirements"]["must_have_skills"].append(
            {"raw_term": "401k benefits", "normalized_term": "401k benefits",
             "importance": "must_have", "evidence_text": "x"})
        v = _S.validate_parsed_jd(d)
        self.assertFalse(v["ok"])
        self.assertTrue(any("VAL_006" in e for e in v["errors"]))

    def test_high_importance_needs_evidence(self):
        d = _valid_parsed_jd(); d["requirements"]["must_have_skills"][0]["evidence_text"] = ""
        v = _S.validate_parsed_jd(d)
        self.assertFalse(v["ok"])
        self.assertTrue(any("VAL_005" in e for e in v["errors"]))


class JDExtractorEngineTests(TestCase):
    def setUp(self):
        self.emp = User.objects.create_user(username="jdx_emp", password="p", role=User.Role.EMPLOYEE)
        self.job = Job.objects.create(
            title="Data Engineer", company="Acme", posted_by=self.emp,
            description="Build batch pipelines with Apache Spark. Python or Scala required.",
            original_link="https://x/de1")

    def _patch_llm(self, responses):
        fake = _FakeLLM()
        calls = {"n": 0}
        def _call(system, user, **kw):
            i = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return responses[i], 100, None
        fake.call = _call
        fake._calls = calls
        return fake

    def test_extracts_and_stores(self):
        from resumes.pipeline.jd_extractor import extract_jd
        fake = self._patch_llm([_json.dumps(_valid_parsed_jd())])
        with patch("resumes.pipeline.llm_client.PipelineLLMClient", return_value=fake):
            data = extract_jd(self.job)
        self.job.refresh_from_db()
        self.assertEqual(self.job.parsed_jd_status, _S.STATUS_OK_LLM)
        self.assertEqual(data["role_classification"]["primary_role_family"], "data_engineer")
        self.assertIn("parser_metadata", data)
        self.assertTrue(self.job.parsed_jd_hash)
        self.assertEqual(self.job.routing_role_family, "data_engineer")
        self.assertEqual(self.job.routing_seniority, "mid")
        self.assertEqual(self.job.routing_status, Job.RoutingStatus.READY)
        self.assertTrue(self.job.routing_hash)
        self.assertEqual(fake._calls["n"], 1)

    def test_routing_policy_can_force_review_when_country_missing(self):
        from core.models import PlatformConfig
        from resumes.pipeline.jd_extractor import extract_jd

        config = PlatformConfig.load()
        config.routing_require_country = True
        config.routing_ready_confidence_threshold = 0.55
        config.save()

        parsed = _valid_parsed_jd()
        parsed.setdefault("routing_profile", {})
        parsed["routing_profile"]["country_codes"] = []
        parsed["job_metadata"]["location"] = ""
        fake = self._patch_llm([_json.dumps(parsed)])
        with patch("resumes.pipeline.llm_client.PipelineLLMClient", return_value=fake):
            extract_jd(self.job)

        self.job.refresh_from_db()
        self.assertEqual(self.job.routing_status, Job.RoutingStatus.REVIEW)
        self.assertIn("country", self.job.routing_profile.get("missing_readiness_signals", []))

    def test_cache_hit_skips_llm(self):
        from resumes.pipeline.jd_extractor import extract_jd
        fake = self._patch_llm([_json.dumps(_valid_parsed_jd())])
        with patch("resumes.pipeline.llm_client.PipelineLLMClient", return_value=fake):
            extract_jd(self.job)
            extract_jd(self.job)  # second call — should hit cache
        self.assertEqual(fake._calls["n"], 1)  # LLM called once only

    def test_repair_retry_then_pass(self):
        from resumes.pipeline.jd_extractor import extract_jd
        bad = _valid_parsed_jd(); bad["role_classification"]["primary_role_family"] = ""
        good = _valid_parsed_jd()
        fake = self._patch_llm([_json.dumps(bad), _json.dumps(good)])
        with patch("resumes.pipeline.llm_client.PipelineLLMClient", return_value=fake):
            extract_jd(self.job)
        self.job.refresh_from_db()
        self.assertEqual(self.job.parsed_jd_status, _S.STATUS_OK_LLM)
        self.assertEqual(fake._calls["n"], 2)  # retried once

    def test_fallback_when_llm_unavailable(self):
        from resumes.pipeline.jd_extractor import extract_jd
        fake = _FakeLLM()
        fake.is_available = lambda: (False, "no key")
        with patch("resumes.pipeline.llm_client.PipelineLLMClient", return_value=fake):
            data = extract_jd(self.job)
        self.job.refresh_from_db()
        self.assertIn(self.job.parsed_jd_status, (_S.STATUS_RULES_FALLBACK, _S.STATUS_FAILED))
        self.assertIn("parser_metadata", data)


class JDParserDiffTests(TestCase):
    def test_diff_finds_new_skills(self):
        from resumes.pipeline.parser_diff import diff_parsers
        legacy = {"required_skills": ["python"]}
        d = diff_parsers(legacy, _valid_parsed_jd())
        self.assertIn("spark", d["new_skills_found_by_llm"])
        self.assertEqual(d["llm_primary_role_family"], "data_engineer")
        self.assertTrue(d["llm_has_alternative_groups"])


class JDExtractorPromptEditableTests(TestCase):
    def setUp(self):
        self.emp = User.objects.create_user(username="jdp_emp", password="p", role=User.Role.EMPLOYEE)
        self.job = Job.objects.create(
            title="Data Engineer", company="Acme", posted_by=self.emp,
            description="Build pipelines with Spark.", original_link="https://x/dp1")

    def _fake(self, capture):
        fake = _FakeLLM()
        def _call(system, user, **kw):
            capture["system"] = system
            return _json.dumps(_valid_parsed_jd()), 100, None
        fake.call = _call
        return fake

    def test_db_prompt_overrides_code_default(self):
        from resumes.models import JDExtractorPrompt
        from resumes.pipeline.jd_extractor import extract_jd
        JDExtractorPrompt.objects.create(name="custom v1", prompt_text="CUSTOM EXTRACTION PROMPT", is_active=True)
        cap = {}
        with patch("resumes.pipeline.llm_client.PipelineLLMClient", return_value=self._fake(cap)):
            extract_jd(self.job)
        self.assertEqual(cap["system"], "CUSTOM EXTRACTION PROMPT")  # DB prompt used, not the code constant
        self.job.refresh_from_db()
        self.assertTrue(self.job.parsed_jd_prompt_version.startswith("db-"))

    def test_editing_prompt_invalidates_cache(self):
        from resumes.models import JDExtractorPrompt
        from resumes.pipeline.jd_extractor import extract_jd
        p = JDExtractorPrompt.objects.create(name="v1", prompt_text="PROMPT A", is_active=True)
        cap = {"n": 0}
        fake = _FakeLLM()
        def _call(system, user, **kw):
            cap["n"] += 1
            return _json.dumps(_valid_parsed_jd()), 100, None
        fake.call = _call
        with patch("resumes.pipeline.llm_client.PipelineLLMClient", return_value=fake):
            extract_jd(self.job)              # call 1
            extract_jd(self.job)              # cached → no call
            self.assertEqual(cap["n"], 1)
            # edit the prompt → new version token → cache miss → re-parse
            import time as _t; _t.sleep(1.1)  # ensure updated_at timestamp advances
            p.prompt_text = "PROMPT B (edited)"; p.save()
            extract_jd(self.job)              # call 2
        self.assertEqual(cap["n"], 2)

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

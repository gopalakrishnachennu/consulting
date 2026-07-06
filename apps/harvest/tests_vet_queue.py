from django.contrib.auth import get_user_model
from django.test import TestCase

from companies.models import Company
from harvest.models import HarvestEngineConfig, RawJob, VetGateConfig
from harvest.vet_queue import (
    get_vet_queue_country_codes,
    job_vet_country_q,
    raw_job_matches_vet_queue_countries,
    raw_job_vet_country_q,
    vet_queue_job_queryset,
)
from jobs.models import Job


class VetQueueCountryTests(TestCase):
    def setUp(self):
        self.engine = HarvestEngineConfig.get()
        self.vet = VetGateConfig.get()
        self._orig_engine_targets = list(self.engine.target_countries or [])
        self._orig_vet_codes = list(self.vet.vet_queue_country_codes or [])
        self._orig_allow_unknown = self.vet.allow_unknown_country
        self.admin = get_user_model().objects.create_superuser("vetadmin", "vet@test.com", "pass")
        self.company = Company.objects.create(name="Acme")

    def tearDown(self):
        self.engine.target_countries = self._orig_engine_targets
        self.engine.save(update_fields=["target_countries", "updated_at"])
        self.vet.vet_queue_country_codes = self._orig_vet_codes
        self.vet.allow_unknown_country = self._orig_allow_unknown
        self.vet.save()

    def test_configured_codes_override_engine_targets(self):
        self.engine.target_countries = ["US", "IN", "CA"]
        self.engine.save(update_fields=["target_countries", "updated_at"])
        self.vet.vet_queue_country_codes = ["US"]
        self.vet.save(update_fields=["vet_queue_country_codes"])
        self.assertEqual(get_vet_queue_country_codes(), ["US"])

    def test_empty_vet_codes_inherit_engine_targets(self):
        self.engine.target_countries = ["US", "GB"]
        self.engine.save(update_fields=["target_countries", "updated_at"])
        self.vet.vet_queue_country_codes = []
        self.vet.save(update_fields=["vet_queue_country_codes"])
        self.assertEqual(get_vet_queue_country_codes(), ["US", "GB"])

    def test_raw_job_country_match(self):
        us_job = RawJob(title="US role", country_code="US")
        in_job = RawJob(title="IN role", country_code="IN")
        self.assertTrue(raw_job_matches_vet_queue_countries(us_job, ["US"]))
        self.assertFalse(raw_job_matches_vet_queue_countries(in_job, ["US"]))

    def test_pool_queryset_filters_non_us_jobs_when_explicitly_configured(self):
        self.engine.target_countries = ["US"]
        self.engine.save(update_fields=["target_countries", "updated_at"])
        self.vet.vet_queue_country_codes = ["US"]
        self.vet.allow_unknown_country = False
        self.vet.save(update_fields=["vet_queue_country_codes", "allow_unknown_country"])
        us_raw = RawJob.objects.create(
            company=self.company,
            title="US Analyst",
            company_name="Acme",
            original_url="https://example.com/us",
            country_code="US",
        )
        in_raw = RawJob.objects.create(
            company=self.company,
            title="IN Analyst",
            company_name="Acme",
            original_url="https://example.com/in",
            country_code="IN",
        )
        us_job = Job.objects.create(
            title="US Analyst",
            company="Acme",
            company_obj=self.company,
            description="US marketing role for vet queue test.",
            country="United States",
            source_raw_job=us_raw,
            status=Job.Status.POOL,
            posted_by=self.admin,
        )
        Job.objects.create(
            title="IN Analyst",
            company="Acme",
            company_obj=self.company,
            description="IN marketing role for vet queue test.",
            country="India",
            source_raw_job=in_raw,
            status=Job.Status.POOL,
            posted_by=self.admin,
        )
        visible = list(vet_queue_job_queryset().values_list("id", flat=True))
        self.assertEqual(visible, [us_job.id])

    def test_raw_job_vet_country_q_filters_queryset(self):
        RawJob.objects.create(
            company=self.company,
            title="US",
            company_name="Acme",
            original_url="https://example.com/1",
            country_code="US",
        )
        RawJob.objects.create(
            company=self.company,
            title="CA",
            company_name="Acme",
            original_url="https://example.com/2",
            country_code="CA",
        )
        qs = RawJob.objects.filter(raw_job_vet_country_q(["US"]))
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().country_code, "US")

    def test_job_vet_country_q_matches_country_name(self):
        raw = RawJob.objects.create(
            company=self.company,
            title="US role",
            company_name="Acme",
            original_url="https://example.com/us",
            country_code="US",
        )
        job = Job.objects.create(
            title="US role",
            company="Acme",
            company_obj=self.company,
            description="US role description.",
            country="United States",
            source_raw_job=raw,
            status=Job.Status.POOL,
            posted_by=self.admin,
        )
        qs = Job.objects.filter(status=Job.Status.POOL, is_archived=False).filter(job_vet_country_q(["US"]))
        self.assertEqual(qs.get().id, job.id)

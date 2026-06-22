from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from jobs.models import Job
from submissions.models import ApplicationSubmission
from .forms import ConsultantProfileEditForm
from .models import User, ConsultantLead, ConsultantProfile, EmployeeProfile, EmployerAccessRequest, Department
from .journey_utils import compute_consultant_readiness, at_risk_submissions_queryset


class EmployeeRouteTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_employee_routes_are_canonical_under_employees_prefix(self):
        self.assertEqual(reverse('employee-list'), '/employees/')
        self.assertEqual(reverse('employee-add'), '/employees/add/')
        self.assertEqual(reverse('employee-create'), '/employees/create/')
        self.assertEqual(reverse('employee-export-csv'), '/employees/export/')
        self.assertEqual(reverse('employee-detail', kwargs={'pk': 7}), '/employees/7/')
        self.assertEqual(reverse('employee-edit', kwargs={'pk': 7}), '/employees/7/edit/')
        self.assertEqual(reverse('employee-delete', kwargs={'pk': 7}), '/employees/7/delete/')

    def test_legacy_consultant_employee_urls_redirect_to_canonical_routes(self):
        legacy = {
            '/consultants/employees/': '/employees/',
            '/consultants/employees/export/': '/employees/export/',
            '/consultants/employees/create/': '/employees/add/',
            '/consultants/employees/7/': '/employees/7/',
            '/consultants/employees/7/edit/': '/employees/7/edit/',
            '/consultants/employees/7/delete/': '/employees/7/delete/',
        }
        for old_path, new_path in legacy.items():
            with self.subTest(old_path=old_path):
                match = resolve(old_path)
                response = match.func(self.factory.get(old_path), **match.kwargs)
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response['Location'], new_path)


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


class ConsultantJourneyTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(
            user=self.consultant_user,
            bio='x' * 50,
        )
        self.profile.onboarding_completed_at = timezone.now()
        self.profile.save(update_fields=['bio', 'onboarding_completed_at'])

    def test_journey_page_requires_consultant(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.get(reverse('consultant-journey'))
        self.assertEqual(resp.status_code, 403)

    def test_journey_page_loads_for_consultant(self):
        self.client.login(username='con1', password='testpass')
        resp = self.client.get(reverse('consultant-journey'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Readiness')

    def test_readiness_score_increases_with_submission(self):
        job = Job.objects.create(
            title='J',
            company='C',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/j',
        )
        my_sub = ApplicationSubmission.objects.filter(consultant=self.profile)
        base = compute_consultant_readiness(self.profile, my_sub)
        ApplicationSubmission.objects.create(
            job=job,
            consultant=self.profile,
            status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee,
        )
        my_sub = ApplicationSubmission.objects.filter(consultant=self.profile)
        after = compute_consultant_readiness(self.profile, my_sub)
        self.assertGreaterEqual(after, base)

    def test_at_risk_queryset_finds_dead_link_job(self):
        job = Job.objects.create(
            title='J',
            company='C',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='D',
            original_link='https://example.com/j',
            original_link_is_live=False,
            possibly_filled=False,
        )
        ApplicationSubmission.objects.create(
            job=job,
            consultant=self.profile,
            status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee,
        )
        qs = at_risk_submissions_queryset(self.profile)
        self.assertEqual(qs.count(), 1)


class ConsultantRoutingProfileFormTests(TestCase):
    def test_edit_form_persists_structured_routing_preferences(self):
        admin = User.objects.create_superuser("consultant_admin", "consultant-admin@example.com", "pass")
        consultant_user = User.objects.create_user(
            username="routing_profile_consultant",
            password="testpass",
            role=User.Role.CONSULTANT,
        )
        profile = ConsultantProfile.objects.create(user=consultant_user, bio="base")

        form = ConsultantProfileEditForm(
            data={
                "bio": "updated",
                "base_resume_text": "",
                "hourly_rate": "125",
                "phone": "",
                "preferred_location": "Austin, TX",
                "match_jd_title_override": "",
                "requires_visa_sponsorship": "true",
                "visa_status": "OPT",
                "clearance_eligible": "on",
                "skills_text": "Python, AWS",
                "work_countries_text": "United States, Canada",
                "preferred_seniority_text": "senior, lead",
                "citizenship_countries_text": "India",
                "work_authorization_countries_text": "United States",
                "employment_preferences_text": "w2, full_time",
                "preferred_work_modes_text": "remote, hybrid",
                "status": ConsultantProfile.Status.ACTIVE,
                "notice_period": "2 weeks",
            },
            instance=profile,
            user=admin,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()

        self.assertEqual(saved.work_countries, ["United States", "Canada"])
        self.assertEqual(saved.preferred_seniority_levels, ["senior", "lead"])
        self.assertEqual(saved.citizenship_countries, ["India"])
        self.assertEqual(saved.work_authorization_countries, ["United States"])
        self.assertEqual(saved.employment_preferences, ["w2", "full_time"])
        self.assertEqual(saved.preferred_work_modes, ["remote", "hybrid"])
        self.assertTrue(saved.requires_visa_sponsorship)
        self.assertEqual(saved.visa_status, "OPT")
        self.assertTrue(saved.clearance_eligible)


class PublicIntakeAndOnboardingTests(TestCase):
    def test_consultant_public_intake_creates_lead(self):
        response = self.client.post(
            reverse("consultant-join"),
            {
                "full_name": "Asha Patel",
                "email": "asha@example.com",
                "phone": "1234567890",
                "current_title": "Data Engineer",
                "location": "Chicago, IL",
                "linkedin_url": "https://linkedin.com/in/asha",
                "preferred_markets": "data, cloud",
                "notes": "Ready for contract work",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ConsultantLead.objects.count(), 1)
        self.assertEqual(ConsultantLead.objects.first().full_name, "Asha Patel")

    def test_employer_access_request_creates_record(self):
        response = self.client.post(
            reverse("employee-access-request"),
            {
                "company_name": "Northwind",
                "contact_name": "Hiring Lead",
                "work_email": "lead@northwind.example",
                "phone": "5555555555",
                "team_size": "25",
                "hiring_volume": "10 roles / month",
                "message": "Need employee accounts",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(EmployerAccessRequest.objects.count(), 1)

    def test_employee_onboarding_marks_profile_complete(self):
        employee = User.objects.create_user(
            username="emp_onboard",
            password="testpass",
            role=User.Role.EMPLOYEE,
        )
        EmployeeProfile.objects.create(user=employee)
        self.client.login(username="emp_onboard", password="testpass")

        response = self.client.post(
            reverse("employee-onboarding"),
            {
                "company_name": "CHENN",
                "phone": "5551112222",
                "work_location": "Dallas, TX",
            },
        )

        self.assertEqual(response.status_code, 302)
        employee.employee_profile.refresh_from_db()
        self.assertIsNotNone(employee.employee_profile.onboarding_completed_at)

    def test_consultant_onboarding_finishes_across_all_steps(self):
        consultant = User.objects.create_user(
            username="consultant_onboard",
            password="testpass",
            role=User.Role.CONSULTANT,
        )
        ConsultantProfile.objects.create(user=consultant)
        self.client.login(username="consultant_onboard", password="testpass")

        self.client.post(reverse("consultant-onboarding"), {
            "step": "1",
            "bio": "Platform engineer",
            "skills_text": "AWS, Terraform",
            "current_location": "Austin, TX",
        })
        self.client.post(reverse("consultant-onboarding"), {
            "step": "2",
            "preferred_location": "Remote",
            "available_from": "2026-06-22",
            "notice_period": "2 weeks",
        })
        self.client.post(reverse("consultant-onboarding"), {
            "step": "3",
            "work_countries_text": "United States",
            "preferred_seniority_text": "senior",
            "employment_preferences_text": "w2",
            "preferred_work_modes_text": "remote",
            "visa_status": "Citizen",
            "requires_visa_sponsorship": "false",
            "clearance_eligible": "on",
        })
        response = self.client.post(reverse("consultant-onboarding"), {"step": "4"})

        self.assertEqual(response.status_code, 302)
        consultant.consultant_profile.refresh_from_db()
        self.assertIsNotNone(consultant.consultant_profile.onboarding_completed_at)

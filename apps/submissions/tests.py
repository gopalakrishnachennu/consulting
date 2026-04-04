from datetime import date
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse
from users.models import User, ConsultantProfile, EmployeeProfile
from jobs.models import Job
from .models import ApplicationSubmission, Placement, Timesheet, Commission


class SubmissionExportCSVTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.consultant_user, bio='Test')
        self.job = Job.objects.create(
            title='Dev', company='Co', posted_by=self.employee, status=Job.Status.OPEN,
            description='Work', original_link='https://example.com/j'
        )
        self.sub = ApplicationSubmission.objects.create(
            job=self.job, consultant=self.profile, status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee
        )

    def test_export_csv_employee_returns_csv(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('submission-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get('Content-Type', '').startswith('text/csv'))
        self.assertIn(b'Dev', resp.content)

    def test_export_csv_consultant_sees_only_own(self):
        self.client.login(username='con1', password='testpass')
        url = reverse('submission-export-csv')
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Dev', resp.content)


class SubmissionBulkStatusTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.consultant_user, bio='Test')
        self.job = Job.objects.create(
            title='Dev', company='Co', posted_by=self.employee, status=Job.Status.OPEN,
            description='Work', original_link='https://example.com/j'
        )
        self.sub = ApplicationSubmission.objects.create(
            job=self.job, consultant=self.profile, status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee
        )

    def test_bulk_status_update_employee(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('submission-bulk-status')
        resp = self.client.post(url, {'submission_ids': [self.sub.pk], 'status': 'INTERVIEW'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ApplicationSubmission.Status.INTERVIEW)


# ─────────────────────────────────────────────────────────────
# Phase 1 Tests: Placement, Timesheet, Commission
# ─────────────────────────────────────────────────────────────

class _Phase1TestBase(TestCase):
    """Shared setUp for Phase 1 tests."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username='admin', password='admin123', email='admin@test.com'
        )
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        EmployeeProfile.objects.create(user=self.employee, company_name='Test Co')
        self.consultant_user = User.objects.create_user(
            username='con1', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.consultant_user, bio='Test')
        self.job = Job.objects.create(
            title='Dev', company='Co', posted_by=self.employee, status=Job.Status.OPEN,
            description='Work', original_link='https://example.com/j'
        )
        self.sub = ApplicationSubmission.objects.create(
            job=self.job, consultant=self.profile,
            status=ApplicationSubmission.Status.OFFER,
            submitted_by=self.employee,
        )


class PlacementModelTests(_Phase1TestBase):
    def test_placed_status_exists(self):
        """PLACED should be a valid status choice."""
        self.assertIn('PLACED', [c[0] for c in ApplicationSubmission.Status.choices])

    def test_create_placement(self):
        placement = Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.CONTRACT,
            start_date=date(2026, 4, 1),
            bill_rate=Decimal('150.00'),
            pay_rate=Decimal('100.00'),
            created_by=self.employee,
        )
        self.assertEqual(placement.spread, Decimal('50.00'))
        self.assertEqual(str(placement), f"Placement: {self.consultant_user.get_full_name()} at Co")

    def test_permanent_placement_revenue(self):
        placement = Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.PERMANENT,
            start_date=date(2026, 4, 1),
            annual_salary=Decimal('120000.00'),
            fee_percentage=Decimal('20.00'),
            created_by=self.employee,
        )
        self.assertEqual(placement.calculated_revenue, Decimal('24000.00'))

    def test_placement_one_to_one(self):
        """Only one placement per submission."""
        Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.CONTRACT,
            start_date=date(2026, 4, 1),
            created_by=self.employee,
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            Placement.objects.create(
                submission=self.sub,
                placement_type=Placement.PlacementType.CONTRACT,
                start_date=date(2026, 5, 1),
                created_by=self.employee,
            )


class TimesheetModelTests(_Phase1TestBase):
    def setUp(self):
        super().setUp()
        self.placement = Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.CONTRACT,
            start_date=date(2026, 4, 1),
            bill_rate=Decimal('150.00'),
            pay_rate=Decimal('100.00'),
            created_by=self.employee,
        )

    def test_create_timesheet(self):
        ts = Timesheet.objects.create(
            placement=self.placement,
            week_ending=date(2026, 4, 5),
            hours_worked=Decimal('40.00'),
        )
        self.assertEqual(ts.bill_amount, Decimal('6000.00'))
        self.assertEqual(ts.pay_amount, Decimal('4000.00'))
        self.assertEqual(ts.margin, Decimal('2000.00'))

    def test_timesheet_unique_per_week(self):
        Timesheet.objects.create(
            placement=self.placement,
            week_ending=date(2026, 4, 5),
            hours_worked=Decimal('40.00'),
        )
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            Timesheet.objects.create(
                placement=self.placement,
                week_ending=date(2026, 4, 5),
                hours_worked=Decimal('20.00'),
            )

    def test_contract_placement_revenue(self):
        """Contract placement revenue = spread * approved hours."""
        Timesheet.objects.create(
            placement=self.placement,
            week_ending=date(2026, 4, 5),
            hours_worked=Decimal('40.00'),
            status=Timesheet.TimesheetStatus.APPROVED,
        )
        Timesheet.objects.create(
            placement=self.placement,
            week_ending=date(2026, 4, 12),
            hours_worked=Decimal('40.00'),
            status=Timesheet.TimesheetStatus.APPROVED,
        )
        # Draft timesheets should NOT count
        Timesheet.objects.create(
            placement=self.placement,
            week_ending=date(2026, 4, 19),
            hours_worked=Decimal('40.00'),
            status=Timesheet.TimesheetStatus.DRAFT,
        )
        # Revenue = $50/hr spread * 80 approved hours = $4,000
        self.assertEqual(self.placement.calculated_revenue, Decimal('4000.00'))


class CommissionModelTests(_Phase1TestBase):
    def test_create_commission(self):
        placement = Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.PERMANENT,
            start_date=date(2026, 4, 1),
            fee_amount=Decimal('25000.00'),
            created_by=self.employee,
        )
        comm = Commission.objects.create(
            placement=placement,
            employee=self.employee,
            commission_rate=Decimal('10.00'),
            commission_amount=Decimal('2500.00'),
        )
        self.assertEqual(comm.status, Commission.CommissionStatus.PENDING)
        self.assertIn('$2500', str(comm))


class PlacementSignalTests(_Phase1TestBase):
    def test_submission_placed_sets_consultant_status(self):
        """When submission status = PLACED, consultant should be marked PLACED."""
        self.sub.status = ApplicationSubmission.Status.PLACED
        self.sub.save()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, ConsultantProfile.Status.PLACED)

    def test_placement_completed_reverts_consultant_status(self):
        """When placement is completed, consultant reverts to ACTIVE."""
        self.sub.status = ApplicationSubmission.Status.PLACED
        self.sub.save()
        placement = Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.CONTRACT,
            start_date=date(2026, 4, 1),
            created_by=self.employee,
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, ConsultantProfile.Status.PLACED)

        placement.status = Placement.PlacementStatus.COMPLETED
        placement.save()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, ConsultantProfile.Status.ACTIVE)


class PlacementViewTests(_Phase1TestBase):
    def test_placement_list_requires_staff(self):
        self.client.login(username='con1', password='testpass')
        resp = self.client.get(reverse('placement-list'))
        self.assertEqual(resp.status_code, 403)

    def test_placement_list_employee_access(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.get(reverse('placement-list'))
        self.assertEqual(resp.status_code, 200)

    def test_placement_create_flow(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('placement-create', kwargs={'submission_pk': self.sub.pk})
        resp = self.client.post(url, {
            'placement_type': 'CONTRACT',
            'status': 'ACTIVE',
            'start_date': '2026-04-01',
            'end_date': '2026-10-01',
            'bill_rate': '150.00',
            'pay_rate': '100.00',
            'currency': 'USD',
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Placement.objects.filter(submission=self.sub).exists())
        # Submission should be PLACED
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ApplicationSubmission.Status.PLACED)

    def test_placement_detail(self):
        Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.CONTRACT,
            start_date=date(2026, 4, 1),
            bill_rate=Decimal('150.00'),
            pay_rate=Decimal('100.00'),
            created_by=self.employee,
        )
        self.client.login(username='emp1', password='testpass')
        resp = self.client.get(reverse('placement-detail', kwargs={'pk': Placement.objects.first().pk}))
        self.assertEqual(resp.status_code, 200)


class TimesheetViewTests(_Phase1TestBase):
    def setUp(self):
        super().setUp()
        self.placement = Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.CONTRACT,
            start_date=date(2026, 4, 1),
            bill_rate=Decimal('150.00'),
            pay_rate=Decimal('100.00'),
            created_by=self.employee,
        )

    def test_timesheet_list(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.get(reverse('timesheet-list'))
        self.assertEqual(resp.status_code, 200)

    def test_create_timesheet(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('timesheet-create', kwargs={'placement_pk': self.placement.pk})
        resp = self.client.post(url, {
            'week_ending': '2026-04-05',
            'hours_worked': '40.00',
            'overtime_hours': '0.00',
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Timesheet.objects.count(), 1)

    def test_approve_timesheet(self):
        ts = Timesheet.objects.create(
            placement=self.placement,
            week_ending=date(2026, 4, 5),
            hours_worked=Decimal('40.00'),
            status=Timesheet.TimesheetStatus.SUBMITTED,
        )
        self.client.login(username='emp1', password='testpass')
        resp = self.client.post(
            reverse('timesheet-approve', kwargs={'pk': ts.pk}),
            {'action': 'approve'},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        ts.refresh_from_db()
        self.assertEqual(ts.status, Timesheet.TimesheetStatus.APPROVED)
        self.assertEqual(ts.approved_by, self.employee)


class CommissionViewTests(_Phase1TestBase):
    def setUp(self):
        super().setUp()
        self.placement = Placement.objects.create(
            submission=self.sub,
            placement_type=Placement.PlacementType.PERMANENT,
            start_date=date(2026, 4, 1),
            fee_amount=Decimal('25000.00'),
            created_by=self.employee,
        )

    def test_commission_list(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.get(reverse('commission-list'))
        self.assertEqual(resp.status_code, 200)

    def test_create_commission(self):
        self.client.login(username='emp1', password='testpass')
        url = reverse('commission-create', kwargs={'placement_pk': self.placement.pk})
        resp = self.client.post(url, {
            'employee': self.employee.pk,
            'commission_rate': '10.00',
            'commission_amount': '2500.00',
            'currency': 'USD',
            'status': 'PENDING',
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Commission.objects.count(), 1)

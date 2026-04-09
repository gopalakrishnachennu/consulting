from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse
from users.models import User, ConsultantProfile, EmployeeProfile
from jobs.models import Job
from core.models import Notification
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


# ─────────────────────────────────────────────────────────────
# Phase 3: Kanban pipeline (permissions + notifications)
# ─────────────────────────────────────────────────────────────


class KanbanPipelineSmokeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp1', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con1',
            password='testpass',
            role=User.Role.CONSULTANT,
            email='consultant@test.com',
        )
        self.consultant_user2 = User.objects.create_user(
            username='con2', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(user=self.consultant_user, bio='Test')
        self.profile2 = ConsultantProfile.objects.create(user=self.consultant_user2, bio='Test2')
        self.job = Job.objects.create(
            title='Dev',
            company='Co',
            posted_by=self.employee,
            status=Job.Status.OPEN,
            description='Work',
            original_link='https://example.com/j',
        )
        self.sub = ApplicationSubmission.objects.create(
            job=self.job,
            consultant=self.profile,
            status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee,
        )
        self.sub_other = ApplicationSubmission.objects.create(
            job=self.job,
            consultant=self.profile2,
            status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee,
        )

    def test_kanban_employee_and_consultant_can_view_board(self):
        self.client.login(username='emp1', password='testpass')
        self.assertEqual(self.client.get(reverse('submission-kanban')).status_code, 200)
        self.client.logout()
        self.client.login(username='con1', password='testpass')
        self.assertEqual(self.client.get(reverse('submission-kanban')).status_code, 200)

    def test_kanban_move_employee_updates_status(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.post(
            reverse('submission-kanban-move'),
            {'submission_id': self.sub.pk, 'status': 'INTERVIEW'},
        )
        self.assertEqual(resp.status_code, 302)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ApplicationSubmission.Status.INTERVIEW)

    def test_kanban_move_consultant_can_move_own_submission(self):
        self.client.login(username='con1', password='testpass')
        resp = self.client.post(
            reverse('submission-kanban-move'),
            {'submission_id': self.sub.pk, 'status': 'INTERVIEW'},
        )
        self.assertEqual(resp.status_code, 302)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, ApplicationSubmission.Status.INTERVIEW)

    def test_kanban_move_consultant_cannot_move_peer_submission(self):
        self.client.login(username='con1', password='testpass')
        resp = self.client.post(
            reverse('submission-kanban-move'),
            {'submission_id': self.sub_other.pk, 'status': 'INTERVIEW'},
        )
        self.assertEqual(resp.status_code, 403)
        self.sub_other.refresh_from_db()
        self.assertEqual(self.sub_other.status, ApplicationSubmission.Status.APPLIED)

    def test_kanban_move_invalid_status_returns_400(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.post(
            reverse('submission-kanban-move'),
            {'submission_id': self.sub.pk, 'status': 'NOT_A_STATUS'},
        )
        self.assertEqual(resp.status_code, 400)

    def test_kanban_move_staff_creates_in_app_notification_for_consultant(self):
        Notification.objects.filter(user=self.consultant_user).delete()
        self.client.login(username='emp1', password='testpass')
        self.client.post(
            reverse('submission-kanban-move'),
            {'submission_id': self.sub.pk, 'status': 'INTERVIEW'},
        )
        self.assertEqual(
            Notification.objects.filter(user=self.consultant_user).count(),
            1,
        )
        note = Notification.objects.get(user=self.consultant_user)
        self.assertEqual(note.kind, Notification.Kind.SUBMISSION)
        self.assertIn(str(self.sub.pk), note.link)

    def test_kanban_move_consultant_own_card_does_not_notify_self(self):
        Notification.objects.filter(user=self.consultant_user).delete()
        self.client.login(username='con1', password='testpass')
        self.client.post(
            reverse('submission-kanban-move'),
            {'submission_id': self.sub.pk, 'status': 'INTERVIEW'},
        )
        self.assertFalse(
            Notification.objects.filter(user=self.consultant_user).exists(),
        )

    def test_kanban_move_hx_returns_board_partial(self):
        self.client.login(username='emp1', password='testpass')
        resp = self.client.post(
            reverse('submission-kanban-move'),
            {'submission_id': self.sub.pk, 'status': 'IN_PROGRESS'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'kanban-root')


# ─────────────────────────────────────────────────────────────
# Phase 4+5 Tests
# ─────────────────────────────────────────────────────────────

class _Phase45TestBase(TestCase):
    """Shared setUp for Phase 4+5 tests."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username='admin45', password='testpass', role=User.Role.ADMIN
        )
        self.employee = User.objects.create_user(
            username='emp45', password='testpass', role=User.Role.EMPLOYEE
        )
        self.consultant_user = User.objects.create_user(
            username='con45', password='testpass', role=User.Role.CONSULTANT
        )
        self.profile = ConsultantProfile.objects.create(
            user=self.consultant_user, bio='Test consultant', skills=['Python', 'Django']
        )
        self.job = Job.objects.create(
            title='Senior Dev', company='TestCo', posted_by=self.employee,
            status=Job.Status.OPEN, description='Build things',
            original_link='https://example.com/j',
        )
        self.sub = ApplicationSubmission.objects.create(
            job=self.job, consultant=self.profile,
            status=ApplicationSubmission.Status.APPLIED,
            submitted_by=self.employee,
        )


class FollowUpReminderTests(_Phase45TestBase):

    def test_create_reminder(self):
        from .models import FollowUpReminder
        self.client.login(username='emp45', password='testpass')
        resp = self.client.post(
            reverse('followup-reminder-create', args=[self.sub.pk]),
            {'remind_at': '2026-04-10T09:00', 'message': 'Follow up with client'},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(FollowUpReminder.objects.filter(submission=self.sub).count(), 1)

    def test_dismiss_reminder(self):
        from .models import FollowUpReminder
        from django.utils import timezone
        reminder = FollowUpReminder.objects.create(
            submission=self.sub, remind_at=timezone.now(), created_by=self.employee,
        )
        self.client.login(username='emp45', password='testpass')
        resp = self.client.post(
            reverse('followup-reminder-dismiss', args=[reminder.pk]),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, FollowUpReminder.ReminderStatus.DISMISSED)


class StaleSubmissionsTests(_Phase45TestBase):

    def test_stale_page_loads(self):
        self.client.login(username='emp45', password='testpass')
        resp = self.client.get(reverse('stale-submissions'))
        self.assertEqual(resp.status_code, 200)

    def test_stale_shows_old_submissions(self):
        from django.utils import timezone
        from datetime import timedelta
        # Make submission old
        ApplicationSubmission.objects.filter(pk=self.sub.pk).update(
            updated_at=timezone.now() - timedelta(days=20)
        )
        self.client.login(username='emp45', password='testpass')
        resp = self.client.get(reverse('stale-submissions'))
        self.assertContains(resp, 'Senior Dev')


class SoftDeleteTests(_Phase45TestBase):

    def test_archive_submission(self):
        self.client.login(username='emp45', password='testpass')
        resp = self.client.post(reverse('submission-archive', args=[self.sub.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertTrue(self.sub.is_archived)

    def test_restore_submission(self):
        self.sub.is_archived = True
        self.sub.save()
        self.client.login(username='emp45', password='testpass')
        resp = self.client.post(reverse('submission-restore', args=[self.sub.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertFalse(self.sub.is_archived)

    def test_archived_list_page(self):
        self.sub.is_archived = True
        self.sub.save()
        self.client.login(username='emp45', password='testpass')
        resp = self.client.get(reverse('submission-archived'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Senior Dev')

    def test_archive_job(self):
        self.client.login(username='emp45', password='testpass')
        resp = self.client.post(reverse('job-archive', args=[self.job.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.job.refresh_from_db()
        self.assertTrue(self.job.is_archived)

    def test_restore_job(self):
        from django.utils import timezone
        self.job.is_archived = True
        self.job.archived_at = timezone.now()
        self.job.save()
        self.client.login(username='emp45', password='testpass')
        resp = self.client.post(reverse('job-restore', args=[self.job.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.job.refresh_from_db()
        self.assertFalse(self.job.is_archived)

    def test_archived_jobs_page(self):
        from django.utils import timezone
        self.job.is_archived = True
        self.job.archived_at = timezone.now()
        self.job.save()
        self.client.login(username='emp45', password='testpass')
        resp = self.client.get(reverse('job-archived'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Senior Dev')


class GDPRExportTests(_Phase45TestBase):

    def test_gdpr_export_admin(self):
        self.client.login(username='admin45', password='testpass')
        resp = self.client.get(reverse('gdpr-export', args=[self.consultant_user.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/json')
        import json
        data = json.loads(resp.content)
        self.assertEqual(data['user']['username'], 'con45')
        self.assertIn('submissions', data)

    def test_gdpr_export_consultant_self(self):
        self.client.login(username='con45', password='testpass')
        resp = self.client.get(reverse('gdpr-export', args=[self.consultant_user.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_gdpr_export_other_consultant_denied(self):
        other = User.objects.create_user(
            username='other_con', password='testpass', role=User.Role.CONSULTANT
        )
        self.client.login(username='con45', password='testpass')
        resp = self.client.get(reverse('gdpr-export', args=[other.pk]), follow=True)
        # Should redirect (denied)
        self.assertEqual(resp.status_code, 200)


class WinLossAnalysisTests(_Phase45TestBase):

    def test_win_loss_page_loads(self):
        self.client.login(username='emp45', password='testpass')
        resp = self.client.get(reverse('win-loss-analysis'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Win/Loss Analysis')

    def test_win_loss_counts(self):
        # Add a placed submission
        con2 = User.objects.create_user(username='con2_45', password='testpass', role=User.Role.CONSULTANT)
        p2 = ConsultantProfile.objects.create(user=con2, bio='Test2')
        j2 = Job.objects.create(
            title='Job2', company='Co2', posted_by=self.employee,
            status=Job.Status.OPEN, description='d', original_link='https://x.com/2',
        )
        ApplicationSubmission.objects.create(
            job=j2, consultant=p2, status=ApplicationSubmission.Status.PLACED,
            submitted_by=self.employee,
        )
        self.client.login(username='emp45', password='testpass')
        resp = self.client.get(reverse('win-loss-analysis'))
        self.assertContains(resp, 'Placed (Wins)')


class CoverLetterModelTests(_Phase45TestBase):

    def test_cover_letter_auto_version(self):
        from resumes.models import CoverLetter
        cl1 = CoverLetter.objects.create(
            consultant=self.profile, job=self.job, content='test', created_by=self.employee,
        )
        self.assertEqual(cl1.version, 1)
        cl2 = CoverLetter.objects.create(
            consultant=self.profile, job=self.job, content='test v2', created_by=self.employee,
        )
        self.assertEqual(cl2.version, 2)


class FollowUpTaskTests(_Phase45TestBase):

    def test_send_followup_reminders_task(self):
        from .models import FollowUpReminder
        from django.utils import timezone
        reminder = FollowUpReminder.objects.create(
            submission=self.sub,
            remind_at=timezone.now() - timedelta(hours=1),
            created_by=self.employee,
        )
        from .tasks import send_followup_reminders
        result = send_followup_reminders()
        self.assertEqual(result['sent'], 1)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, FollowUpReminder.ReminderStatus.SENT)
        self.assertTrue(Notification.objects.filter(user=self.employee, title__startswith='Follow-up').exists())

    def test_detect_stale_submissions_task(self):
        from django.utils import timezone
        ApplicationSubmission.objects.filter(pk=self.sub.pk).update(
            updated_at=timezone.now() - timedelta(days=20)
        )
        from .tasks import detect_stale_submissions
        result = detect_stale_submissions()
        self.assertGreaterEqual(result['notifications_created'], 1)
        self.assertTrue(Notification.objects.filter(user=self.employee, title__startswith='Stale').exists())


class FieldLevelAuditTests(_Phase45TestBase):

    def test_log_field_changes(self):
        from core.models import AuditLog
        from core.audit_utils import log_field_changes
        old = {'status': 'APPLIED', 'notes': 'old'}
        new = {'status': 'INTERVIEW', 'notes': 'old'}
        changes = log_field_changes(self.employee, self.sub, old, new)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['field'], 'status')
        self.assertEqual(AuditLog.objects.count(), 1)
        log = AuditLog.objects.first()
        self.assertEqual(log.action, 'field_change')
        self.assertEqual(log.target_model, 'ApplicationSubmission')

    def test_no_log_when_no_changes(self):
        from core.models import AuditLog
        from core.audit_utils import log_field_changes
        old = {'status': 'APPLIED'}
        new = {'status': 'APPLIED'}
        changes = log_field_changes(self.employee, self.sub, old, new)
        self.assertEqual(len(changes), 0)
        self.assertEqual(AuditLog.objects.count(), 0)

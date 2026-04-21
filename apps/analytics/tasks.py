from celery import shared_task
from django.utils import timezone
from django.db.models import Avg, Sum, Count


@shared_task
def take_daily_snapshot_task():
    """
    Capture a point-in-time snapshot of platform health.
    Idempotent — updates today's row if it already exists.
    Run daily via Celery beat (add to seed_periodic_tasks).
    """
    from jobs.models import Job
    from submissions.models import ApplicationSubmission, Placement
    from users.models import ConsultantProfile
    from .models import DailySnapshot

    today = timezone.localdate()
    AS = ApplicationSubmission

    jobs_harvested = Job.objects.count()
    jobs_pool = Job.objects.filter(status=Job.Status.POOL, is_archived=False).count()
    jobs_live = Job.objects.filter(status=Job.Status.OPEN, is_archived=False).count()
    jobs_closed_today = Job.objects.filter(
        status=Job.Status.CLOSED,
        updated_at__date=today,
    ).count()

    subs_total = AS.objects.filter(is_archived=False).count()
    subs_applied = AS.objects.filter(status=AS.Status.APPLIED).count()
    subs_interview = AS.objects.filter(status=AS.Status.INTERVIEW).count()
    subs_offer = AS.objects.filter(status=AS.Status.OFFER).count()
    subs_placed = AS.objects.filter(status=AS.Status.PLACED).count()
    subs_rejected = AS.objects.filter(status=AS.Status.REJECTED).count()

    # Active placements & revenue
    active_placements = Placement.objects.filter(end_date__gte=today).count()
    placement_rates = Placement.objects.filter(end_date__gte=today).aggregate(
        avg_bill=Avg('bill_rate'),
        avg_pay=Avg('pay_rate'),
    )
    avg_bill = float(placement_rates['avg_bill'] or 0)
    avg_pay = float(placement_rates['avg_pay'] or 0)
    avg_margin_pct = round(((avg_bill - avg_pay) / avg_bill * 100) if avg_bill else 0, 2)

    # MTD revenue from RevenueRecord
    from .models import RevenueRecord
    mtd_start = today.replace(day=1)
    revenue_mtd_agg = RevenueRecord.objects.filter(
        period_start__gte=mtd_start
    ).aggregate(total=Sum('gross_revenue'))
    revenue_mtd = float(revenue_mtd_agg['total'] or 0)

    consultants_active = ConsultantProfile.objects.filter(status='ACTIVE').count()
    consultants_bench = ConsultantProfile.objects.filter(status='BENCH').count()
    consultants_placed = ConsultantProfile.objects.filter(status='PLACED').count()

    DailySnapshot.objects.update_or_create(
        date=today,
        defaults=dict(
            jobs_harvested_total=jobs_harvested,
            jobs_in_pool=jobs_pool,
            jobs_live=jobs_live,
            jobs_closed_today=jobs_closed_today,
            submissions_total=subs_total,
            submissions_applied=subs_applied,
            submissions_interview=subs_interview,
            submissions_offer=subs_offer,
            submissions_placed=subs_placed,
            submissions_rejected=subs_rejected,
            active_placements=active_placements,
            revenue_mtd=revenue_mtd,
            avg_bill_rate=avg_bill,
            avg_pay_rate=avg_pay,
            avg_margin_pct=avg_margin_pct,
            consultants_active=consultants_active,
            consultants_bench=consultants_bench,
            consultants_placed=consultants_placed,
        ),
    )
    return {"date": str(today), "jobs_live": jobs_live, "active_placements": active_placements}

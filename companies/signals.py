from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="jobs.Job")
def auto_create_company_from_job(sender, instance, **kwargs):
    """
    When a Job is saved with a company name but no company_obj link,
    find-or-create the Company record and queue free enrichment.
    """
    # Skip if already linked
    if instance.company_obj_id:
        return

    raw_name = (getattr(instance, "company", None) or "").strip()
    if not raw_name:
        return

    from difflib import SequenceMatcher
    from .models import Company
    from .services import normalize_company_name
    from .tasks import enrich_company_task

    name = normalize_company_name(raw_name)
    name_lower = name.lower()

    # 1. Exact normalised name match (case-insensitive)
    company = Company.objects.filter(name__iexact=name).first()

    # 2. Alias match
    if not company:
        company = Company.objects.filter(alias__iexact=raw_name).first()

    # 3. Fuzzy match — catch typos like "brighthorizon" → "BrightHorizons"
    if not company:
        best, best_ratio = None, 0.0
        for c in Company.objects.only("pk", "name", "alias"):
            ratio = SequenceMatcher(None, name_lower, c.name.lower()).ratio()
            if c.alias:
                ratio = max(ratio, SequenceMatcher(None, name_lower, c.alias.lower()).ratio())
            if ratio > best_ratio:
                best_ratio = ratio
                best = c
        if best_ratio >= 0.82:
            company = best

    if not company:
        company = Company.objects.create(name=name)
        enrich_company_task.delay(company.pk)

    # Link without triggering the signal again (use queryset update)
    sender.objects.filter(pk=instance.pk).update(company_obj=company)

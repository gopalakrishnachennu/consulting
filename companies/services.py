from __future__ import annotations

import re
from typing import List, Tuple

from django.db import transaction

from .models import Company, CompanyDoNotSubmit
from jobs.models import Job


LEGAL_SUFFIXES = (
    "inc",
    "inc.",
    "llc",
    "llc.",
    "ltd",
    "ltd.",
    "corp",
    "corp.",
    "co",
    "co.",
    "gmbh",
    "s.a.",
    "s.a",
)


def normalize_company_name(raw: str) -> str:
    """
    Normalize a raw company name:
    - strip legal suffixes (Inc, LLC, Corp, Ltd, Co, etc.)
    - collapse whitespace
    - title-case the result
    """
    if not raw:
        return ""
    name = raw.strip()
    # Remove common legal suffixes at the end
    lower = name.lower()
    for suffix in LEGAL_SUFFIXES:
        token = " " + suffix
        if lower.endswith(token):
            name = name[: -len(token)]
            break
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name.title()


def _norm_name(name: str) -> str:
    if not name:
        return ""
    name = name.lower()
    # Remove punctuation and collapse whitespace
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _tokenize(name: str) -> set[str]:
    name = _norm_name(name)
    return set(name.split()) if name else set()


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def normalize_domain(value: str) -> str:
    """
    Take a URL or bare hostname and return a canonical domain, e.g.:
    - https://www.google.com/ → google.com
    - amazon.com/jobs → amazon.com
    """
    if not value:
        return ""
    text = value.strip().lower()
    if not text:
        return ""
    # Ensure we have a scheme so regex can work reliably
    if not re.match(r"^[a-z]+://", text):
        text = "https://" + text
    m = re.search(r"//([^/]+)", text)
    if not m:
        return ""
    host = m.group(1)
    for prefix in ("www.", "careers.", "jobs."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host


def _extract_domain(url: str) -> str:
    return normalize_domain(url)


def find_potential_duplicate_companies(
    name: str,
    website: str | None = None,
    threshold: float = 0.7,
    limit: int = 5,
) -> List[Tuple[Company, float]]:
    """
    Rules-first duplicate detection for companies.
    Returns a list of (company, score) sorted by score (1.0 = perfect match).
    """
    name_tokens = _tokenize(name or "")
    domain = _extract_domain(website or "")

    qs = Company.objects.all()
    if name:
        qs = qs.filter(name__icontains=name) | qs.filter(alias__icontains=name)

    candidates: list[tuple[Company, float]] = []
    for company in qs[:50]:
        score = 0.0
        existing_tokens = _tokenize(company.name) | _tokenize(company.alias)
        score = _jaccard(name_tokens, existing_tokens)

        # Boost when domains match
        existing_domain = _extract_domain(company.website or "")
        if domain and existing_domain and domain == existing_domain:
            score = max(score, 0.9)

        if score >= threshold:
            candidates.append((company, score))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:limit]


@transaction.atomic
def merge_companies(source: Company, target: Company) -> None:
    """
    Merge source company into target.
    - Re-point jobs to target (and sync legacy text field).
    - Merge DoNotSubmit rules.
    - Aggregate simple counters.
    - Delete source.
    """
    if source.pk == target.pk:
        return

    # Jobs
    Job.objects.filter(company_obj=source).update(company_obj=target, company=target.name)

    # DoNotSubmit rules – avoid unique_together conflicts
    for dnd in CompanyDoNotSubmit.objects.filter(company=source):
        existing, created = CompanyDoNotSubmit.objects.get_or_create(
            company=target,
            consultant=dnd.consultant,
            defaults={"until": dnd.until, "reason": dnd.reason},
        )
        if not created:
            # Prefer the later until date and concatenate reasons
            if dnd.until and (not existing.until or dnd.until > existing.until):
                existing.until = dnd.until
            if dnd.reason and dnd.reason not in (existing.reason or ""):
                existing.reason = (existing.reason or "").strip()
                if existing.reason:
                    existing.reason += "\n"
                existing.reason += dnd.reason
            existing.save()
        dnd.delete()

    # Simple metric aggregation
    target.total_submissions += source.total_submissions
    target.total_interviews += source.total_interviews
    target.total_offers += source.total_offers
    target.total_placements += source.total_placements
    if source.last_activity_at and (
        not target.last_activity_at or source.last_activity_at > target.last_activity_at
    ):
        target.last_activity_at = source.last_activity_at
    target.save()

    # Finally, remove the source company
    source.delete()


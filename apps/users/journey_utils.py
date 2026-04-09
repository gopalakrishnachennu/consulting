"""Consultant readiness score and journey steps (single place for dashboard + journey page)."""

from __future__ import annotations

from django.db.models import Q
from django.urls import reverse

from submissions.models import ApplicationSubmission
from resumes.models import ResumeDraft
from interviews_app.models import Interview
from jobs.models import Job


def compute_consultant_readiness(profile, my_submissions) -> int:
    """
    0–100 score: profile depth, roles, pipeline activity, interviews.
    Weights are additive and capped at 100.
    """
    if not profile:
        return 0

    pts = 0
    # Onboarding / profile (35)
    if getattr(profile, "onboarding_completed_at", None):
        pts += 15
    bio = (profile.bio or "").strip()
    if len(bio) >= 40:
        pts += 10
    if profile.marketing_roles.exists():
        pts += 10

    # Resume / drafts (20)
    if ResumeDraft.objects.filter(consultant=profile).exists():
        pts += 20

    # Applications (25)
    active = my_submissions.exclude(
        status__in=[
            ApplicationSubmission.Status.WITHDRAWN,
        ]
    )
    if active.exists():
        pts += 15
    if active.exclude(status=ApplicationSubmission.Status.IN_PROGRESS).count() >= 1:
        pts += 10

    # Interviews (20)
    if my_submissions.filter(
        status__in=[
            ApplicationSubmission.Status.INTERVIEW,
            ApplicationSubmission.Status.OFFER,
            ApplicationSubmission.Status.PLACED,
        ]
    ).exists():
        pts += 12
    if Interview.objects.filter(consultant=profile).exists():
        pts += 8

    return min(100, pts)


def build_journey_steps(profile, my_submissions) -> list[dict]:
    """Ordered checklist for the journey page (includes resolved hrefs)."""
    steps = []

    onboarding_ok = bool(getattr(profile, "onboarding_completed_at", None)) and len(
        (profile.bio or "").strip()
    ) >= 40
    profile_href = (
        reverse("consultant-onboarding")
        if not profile.onboarding_completed_at
        else reverse("consultant-detail", kwargs={"pk": profile.user_id})
    )
    steps.append(
        {
            "id": "profile",
            "title": "Strengthen your profile",
            "description": "Complete onboarding and add a clear bio so recruiters can represent you.",
            "done": onboarding_ok,
            "href": profile_href,
        }
    )

    roles_ok = profile.marketing_roles.exists()
    steps.append(
        {
            "id": "roles",
            "title": "Define target roles",
            "description": "Pick marketing roles so we can match you to the right openings.",
            "done": roles_ok,
            "href": reverse("consultant-detail", kwargs={"pk": profile.user_id}),
        }
    )

    has_pipeline = my_submissions.exclude(
        status=ApplicationSubmission.Status.WITHDRAWN
    ).exists()
    steps.append(
        {
            "id": "applications",
            "title": "Move applications forward",
            "description": "Track status, proof, and pipeline — use the board for a quick view.",
            "done": has_pipeline,
            "href": reverse("submission-list"),
        }
    )
    steps.append(
        {
            "id": "pipeline_board",
            "title": "Pipeline board",
            "description": "Drag cards across stages to mirror where each application stands.",
            "done": has_pipeline,
            "href": reverse("submission-kanban"),
        }
    )

    interview_ok = (
        Interview.objects.filter(consultant=profile).exists()
        or my_submissions.filter(
            status__in=[
                ApplicationSubmission.Status.INTERVIEW,
                ApplicationSubmission.Status.OFFER,
            ]
        ).exists()
    )
    steps.append(
        {
            "id": "interviews",
            "title": "Prepare for interviews",
            "description": "Review schedule, roles, and feedback after each conversation.",
            "done": interview_ok,
            "href": reverse("interview-list"),
        }
    )

    return steps


def at_risk_submissions_queryset(profile):
    """OPEN jobs linked to the consultant where the posting URL looks gone or flagged."""
    if not profile:
        return ApplicationSubmission.objects.none()

    return (
        ApplicationSubmission.objects.filter(consultant=profile)
        .filter(job__status=Job.Status.OPEN)
        .filter(Q(job__possibly_filled=True) | Q(job__original_link_is_live=False))
        .select_related("job")
        .order_by("-updated_at")
    )

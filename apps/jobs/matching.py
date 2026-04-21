"""
Semantic job–consultant matching using OpenAI embeddings + cosine similarity.

Usage:
  from jobs.matching import embed_job, embed_consultant, compute_matches_for_job
"""
import logging
import math
from typing import List, Optional

from django.conf import settings

logger = logging.getLogger(__name__)


def _openai_embed(text: str) -> Optional[List[float]]:
    """Return embedding vector via OpenAI text-embedding-3-small, or None on failure."""
    try:
        import openai
        from core.models import LLMConfig
        from core.security import decrypt_value

        cfg = LLMConfig.load()
        raw_key = decrypt_value(cfg.encrypted_api_key) if getattr(cfg, 'encrypted_api_key', None) else None
        api_key = raw_key or getattr(settings, 'OPENAI_API_KEY', None)
        if not api_key:
            logger.warning("No OpenAI API key available for embeddings")
            return None

        client = openai.OpenAI(api_key=api_key)
        response = client.embeddings.create(
            model="text-embedding-3-small",
            input=text[:8000],
        )
        return response.data[0].embedding
    except Exception:
        logger.exception("Embedding generation failed")
        return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _build_job_text(job) -> str:
    parts = [job.title]
    if job.company:
        parts.append(f"Company: {job.company}")
    if job.location:
        parts.append(f"Location: {job.location}")
    if job.description:
        parts.append(job.description[:3000])
    if job.parsed_jd:
        skills = job.parsed_jd.get('skills') or []
        if skills:
            parts.append("Required skills: " + ", ".join(skills[:30]))
    return "\n".join(parts)


def _build_consultant_text(consultant) -> str:
    parts = []
    name = consultant.user.get_full_name() or consultant.user.username
    parts.append(f"Consultant: {name}")
    if consultant.bio:
        parts.append(consultant.bio[:500])
    if consultant.skills:
        skills = consultant.skills if isinstance(consultant.skills, list) else []
        parts.append("Skills: " + ", ".join(str(s) for s in skills[:40]))
    roles = list(consultant.marketing_roles.values_list('name', flat=True))
    if roles:
        parts.append("Marketing roles: " + ", ".join(roles))
    for exp in consultant.experience.all()[:5]:
        parts.append(f"{exp.title} at {exp.company}")
    if consultant.base_resume_text:
        parts.append(consultant.base_resume_text[:1000])
    return "\n".join(parts)


def embed_job(job) -> bool:
    """Generate and store embedding for a job. Returns True on success."""
    from jobs.models import JobEmbedding
    text = _build_job_text(job)
    vector = _openai_embed(text)
    if vector is None:
        return False
    JobEmbedding.objects.update_or_create(
        job=job,
        defaults={"vector": vector, "model": "text-embedding-3-small"},
    )
    return True


def embed_consultant(consultant) -> bool:
    """Generate and store embedding for a consultant profile. Returns True on success."""
    from users.models import ConsultantEmbedding
    text = _build_consultant_text(consultant)
    vector = _openai_embed(text)
    if vector is None:
        return False
    ConsultantEmbedding.objects.update_or_create(
        consultant=consultant,
        defaults={"vector": vector, "model": "text-embedding-3-small"},
    )
    return True


def compute_matches_for_job(job, top_n: int = 20) -> List[dict]:
    """
    Compute cosine similarity between job and all consultants that have embeddings.
    Persists MatchScore rows and returns top_n ranked results.

    Returns list of dicts: {consultant, score, score_pct, rank}
    """
    from jobs.models import JobEmbedding, MatchScore
    from users.models import ConsultantEmbedding

    try:
        job_emb = JobEmbedding.objects.get(job=job)
    except JobEmbedding.DoesNotExist:
        if not embed_job(job):
            return []
        try:
            job_emb = JobEmbedding.objects.get(job=job)
        except JobEmbedding.DoesNotExist:
            return []

    job_vec = job_emb.vector
    consultant_embs = ConsultantEmbedding.objects.select_related('consultant__user').all()

    scores = []
    for emb in consultant_embs:
        sim = _cosine_similarity(job_vec, emb.vector)
        scores.append((emb.consultant, sim))

    scores.sort(key=lambda x: x[1], reverse=True)

    # Persist scores
    MatchScore.objects.filter(job=job).delete()
    bulk = []
    for rank, (consultant, sim) in enumerate(scores, start=1):
        bulk.append(MatchScore(job=job, consultant=consultant, score=sim, rank=rank))
    if bulk:
        MatchScore.objects.bulk_create(bulk, ignore_conflicts=True)

    return [
        {"consultant": c, "score": s, "score_pct": int(s * 100), "rank": i}
        for i, (c, s) in enumerate(scores[:top_n], start=1)
    ]


def notify_top_matches_for_job(job, top_n: int = 5):
    """
    Send in-app notifications to the top_n matched consultants for an approved job.
    Silently skips if no embeddings exist.
    """
    from jobs.models import MatchScore
    from core.notification_utils import create_notification

    top_scores = (
        MatchScore.objects.filter(job=job)
        .select_related('consultant__user')
        .order_by('rank')[:top_n]
    )
    for ms in top_scores:
        try:
            create_notification(
                recipient=ms.consultant.user,
                kind='JOB',
                title=f"New job match: {job.title}",
                message=f"{job.title} at {job.company} is a {ms.score_pct}% match for your profile.",
                link=f"/jobs/{job.pk}/",
                dedupe_key=f"match-{job.pk}-{ms.consultant_id}",
            )
        except Exception:
            logger.exception("Failed to notify consultant %s for job %s", ms.consultant_id, job.pk)

import json
import logging
import re
from django.utils import timezone

from .models import Job
from resumes.services import LLMService
from users.models import ConsultantProfile
from django.db.models import Q
from resumes.prompt_strings import JD_PARSER_SYSTEM_PROMPT, JD_PARSER_USER_PROMPT

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a","an","and","are","as","at","be","by","for","from","has","have","in","is","it","its","of","on","or","that",
    "the","to","with","will","you","your","we","our","they","their","this","these","those",
}


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokenize(s: str) -> set[str]:
    s = _norm_text(s)
    tokens = set(re.findall(r"[a-z0-9][a-z0-9\+\.\#\-]{1,}", s))
    return {t for t in tokens if t not in _STOPWORDS and len(t) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def find_potential_duplicate_jobs(
    *,
    title: str,
    company: str,
    description: str = "",
    exclude_job_id: int | None = None,
    limit: int = 5,
):
    """
    Rules-based duplicate detection:
    - Strong signal: same company + very similar title
    - Secondary: description similarity (Jaccard on tokens)

    Returns list of dicts: {job, title_score, desc_score, overall_score}
    """
    title_n = _norm_text(title)
    company_n = _norm_text(company)
    desc_tokens = _tokenize(description or "")

    if not title_n or not company_n:
        return []

    qs = Job.objects.all()
    if exclude_job_id:
        qs = qs.exclude(pk=exclude_job_id)
    # Narrow candidate set cheaply
    qs = qs.filter(company__icontains=company.strip()).only("id", "title", "company", "description", "status", "created_at")

    title_tokens = _tokenize(title_n)
    results = []
    for j in qs[:200]:  # safety cap
        jt = _tokenize(j.title)
        title_score = _jaccard(title_tokens, jt)
        if title_score < 0.55 and company_n != _norm_text(j.company):
            continue
        desc_score = _jaccard(desc_tokens, _tokenize(j.description or "")) if desc_tokens else 0.0
        overall = (title_score * 0.75) + (desc_score * 0.25)
        if overall >= 0.62 or (title_score >= 0.72 and desc_score >= 0.35):
            results.append(
                {
                    "job": j,
                    "title_score": round(title_score, 2),
                    "desc_score": round(desc_score, 2),
                    "overall_score": round(overall, 2),
                }
            )
    results.sort(key=lambda r: r["overall_score"], reverse=True)
    return results[:limit]


def rule_parse_jd(description: str) -> dict:
    """
    Rules-first JD parsing (no LLM):
    - Extract required_skills by matching against existing consultant skills
    - Lightweight extraction of keywords from common 'Requirements' style sections
    """
    text = (description or "").strip()
    if not text:
        return {}

    low = text.lower()

    # Build known skill universe from stored consultant skills (data-driven, no tokens)
    known = set()
    for skills in ConsultantProfile.objects.values_list("skills", flat=True):
        if not skills:
            continue
        try:
            for s in skills:
                if isinstance(s, str) and s.strip():
                    known.add(s.strip().lower())
        except Exception:
            continue

    required = []
    if known:
        # Prefer exact/phrase hits (substring match) for multi-word skills
        for skill in sorted(known, key=lambda x: (-len(x), x))[:2500]:
            if len(skill) < 2:
                continue
            if skill in low:
                required.append(skill)
            if len(required) >= 40:
                break

    # Fallback: try to capture bullet-ish requirement lines as keywords
    req_section = ""
    m = re.search(r"(requirements|what you will do|qualifications)\s*:?\s*(.+)", low, re.IGNORECASE | re.DOTALL)
    if m:
        req_section = m.group(2)[:1500]
    if not required and req_section:
        bullets = re.findall(r"(?:^|\n)\s*[-•\*]\s*([^\n]{3,120})", req_section)
        # keep short phrases as "required_skills" candidates
        for b in bullets[:20]:
            phrase = re.sub(r"[^a-z0-9\+\.\#\-\s]", " ", b.lower()).strip()
            phrase = re.sub(r"\s+", " ", phrase)
            if phrase and phrase not in required and len(phrase) <= 40:
                required.append(phrase)

    return {
        "required_skills": required[:40],
        "source": "rules",
    }


class JDParserService:
    @staticmethod
    def parse_job(job: Job, actor=None):
        """
        Parse JD into structured JSON and persist it on the Job.
        Rules-first (no tokens). Uses LLM only if rules parsing finds nothing AND LLM is configured.
        """
        if not job or not job.description:
            return False, "Missing job description"

        # 1) Rules-first parse
        data = rule_parse_jd(job.description)
        if data and data.get("required_skills"):
            job.parsed_jd = data
            job.parsed_jd_status = "OK_RULES"
            job.parsed_jd_error = ""
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd", "parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return True, ""

        # 2) LLM fallback (only if configured)
        llm = LLMService()
        if not llm.client:
            job.parsed_jd_status = "ERROR"
            job.parsed_jd_error = "No rules parse result and LLM not configured"
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return False, job.parsed_jd_error

        system_prompt = JD_PARSER_SYSTEM_PROMPT
        user_prompt = JD_PARSER_USER_PROMPT.replace("{jd_text}", job.description)
        content, _, error = llm.generate_with_prompts(job, None, system_prompt, user_prompt, actor=actor, force_new=True)
        if error or not content:
            job.parsed_jd_status = "ERROR"
            job.parsed_jd_error = error or "Empty parser response"
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return False, job.parsed_jd_error

        try:
            data = json.loads(content)
        except Exception:
            # Try to extract JSON block
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                try:
                    data = json.loads(content[start:end+1])
                except Exception as exc:
                    job.parsed_jd_status = "ERROR"
                    job.parsed_jd_error = f"Parser JSON decode failed: {exc}"
                    job.parsed_jd_updated_at = timezone.now()
                    job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
                    return False, job.parsed_jd_error
            else:
                job.parsed_jd_status = "ERROR"
                job.parsed_jd_error = "Parser returned non-JSON"
                job.parsed_jd_updated_at = timezone.now()
                job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
                return False, job.parsed_jd_error

        if not isinstance(data, dict):
            job.parsed_jd_status = "ERROR"
            job.parsed_jd_error = "Parser output is not a JSON object"
            job.parsed_jd_updated_at = timezone.now()
            job.save(update_fields=["parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
            return False, job.parsed_jd_error

        job.parsed_jd = data
        job.parsed_jd_status = "OK"
        job.parsed_jd_error = ""
        job.parsed_jd_updated_at = timezone.now()
        job.save(update_fields=["parsed_jd", "parsed_jd_status", "parsed_jd_error", "parsed_jd_updated_at"])
        return True, ""


def ensure_parsed_jd(job: Job, actor=None):
    if not job.parsed_jd:
        return JDParserService.parse_job(job, actor=actor)
    return True, ""


def _normalize_list(values):
    if not values:
        return []
    out = []
    for v in values:
        if not v:
            continue
        if isinstance(v, str):
            out.append(v.strip().lower())
        else:
            out.append(str(v).strip().lower())
    return out


def _score_job_for_consultant(job: Job, consultant: ConsultantProfile) -> int:
    """
    Heuristic score:
    - Overlap between consultant skills and parsed_jd.required_skills
    - Bonus for matching marketing roles
    """
    score = 0
    skills = _normalize_list(consultant.skills)
    parsed = job.parsed_jd or {}
    required = _normalize_list(parsed.get("required_skills") or [])

    # Skill overlap
    if skills and required:
        overlap = set(skills) & set(required)
        score += len(overlap) * 5

    # Marketing role alignment
    consultant_roles = set(
        consultant.marketing_roles.values_list("id", flat=True)
    )
    job_roles = set(job.marketing_roles.values_list("id", flat=True))
    if consultant_roles and job_roles:
        score += len(consultant_roles & job_roles) * 3

    # Fallback: slight score if at least one skill term appears in description
    if score == 0 and skills and job.description:
        desc = job.description.lower()
        for s in skills[:10]:
            if s and s in desc:
                score += 1

    return score


def match_jobs_for_consultant(
    consultant: ConsultantProfile, limit: int = 10
):
    """
    Return a list of best matching OPEN jobs for a consultant.
    """
    qs = Job.objects.filter(status=Job.Status.OPEN)
    scores = []
    for job in qs.prefetch_related("marketing_roles"):
        s = _score_job_for_consultant(job, consultant)
        if s > 0:
            scores.append((s, job))
    scores.sort(key=lambda x: x[0], reverse=True)
    return [job for _, job in scores[:limit]]


def match_consultants_for_job(
    job: Job, limit: int = 10
):
    """
    Return a list of best matching consultants for a given job.
    """
    # Simple filter to narrow down candidates
    qs = ConsultantProfile.objects.filter(
        status=ConsultantProfile.Status.ACTIVE
    ).prefetch_related("marketing_roles", "user")

    results = []
    for consultant in qs:
        s = _score_job_for_consultant(job, consultant)
        if s > 0:
            results.append((s, consultant))

    results.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in results[:limit]]


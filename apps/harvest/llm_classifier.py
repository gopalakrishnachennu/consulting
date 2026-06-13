"""
LLM-based job classification — two separate functions:

1. classify_batch()  — category classifier (second-pass after enrichment).
   Asks "what category is this job?" (Engineering, DevOps, etc.)
   Output: {"id", "category", "confidence"}

2. gate_jobs_batch() — Tier-2 JD relevance gate (new).
   Asks "is this a tech consulting role we should pursue?" YES | NO.
   Runs on AMBIGUOUS jobs BEFORE committing to full JD fetch.
   Output: {"id", "decision", "confidence", "category", "reason"}

Both use the OpenAI chat completions API (model: gpt-4o-mini by default).
Jobs are batched (20/call for gate, 10/call for classify) to keep cost minimal.
Estimated cost: ~$0.001 per 20 gate calls, ~$0.001 per 10 classify calls.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Fixed category list — must match _CATEGORY_PATTERNS in enrichments.py
VALID_CATEGORIES: list[str] = [
    "AI / ML",
    "Data & Analytics",
    "Security",
    "DevOps / SRE",
    "Engineering",
    "Product",
    "Design",
    "Marketing",
    "Sales",
    "Customer Success",
    "Finance",
    "HR & People",
    "Legal",
    "Operations",
    "Healthcare",
    "Education",
]

_CATEGORY_SET = frozenset(VALID_CATEGORIES)

BATCH_SIZE = 10          # jobs per LLM call
LLM_CONFIDENCE = 0.82   # stored category_confidence for LLM-classified jobs
LLM_SOURCE = "llm"      # classification_source value

_SYSTEM_PROMPT = """\
You are a job classification assistant. Classify each job into exactly one category from this list:

""" + "\n".join(f"  {c}" for c in VALID_CATEGORIES) + """

Rules:
- Return ONLY a JSON array — no prose, no markdown fences.
- Each element: {"id": <integer>, "category": "<category from list>", "confidence": <0.0–1.0>}
- confidence: 1.0 = obvious match, 0.6 = educated guess
- If genuinely ambiguous, pick the closest match and set confidence < 0.7
- Never return a category not in the list above"""


def _make_user_prompt(jobs: list[dict]) -> str:
    lines = []
    for j in jobs:
        title = (j.get("title") or "").strip()[:120]
        desc = (j.get("description") or "").strip()[:300]
        lines.append(f'ID {j["id"]}: Title="{title}" | Excerpt="{desc}"')
    return "Classify these jobs:\n\n" + "\n\n".join(lines)


def _parse_llm_response(text: str) -> list[dict]:
    """Extract the JSON array from the LLM response robustly."""
    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text.strip())
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # Try finding the first [...] block
    m = re.search(r"\[.*\]", cleaned, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return []


def _record_llm_failure(context: str, exc: Exception) -> None:
    """Surface LLM API failures in the in-app incident log (best-effort).

    A swallowed API error here silently leaves a whole batch unclassified —
    recording it makes the outage visible on /core/incidents/ instead of only
    in worker logs."""
    try:
        from core.models import ErrorLog
        ErrorLog.objects.create(
            severity=ErrorLog.Severity.ERROR,
            path=f"celery://harvest.llm_classifier.{context}",
            method="TASK",
            status_code=502,
            error_type=type(exc).__name__[:200],
            error_message=str(exc)[:2000],
        )
    except Exception:
        logger.debug("llm_classifier: ErrorLog write skipped", exc_info=True)


def _resolve_llm(api_key: str | None, model: str):
    """Resolve (key, base_url, model, do_log) for a harvest LLM call.

    Central LLMConfig routing is OPT-IN via the 'harvest_use_central_llm' feature
    flag. Default (flag off / missing) = unchanged behavior: caller/env key, the
    caller's model, OpenAI default endpoint, and no usage logging — so the
    scheduled harvest is byte-identical until the flag is deliberately turned on.
    """
    try:
        from core.models import FeatureFlag
        ff = FeatureFlag.objects.filter(key="harvest_use_central_llm").first()
        use_central = bool(ff and ff.is_enabled)
    except Exception:
        use_central = False

    if use_central:
        try:
            from core.models import LLMConfig
            from core.security import decrypt_value
            cfg = LLMConfig.load()
            key = decrypt_value(cfg.encrypted_api_key) or api_key or os.environ.get("OPENAI_API_KEY", "")
            cfg_model = (cfg.validation_model or "").strip() or cfg.active_model
            return key, cfg.effective_base_url(), (cfg_model or model), True
        except Exception:
            logger.exception("llm_classifier: central LLMConfig resolve failed; using defaults")

    return (api_key or os.environ.get("OPENAI_API_KEY", "")), None, model, False


def _log_harvest_usage(model: str, response, request_type: str) -> None:
    """Best-effort: record a harvest LLM call in LLMUsageLog (one row per batch)."""
    try:
        from core.models import LLMUsageLog
        from core.llm_services import calculate_cost
        u = getattr(response, "usage", None)
        pt = getattr(u, "prompt_tokens", 0) or 0
        ct = getattr(u, "completion_tokens", 0) or 0
        tt = getattr(u, "total_tokens", 0) or 0
        costs = calculate_cost(model, pt, ct)
        LLMUsageLog.objects.create(
            request_type=request_type, model_name=model,
            prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            cost_input=costs["input"], cost_output=costs["output"], cost_total=costs["total"],
            success=True,
        )
    except Exception:
        logger.debug("llm_classifier: usage logging skipped", exc_info=True)


def classify_batch(
    jobs: list[dict],
    *,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
) -> dict[int, dict[str, Any]]:
    """
    Classify a batch of jobs via LLM.

    Args:
        jobs: list of dicts with keys: id (int), title (str), description (str)
        api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
        model: OpenAI model name.

    Returns:
        dict mapping job id → {"category": str, "confidence": float}
        Missing IDs = LLM returned no result for that job (caller should skip).
    """
    if not jobs:
        return {}

    key, base_url, model, do_log = _resolve_llm(api_key, model)
    if not key:
        logger.warning("llm_classifier: no API key available — skipping LLM pass")
        return {}

    try:
        import openai
    except ImportError:
        logger.warning("llm_classifier: openai package not installed")
        return {}

    client = openai.OpenAI(api_key=key, base_url=base_url)
    user_prompt = _make_user_prompt(jobs)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=len(jobs) * 60 + 50,
        )
    except Exception as exc:
        logger.error("llm_classifier: API call failed: %s", exc)
        _record_llm_failure("classify_batch", exc)
        return {}

    if do_log:
        _log_harvest_usage(model, response, "harvest_classification")

    raw_text = (response.choices[0].message.content or "").strip()
    items = _parse_llm_response(raw_text)

    results: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            job_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        category = str(item.get("category") or "").strip()
        if category not in _CATEGORY_SET:
            # Try case-insensitive rescue
            for valid in VALID_CATEGORIES:
                if valid.lower() == category.lower():
                    category = valid
                    break
            else:
                continue  # discard invalid category
        confidence = float(item.get("confidence") or LLM_CONFIDENCE)
        results[job_id] = {"category": category, "confidence": min(1.0, max(0.0, confidence))}

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Tier-2 JD Content Gate — binary YES/NO relevance gate
# ─────────────────────────────────────────────────────────────────────────────

_GATE_SYSTEM_PROMPT = """\
You are a hiring intake filter for a tech consulting firm called GoCareers.
We EXCLUSIVELY place consultants in B2B technology roles.

Target roles (answer YES):
  • Software / backend / frontend / full-stack engineering
  • DevOps, SRE, Cloud (AWS/GCP/Azure), Infrastructure, Platform engineering
  • Data engineering, Data pipelines, Analytics engineering
  • AI / ML / LLM engineering
  • Cybersecurity, SecOps, Network engineering
  • Enterprise platforms: ServiceNow, Salesforce, SAP, Workday (IT roles), Oracle, Dynamics
  • Healthcare IT: Epic, Cerner, Meditech, HL7/FHIR, EHR/EMR analysts
  • QA automation, Test engineering
  • IT infrastructure, Systems administration (tech-depth roles)

NOT target roles (answer NO):
  • Clinical / nursing / pharmacy / lab / radiology / patient care
  • Finance, accounting, FP&A, audit
  • HR, recruiting, talent acquisition, payroll
  • Sales, marketing, PR, communications
  • Legal, compliance, policy
  • Operations management (non-tech: logistics, facilities, retail, food service)
  • General management / C-suite without explicit tech delivery scope

Rules:
  - When a title is ambiguous, rely on the JD snippet to decide.
  - "Manager, X Engineering" = YES if they manage engineers.
  - "Analyst" = YES if the snippet shows data/BI/SQL/cloud; NO if it shows business/finance/marketing.
  - "Consultant" = YES if the snippet shows IT systems; NO if it shows strategy/management.
  - Confidence 1.0 = crystal clear. 0.6 = educated guess from limited snippet.
  - If snippet is empty or too short to judge: confidence ≤ 0.55, lean YES if title sounds tech.

Return ONLY a JSON array — no prose, no markdown fences:
[{"id": <int>, "decision": "YES"|"NO", "confidence": <0.0-1.0>, "category": "<short tech category or 'non-tech'>", "reason": "<one sentence>"}]"""


def _make_gate_prompt(jobs: list[dict]) -> str:
    """Build the user prompt for gate_jobs_batch()."""
    lines = []
    for j in jobs:
        title = (j.get("title") or "").strip()[:120]
        company = (j.get("company") or "").strip()[:80]
        dept = (j.get("department") or "").strip()[:80]
        snippet = (j.get("snippet") or "").strip()[:800]
        parts = [f'ID {j["id"]}: Title="{title}"']
        if company:
            parts.append(f'Company="{company}"')
        if dept:
            parts.append(f'Department="{dept}"')
        if snippet:
            parts.append(f'Snippet="{snippet}"')
        else:
            parts.append('Snippet=""')
        lines.append(" | ".join(parts))
    return "Evaluate these jobs:\n\n" + "\n\n".join(lines)


def gate_jobs_batch(
    jobs: list[dict],
    *,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
    confidence_threshold: float = 0.65,
) -> dict[int, dict[str, Any]]:
    """
    Tier-2 JD content gate — binary YES/NO relevance filter.

    Args:
        jobs: list of dicts with keys:
              id (int), title (str), company (str), department (str), snippet (str)
              snippet = first 800 chars of clean JD text (from list payload or detail fetch)
        api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
        model: OpenAI model. gpt-4o-mini is recommended (cheap + fast).
        confidence_threshold: results below this are returned with decision="UNCERTAIN".

    Returns:
        dict mapping job id → {
            "decision":   "YES" | "NO" | "UNCERTAIN",
            "confidence": float (0.0–1.0),
            "category":   str  (e.g. "devops", "data-engineering", "non-tech"),
            "reason":     str  (one-sentence LLM reason, for audit),
        }
        Missing IDs = LLM returned no result (caller treats as UNCERTAIN).
    """
    if not jobs:
        return {}

    key, base_url, model, do_log = _resolve_llm(api_key, model)
    if not key:
        logger.warning("gate_jobs_batch: no API key available — skipping JD gate")
        return {}

    try:
        import openai
    except ImportError:
        logger.warning("gate_jobs_batch: openai package not installed")
        return {}

    client = openai.OpenAI(api_key=key, base_url=base_url)
    user_prompt = _make_gate_prompt(jobs)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _GATE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.05,           # near-deterministic for a filter
            max_tokens=len(jobs) * 80 + 50,
        )
    except Exception as exc:
        logger.error("gate_jobs_batch: API call failed: %s", exc)
        _record_llm_failure("gate_jobs_batch", exc)
        return {}

    if do_log:
        _log_harvest_usage(model, response, "harvest_jd_gate")

    raw_text = (response.choices[0].message.content or "").strip()
    items = _parse_llm_response(raw_text)

    results: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            job_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not job_id:
            continue

        raw_decision = str(item.get("decision") or "").strip().upper()
        conf = float(item.get("confidence") or 0.0)
        conf = min(1.0, max(0.0, conf))
        category = str(item.get("category") or "").strip().lower()[:64]
        reason = str(item.get("reason") or "").strip()[:512]

        # Apply confidence threshold — below it, mark UNCERTAIN for human review
        if conf < confidence_threshold:
            decision = "UNCERTAIN"
        elif raw_decision == "YES":
            decision = "YES"
        elif raw_decision == "NO":
            decision = "NO"
        else:
            decision = "UNCERTAIN"

        results[job_id] = {
            "decision":   decision,
            "confidence": conf,
            "category":   category,
            "reason":     reason,
        }

    logger.info(
        "gate_jobs_batch: %d jobs → %d results (YES=%d NO=%d UNCERTAIN=%d)",
        len(jobs),
        len(results),
        sum(1 for r in results.values() if r["decision"] == "YES"),
        sum(1 for r in results.values() if r["decision"] == "NO"),
        sum(1 for r in results.values() if r["decision"] == "UNCERTAIN"),
    )
    return results


_REMOTE_LOCATION_SYSTEM_PROMPT = (
    "You extract the primary WORK LOCATION of a job from its description. Many "
    "'remote' jobs are still tied to a country or city via time zone, right-to-work, "
    "'must reside in', visa, or office-hub language. Reply with STRICT JSON only, no "
    'prose: {"location": "<City, Country | Country | empty>", "confidence": <0.0-1.0>}. '
    "Use an empty string only if the job is genuinely location-agnostic. Prefer a "
    "country name or 'City, Country'. Never invent a location not implied by the text."
)


def extract_remote_location(
    title: str,
    description: str,
    *,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
) -> str:
    """Ask the LLM for a remote job's work location from its JD.

    Returns a location string ('Berlin, Germany', 'United States') or '' when the
    job is location-agnostic / on failure. Best-effort — never raises. Honors the
    central LLM config when the 'harvest_use_central_llm' flag is on.
    """
    desc = (description or "").strip()
    if not desc:
        return ""
    key, base_url, model, do_log = _resolve_llm(api_key, model)
    if not key:
        return ""
    try:
        import openai
    except ImportError:
        return ""

    client = openai.OpenAI(api_key=key, base_url=base_url)
    user_prompt = f"TITLE: {title or ''}\n\nDESCRIPTION:\n{desc[:4000]}"
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _REMOTE_LOCATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=60,
        )
    except Exception as exc:
        _record_llm_failure("extract_remote_location", exc)
        return ""

    if do_log:
        _log_harvest_usage(model, response, "remote_location")
    try:
        text = (response.choices[0].message.content or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text).strip()
        data = json.loads(text)
        return (data.get("location") or "").strip()[:128]
    except Exception:
        return ""

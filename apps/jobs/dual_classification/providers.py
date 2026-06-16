from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from harvest.enrichments import CURRENT_ENRICHMENT_VERSION, extract_enrichments
from resumes.pipeline.llm_client import PipelineLLMClient

from .schema import canonical_from_enrichment, validate_canonical_output


@dataclass
class ClassificationContext:
    raw_job: Any
    input_payload: dict[str, Any]
    input_hash: str


@dataclass
class ProviderResult:
    provider: str
    provider_role: str
    provider_version: str = ""
    prompt_version: str = ""
    confidence: float | None = None
    raw_output: dict[str, Any] = field(default_factory=dict)
    normalized_output: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class BaseProvider:
    code = ""
    provider_role = ""
    provider_version = ""
    prompt_version = ""

    def classify(self, context: ClassificationContext) -> ProviderResult:
        raise NotImplementedError


class BackendRulesProvider(BaseProvider):
    code = "backend_rules"
    provider_role = "PRIMARY"
    provider_version = CURRENT_ENRICHMENT_VERSION

    def classify(self, context: ClassificationContext) -> ProviderResult:
        enriched = extract_enrichments(context.input_payload)
        normalized = canonical_from_enrichment(context.raw_job, enriched, context.input_payload)
        confidence = normalized.get("scores", {}).get("classification_confidence")
        return ProviderResult(
            provider=self.code,
            provider_role=self.provider_role,
            provider_version=self.provider_version,
            confidence=confidence,
            raw_output=enriched,
            normalized_output=normalized,
        )


class SecondaryStubProvider(BaseProvider):
    """Placeholder secondary provider until Codex/Claude runtime is wired in."""

    code = "secondary_stub"
    provider_role = "SECONDARY"
    provider_version = "shadow_v1"

    def classify(self, context: ClassificationContext) -> ProviderResult:
        normalized = {
            "identity": {
                "raw_job_id": context.raw_job.pk,
                "provider_note": "Secondary provider not configured yet.",
            }
        }
        return ProviderResult(
            provider=self.code,
            provider_role=self.provider_role,
            provider_version=self.provider_version,
            warnings=["secondary_provider_not_configured"],
            raw_output={},
            normalized_output=normalized,
        )


def _strip_code_fences(text: str) -> str:
    return re.sub(r"```(?:json)?\s*|\s*```", "", (text or "").strip(), flags=re.IGNORECASE)


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = _strip_code_fences(text)
    if not cleaned:
        return {}
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return {}
    return {}


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _coerce_int(value: Any) -> int | None:
    if value in (None, "", []):
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+", value)
            if not match:
                return None
            return int(match.group(0))
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _coerce_optional_bool(value: Any) -> bool | None:
    if value in (None, "", []):
        return None
    return _coerce_bool(value)


def _coerce_canonical_output(context: ClassificationContext, payload: dict[str, Any]) -> dict[str, Any]:
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
    skills = payload.get("skills") if isinstance(payload.get("skills"), dict) else {}
    requirements = payload.get("requirements") if isinstance(payload.get("requirements"), dict) else {}
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}

    return {
        "identity": {
            "raw_job_id": context.raw_job.pk,
            "title": str(identity.get("title") or context.raw_job.title or ""),
            "company_name": str(identity.get("company_name") or context.raw_job.company_name or ""),
            "platform_slug": str(identity.get("platform_slug") or context.raw_job.platform_slug or ""),
            "original_url": str(identity.get("original_url") or context.raw_job.original_url or ""),
        },
        "classification": {
            "job_category": str(classification.get("job_category") or ""),
            "job_domain": str(classification.get("job_domain") or ""),
            "department_normalized": str(classification.get("department_normalized") or ""),
            "role_category": str(classification.get("role_category") or ""),
        },
        "skills": {
            "skills": _coerce_list(skills.get("skills")),
            "tech_stack": _coerce_list(skills.get("tech_stack")),
        },
        "requirements": {
            "years_required": _coerce_int(requirements.get("years_required")),
            "years_required_max": _coerce_int(requirements.get("years_required_max")),
            "education_required": str(requirements.get("education_required") or ""),
            "visa_sponsorship": _coerce_optional_bool(requirements.get("visa_sponsorship")),
            "work_authorization": str(requirements.get("work_authorization") or ""),
            "clearance_required": _coerce_bool(requirements.get("clearance_required")),
            "clearance_level": str(requirements.get("clearance_level") or ""),
        },
        "location": {
            "country": str(location.get("country") or ""),
            "country_codes": _coerce_list(location.get("country_codes")),
            "location_type": str(location.get("location_type") or ""),
            "is_remote": _coerce_bool(location.get("is_remote")),
        },
    }


def _secondary_prompt_version() -> str:
    from .config import secondary_prompt_version

    return secondary_prompt_version()


class RuntimeLLMSecondaryProvider(BaseProvider):
    provider_role = "SECONDARY"

    def __init__(self, provider_code: str):
        self.code = provider_code
        self.prompt_version = _secondary_prompt_version()

    @property
    def provider_version(self) -> str:
        return getattr(self, "_provider_version", "")

    @staticmethod
    def available_provider_codes() -> tuple[str, ...]:
        return ("codex", "claude")

    @staticmethod
    def system_prompt() -> str:
        return (
            "You are a strict JSON extractor for technical job descriptions.\n"
            "Return exactly one JSON object with these top-level objects: "
            "identity, classification, skills, requirements, location.\n"
            "Rules:\n"
            "- Output JSON only. No prose. No markdown fences.\n"
            "- Use empty strings, empty arrays, or null when unknown.\n"
            "- Do not invent skills, years, sponsorship, clearance, or countries that are not supported by the JD.\n"
            "- Prefer JD evidence over title assumptions when they conflict.\n"
            "- skills.skills and skills.tech_stack must be arrays of strings.\n"
            "- requirements.years_required and years_required_max must be integers or null.\n"
            "- requirements.visa_sponsorship must be true, false, or null.\n"
            "- location.is_remote and requirements.clearance_required must be booleans.\n"
        )

    def user_prompt(self, context: ClassificationContext) -> str:
        payload = context.input_payload
        return json.dumps(
            {
                "task": "Extract canonical dual-classification JSON for this RawJob.",
                "provider_label": self.code,
                "prompt_version": self.prompt_version,
                "raw_job": {
                    "raw_job_id": context.raw_job.pk,
                    "title": context.raw_job.title or "",
                    "company_name": context.raw_job.company_name or "",
                    "platform_slug": context.raw_job.platform_slug or "",
                    "original_url": context.raw_job.original_url or "",
                    "location_raw": payload.get("location_raw") or "",
                    "country_code": payload.get("country_code") or "",
                    "description_clean": payload.get("description_clean") or payload.get("description") or "",
                    "requirements_section": payload.get("requirements") or "",
                    "responsibilities_section": payload.get("responsibilities") or "",
                    "benefits_section": payload.get("benefits") or "",
                },
                "schema": {
                    "identity": {
                        "raw_job_id": "integer",
                        "title": "string",
                        "company_name": "string",
                        "platform_slug": "string",
                        "original_url": "string",
                    },
                    "classification": {
                        "job_category": "string",
                        "job_domain": "string",
                        "department_normalized": "string",
                        "role_category": "string",
                    },
                    "skills": {
                        "skills": ["string"],
                        "tech_stack": ["string"],
                    },
                    "requirements": {
                        "years_required": "integer|null",
                        "years_required_max": "integer|null",
                        "education_required": "string",
                        "visa_sponsorship": "boolean|null",
                        "work_authorization": "string",
                        "clearance_required": "boolean",
                        "clearance_level": "string",
                    },
                    "location": {
                        "country": "string",
                        "country_codes": ["string"],
                        "location_type": "string",
                        "is_remote": "boolean",
                    },
                },
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )

    def classify(self, context: ClassificationContext) -> ProviderResult:
        client = PipelineLLMClient()
        available, availability_error = client.is_available()
        if not available:
            raise RuntimeError(availability_error or "Central LLM runtime is not available.")
        cap_ok, cap_error = client.check_token_cap()
        if not cap_ok:
            raise RuntimeError(cap_error or "Central LLM runtime token cap has been reached.")

        model_name = client.validation_model
        content, _tokens, error = client.call(
            self.system_prompt(),
            self.user_prompt(context),
            request_type=f"jobs_dual_classification_{self.code}",
            model=model_name,
            temperature=0.10,
            max_tokens=min(int(getattr(client.config, "max_output_tokens", 2000) or 2000), 2400),
        )
        if error:
            raise RuntimeError(error)

        parsed = _extract_json_object(content or "")
        if not parsed:
            raise ValueError("Secondary provider returned no parseable JSON object.")

        normalized = _coerce_canonical_output(context, parsed)
        schema_errors = validate_canonical_output(normalized)
        if schema_errors:
            raise ValueError(" ; ".join(schema_errors[:4]))

        confidence = parsed.get("confidence")
        try:
            confidence_value = float(confidence) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence_value = None

        self._provider_version = str(model_name or "")
        return ProviderResult(
            provider=self.code,
            provider_role=self.provider_role,
            provider_version=self.provider_version,
            prompt_version=self.prompt_version,
            confidence=confidence_value,
            raw_output={
                "response_text": content or "",
                "provider_label": self.code,
                "llm_model": model_name,
            },
            normalized_output=normalized,
        )

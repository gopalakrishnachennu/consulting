from __future__ import annotations

import re
from typing import Any

from django.utils import timezone

from harvest.enrichments import infer_country_from_location
from harvest.location_resolver import COUNTRY_CODE_TO_NAME, _code_for_country

from .dual_classification.effective import effective_raw_job_classification


_REMOTE_PATTERNS = (
    re.compile(r"\bremote\b", re.I),
    re.compile(r"\bwork from home\b", re.I),
    re.compile(r"\banywhere\b", re.I),
)
_HYBRID_PATTERNS = (re.compile(r"\bhybrid\b", re.I),)
_ONSITE_PATTERNS = (re.compile(r"\bon[- ]site\b", re.I), re.compile(r"\bin office\b", re.I))

_NO_SPONSOR_PATTERNS = (
    re.compile(r"\bno\s+visa\s+sponsor", re.I),
    re.compile(r"\bwill\s+not\s+sponsor", re.I),
    re.compile(r"\bunable\s+to\s+sponsor", re.I),
    re.compile(r"\bmust\s+be\s+authorized\b", re.I),
)
_YES_SPONSOR_PATTERNS = (
    re.compile(r"\bvisa\s+sponsor(ship)?\s+available\b", re.I),
    re.compile(r"\bcan\s+sponsor\b", re.I),
    re.compile(r"\bwill\s+sponsor\b", re.I),
)
_WORK_AUTH_CATEGORY_PATTERNS = (
    ("citizen_only", re.compile(r"\b(us\s+citizens?\s+only|citizen\s+only)\b", re.I)),
    ("gc_or_citizen", re.compile(r"\b(green\s+card|permanent\s+resident|citizen)\b", re.I)),
    ("opt_only", re.compile(r"\bopt\b", re.I)),
    ("opt_cpt", re.compile(r"\b(opt|cpt)\b", re.I)),
    ("h1b_transfer", re.compile(r"\bh-?1b\b", re.I)),
    ("sponsorship_available", re.compile(r"\b(sponsorship available|will sponsor|can sponsor)\b", re.I)),
    ("authorized_no_sponsorship", re.compile(r"\b(authorized to work|work authorization required|no sponsorship)\b", re.I)),
)
_CONTRACT_PATTERNS = {
    "W2 only": re.compile(r"\bw2\s+only\b", re.I),
    "No C2C": re.compile(r"\bno\s+c2c\b", re.I),
    "No third party": re.compile(r"\bno\s+third[- ]party\b", re.I),
    "1099 allowed": re.compile(r"\b1099\b", re.I),
}
_CLEARANCE_PATTERNS = (
    re.compile(r"\bsecurity clearance\b", re.I),
    re.compile(r"\bsecret clearance\b", re.I),
    re.compile(r"\btop secret\b", re.I),
    re.compile(r"\bpublic trust\b", re.I),
)
_SENIORITY_ORDER = [
    "intern",
    "graduate",
    "entry",
    "junior",
    "mid",
    "senior",
    "lead",
    "manager",
    "director",
    "unknown",
]
_YEARS_PATTERNS = (
    re.compile(r"\b(\d{1,2})\s*\+\s*years?\b", re.I),
    re.compile(r"\b(\d{1,2})\s*-\s*(\d{1,2})\s*years?\b", re.I),
    re.compile(r"\b(\d{1,2})\s*to\s*(\d{1,2})\s*years?\b", re.I),
    re.compile(r"\bminimum\s+of\s+(\d{1,2})\s+years?\b", re.I),
)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _dedupe_strings(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = _normalize_text(str(value))
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _country_codes(values: list[Any]) -> list[str]:
    codes: list[str] = []
    for value in values or []:
        text = _normalize_text(str(value)).upper()
        if len(text) == 2 and text.isalpha() and text not in codes:
            codes.append(text)
    return codes


def _country_labels(codes: list[str]) -> list[str]:
    return [COUNTRY_CODE_TO_NAME.get(code, code) for code in codes]


def _infer_country_codes(job, raw_effective: dict[str, Any], location_text: str) -> list[str]:
    if raw_effective.get("country_codes"):
        return _country_codes(raw_effective.get("country_codes"))
    if job.country:
        code = _code_for_country(job.country)
        if code:
            return [code]
    inferred_country = infer_country_from_location(location_text or job.location or "") or ""
    inferred_code = _code_for_country(inferred_country)
    return [inferred_code] if inferred_code else []


def _infer_country_mode(location_text: str, country_codes: list[str], description: str) -> str:
    text = f"{location_text}\n{description}".lower()
    if any(pattern.search(text) for pattern in _REMOTE_PATTERNS) and re.search(r"\b(global|worldwide|anywhere)\b", text):
        return "remote_global"
    if re.search(r"\b(emea|apac|latam|europe|worldwide|global)\b", text):
        return "regional"
    if len(country_codes) > 1:
        return "multi"
    if len(country_codes) == 1:
        return "single"
    return "unknown"


def _infer_work_mode(location_text: str, description: str, raw_effective: dict[str, Any]) -> str:
    raw_location_type = str(raw_effective.get("location_type") or "").upper()
    if raw_location_type == "REMOTE":
        return "remote"
    if raw_location_type == "HYBRID":
        return "hybrid"
    if raw_location_type in {"ONSITE", "ONSITE"}:
        return "onsite"
    text = f"{location_text}\n{description}"
    if any(pattern.search(text) for pattern in _REMOTE_PATTERNS):
        return "remote"
    if any(pattern.search(text) for pattern in _HYBRID_PATTERNS):
        return "hybrid"
    if any(pattern.search(text) for pattern in _ONSITE_PATTERNS):
        return "onsite"
    return "unknown"


def _infer_visa_sponsorship(raw_effective: dict[str, Any], description: str, work_auth: str) -> bool | None:
    if raw_effective.get("visa_sponsorship") is not None:
        return bool(raw_effective.get("visa_sponsorship"))
    text = f"{description}\n{work_auth}"
    if any(pattern.search(text) for pattern in _NO_SPONSOR_PATTERNS):
        return False
    if any(pattern.search(text) for pattern in _YES_SPONSOR_PATTERNS):
        return True
    return None


def _infer_work_authorization(parsed: dict[str, Any], raw_effective: dict[str, Any], description: str) -> str:
    explicit = _normalize_text(str(raw_effective.get("work_authorization") or ""))
    if explicit:
        return explicit
    for item in (parsed.get("special_resume_requirements") or []):
        if str(item.get("requirement") or "").lower() == "work_authorization":
            return _normalize_text(str(item.get("evidence_text") or ""))
    for item in ((parsed.get("requirements") or {}).get("screen_out_requirements") or []):
        if str(item.get("category") or "").lower() == "work_authorization":
            return _normalize_text(str(item.get("requirement") or item.get("evidence_text") or ""))
    match = re.search(r"\b(must be authorized to work[^.]*|us work authorization[^.]*|citizen[^.]*|green card[^.]*|opt[^.]*)", description, re.I)
    return _normalize_text(match.group(1)) if match else ""


def _infer_contract_constraints(description: str) -> list[str]:
    return [label for label, pattern in _CONTRACT_PATTERNS.items() if pattern.search(description or "")]


def _infer_work_auth_category(work_auth: str, visa_sponsorship: bool | None, description: str) -> str:
    text = "\n".join([work_auth or "", description or ""])
    for label, pattern in _WORK_AUTH_CATEGORY_PATTERNS:
        if pattern.search(text):
            return label
    if visa_sponsorship is True:
        return "sponsorship_available"
    if visa_sponsorship is False:
        return "authorized_no_sponsorship"
    return "unknown"


def _employment_terms(employment_type: str, constraints: list[str]) -> list[str]:
    out: list[str] = []
    lowered = _normalize_text(employment_type).replace("-", "_").replace(" ", "_")
    if lowered in {"full_time", "fulltime", "permanent"}:
        out.append("full_time")
    elif lowered:
        out.append(lowered)
    for value in constraints or []:
        norm = _normalize_text(str(value)).lower()
        if norm == "w2 only":
            out.extend(["w2_only", "w2"])
        elif norm == "no c2c":
            out.append("no_c2c")
        elif norm == "no third party":
            out.append("no_third_party")
        elif norm == "1099 allowed":
            out.extend(["1099_allowed", "1099"])
    return _dedupe_strings(out)


def _infer_clearance(parsed: dict[str, Any], raw_effective: dict[str, Any], description: str) -> tuple[bool, str]:
    if raw_effective.get("clearance_required") is not None:
        required = bool(raw_effective.get("clearance_required"))
        return required, _normalize_text(str(raw_effective.get("clearance_level") or ""))
    for item in ((parsed.get("requirements") or {}).get("screen_out_requirements") or []):
        if str(item.get("category") or "").lower() == "clearance":
            return True, _normalize_text(str(item.get("requirement") or ""))
    if any(pattern.search(description or "") for pattern in _CLEARANCE_PATTERNS):
        return True, ""
    return False, ""


def _infer_years(parsed: dict[str, Any], raw_effective: dict[str, Any], description: str) -> tuple[int | None, int | None]:
    role = parsed.get("role_classification") or {}
    years_min = _coerce_int((parsed.get("routing_profile") or {}).get("years_min"))
    years_max = _coerce_int((parsed.get("routing_profile") or {}).get("years_max"))
    if years_min is None:
        years_min = _coerce_int(role.get("required_years"))
    if years_min is None:
        years_min = _coerce_int(raw_effective.get("years_required"))
    if years_max is None:
        years_max = _coerce_int(raw_effective.get("years_required_max"))
    if years_min is not None:
        return years_min, years_max
    for pattern in _YEARS_PATTERNS:
        match = pattern.search(description or "")
        if not match:
            continue
        if len(match.groups()) == 1:
            value = _coerce_int(match.group(1))
            return value, None
        low = _coerce_int(match.group(1))
        high = _coerce_int(match.group(2))
        return low, high
    return None, None


def _normalize_seniority(value: Any) -> str:
    text = _normalize_text(str(value or "")).lower()
    if text in _SENIORITY_ORDER:
        return text
    if text.startswith("sr"):
        return "senior"
    return "unknown" if not text else text


def _secondary_seniority(title: str) -> str:
    low = (title or "").lower()
    if "staff" in low and "principal" in low:
        return "principal"
    if "senior" in low and "lead" in low:
        return "lead"
    return ""


def _warnings_from_parsed(
    parsed: dict[str, Any],
    country_mode: str,
    confidence: float,
    description: str,
    ready_threshold: float,
) -> list[str]:
    warnings = [
        _normalize_text(str(item.get("message") or ""))
        for item in ((parsed.get("extraction_quality") or {}).get("extraction_warnings") or [])
        if _normalize_text(str(item.get("message") or ""))
    ]
    if country_mode in {"regional", "multi"}:
        warnings.append("Multi-country or regional location detected.")
    if len((description or "").split()) < 60:
        warnings.append("JD is short; routing confidence should be treated cautiously.")
    if confidence < ready_threshold:
        warnings.append("Routing confidence is below the auto-route threshold.")
    return _dedupe_strings(warnings)


def _evidence_spans(parsed: dict[str, Any], work_auth: str, clearance_level: str) -> list[str]:
    evidence: list[str] = []
    for item in ((parsed.get("requirements") or {}).get("screen_out_requirements") or []):
        text = _normalize_text(str(item.get("evidence_text") or item.get("requirement") or ""))
        if text:
            evidence.append(text)
    for item in ((parsed.get("special_resume_requirements") or [])):
        text = _normalize_text(str(item.get("evidence_text") or ""))
        if text:
            evidence.append(text)
    if work_auth:
        evidence.append(work_auth)
    if clearance_level:
        evidence.append(clearance_level)
    return _dedupe_strings(evidence)[:8]


def build_routing_profile(job, parsed_jd: dict[str, Any] | None = None) -> dict[str, Any]:
    from core.models import PlatformConfig

    cfg = PlatformConfig.load()
    parsed = parsed_jd if isinstance(parsed_jd, dict) else (getattr(job, "parsed_jd", None) or {})
    routing = parsed.get("routing_profile") if isinstance(parsed.get("routing_profile"), dict) else {}
    role = parsed.get("role_classification") if isinstance(parsed.get("role_classification"), dict) else {}
    quality = parsed.get("extraction_quality") if isinstance(parsed.get("extraction_quality"), dict) else {}
    parser_meta = parsed.get("parser_metadata") if isinstance(parsed.get("parser_metadata"), dict) else {}

    raw_effective = effective_raw_job_classification(job.source_raw_job) if getattr(job, "source_raw_job_id", None) else {}
    description = getattr(job, "description", "") or ""
    location_text = _normalize_text(
        str(
            routing.get("location_text")
            or (parsed.get("job_metadata") or {}).get("location")
            or getattr(job, "location", "")
            or raw_effective.get("country")
            or ""
        )
    )
    country_codes = _country_codes(
        routing.get("country_codes")
        or raw_effective.get("country_codes")
        or _infer_country_codes(job, raw_effective, location_text)
    )
    country_mode = _normalize_text(str(routing.get("country_mode") or "")) or _infer_country_mode(location_text, country_codes, description)
    work_mode = _normalize_text(str(routing.get("work_mode") or "")) or _infer_work_mode(location_text, description, raw_effective)
    work_auth = _infer_work_authorization(parsed, raw_effective, description)
    visa_sponsorship = routing.get("visa_sponsorship")
    if visa_sponsorship is None:
        visa_sponsorship = _infer_visa_sponsorship(raw_effective, description, work_auth)
    clearance_required, clearance_level = _infer_clearance(parsed, raw_effective, description)
    years_min, years_max = _infer_years(parsed, raw_effective, description)

    confidence = _coerce_float(
        routing.get("confidence"),
        _coerce_float(quality.get("overall_extraction_confidence"), 0.0),
    )
    ready_threshold = _coerce_float(getattr(cfg, "routing_ready_confidence_threshold", 0.55), 0.55)
    warnings = _warnings_from_parsed(parsed, country_mode, confidence, description, ready_threshold)
    missing_readiness_signals: list[str] = []
    if getattr(cfg, "routing_require_country", True) and not country_codes:
        missing_readiness_signals.append("country")
        warnings.append("Country is missing; routing policy requires it before READY.")
    if getattr(cfg, "routing_require_seniority", False) and _normalize_seniority(routing.get("seniority_primary") or role.get("seniority")) == "unknown":
        missing_readiness_signals.append("seniority")
        warnings.append("Seniority is missing; routing policy requires it before READY.")
    if getattr(cfg, "routing_require_work_authorization", False) and not work_auth:
        missing_readiness_signals.append("work_authorization")
        warnings.append("Work authorization evidence is missing; routing policy requires it before READY.")
    needs_review = (
        bool(quality.get("needs_human_review"))
        or confidence < ready_threshold
        or not description.strip()
        or bool(missing_readiness_signals)
    )
    status = getattr(job, "routing_status", "") or ""
    if getattr(job, "routing_override", None):
        status = job.RoutingStatus.OVERRIDDEN
    elif not description.strip():
        status = job.RoutingStatus.FAILED
    elif needs_review:
        status = job.RoutingStatus.REVIEW
    else:
        status = job.RoutingStatus.READY

    profile = {
        "role_family": _normalize_text(str(routing.get("role_family") or role.get("primary_role_family") or "")),
        "secondary_role_families": routing.get("secondary_role_families") or role.get("secondary_role_families") or [],
        "seniority_primary": _normalize_seniority(routing.get("seniority_primary") or role.get("seniority")),
        "seniority_secondary": _normalize_text(str(routing.get("seniority_secondary") or _secondary_seniority(job.title))),
        "seniority_confidence": _coerce_float(routing.get("seniority_confidence"), confidence),
        "years_min": years_min,
        "years_max": years_max,
        "country_mode": country_mode,
        "country_codes": country_codes,
        "country_labels": _country_labels(country_codes),
        "location_text": location_text,
        "work_mode": work_mode,
        "visa_sponsorship": visa_sponsorship,
        "work_authorization": work_auth,
        "work_auth_category": _infer_work_auth_category(work_auth, visa_sponsorship, description),
        "employment_type": _normalize_text(str(routing.get("employment_type") or getattr(job, "job_type", ""))),
        "contract_constraints": _dedupe_strings(list(routing.get("contract_constraints") or []) + _infer_contract_constraints(description)),
        "clearance_required": clearance_required,
        "clearance_level": clearance_level,
        "warnings": _dedupe_strings(warnings),
        "evidence_spans": _evidence_spans(parsed, work_auth, clearance_level),
        "confidence": confidence,
        "needs_review": needs_review,
        "status": status,
        "readiness_threshold": ready_threshold,
        "missing_readiness_signals": missing_readiness_signals,
        "source": "routing_profile" if routing else ("raw_job" if raw_effective else "parsed_jd"),
        "extractor_model": _normalize_text(str(parser_meta.get("extractor_model") or getattr(job, "parsed_jd_model", "") or "")),
        "prompt_version": _normalize_text(str(parser_meta.get("extractor_prompt_version") or getattr(job, "parsed_jd_prompt_version", "") or "")),
        "schema_version": _normalize_text(str(parser_meta.get("extractor_schema_version") or getattr(job, "parsed_jd_schema_version", "") or "")),
        "hash": _normalize_text(str(parser_meta.get("jd_text_hash") or getattr(job, "parsed_jd_hash", "") or "")),
    }
    if getattr(job, "routing_override", None):
        profile = apply_routing_override(profile, job.routing_override)
        profile["status"] = job.RoutingStatus.OVERRIDDEN
    profile["employment_terms"] = _employment_terms(profile.get("employment_type") or "", profile.get("contract_constraints") or [])
    return profile


def apply_routing_override(profile: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(override, dict) or not override:
        return dict(profile)
    out = dict(profile)
    for key, value in override.items():
        if value in (None, "", []):
            continue
        out[key] = value
    out["source"] = "manual_override"
    return out


def persist_routing_profile(job, parsed_jd: dict[str, Any] | None = None, *, save: bool = True) -> dict[str, Any]:
    profile = build_routing_profile(job, parsed_jd=parsed_jd)
    job.routing_profile = profile
    job.routing_status = profile.get("status") or job.RoutingStatus.PENDING
    job.routing_confidence = _coerce_float(profile.get("confidence"), 0.0)
    job.routing_source = _normalize_text(str(profile.get("source") or ""))[:32]
    job.routing_role_family = _normalize_text(str(profile.get("role_family") or ""))[:80]
    job.routing_seniority = _normalize_seniority(profile.get("seniority_primary"))[:20]
    job.routing_years_min = _coerce_int(profile.get("years_min"))
    job.routing_years_max = _coerce_int(profile.get("years_max"))
    job.routing_country_mode = _normalize_text(str(profile.get("country_mode") or ""))[:20]
    job.routing_country_codes = _country_codes(profile.get("country_codes") or [])
    job.routing_work_mode = _normalize_text(str(profile.get("work_mode") or ""))[:20]
    job.routing_visa_sponsorship = profile.get("visa_sponsorship")
    job.routing_work_authorization = _normalize_text(str(profile.get("work_authorization") or ""))[:160]
    job.routing_work_auth_category = _normalize_text(str(profile.get("work_auth_category") or ""))[:40]
    job.routing_employment_terms = _dedupe_strings(profile.get("employment_terms") or [])
    job.routing_clearance_required = bool(profile.get("clearance_required"))
    job.routing_warnings = _dedupe_strings(profile.get("warnings") or [])
    job.routing_hash = _normalize_text(str(profile.get("hash") or ""))[:64]
    job.routing_model = _normalize_text(str(profile.get("extractor_model") or ""))[:100]
    job.routing_prompt_version = _normalize_text(str(profile.get("prompt_version") or ""))[:40]
    job.routing_schema_version = _normalize_text(str(profile.get("schema_version") or ""))[:40]
    job.routing_extracted_at = timezone.now()
    if save:
        job.save(
            update_fields=[
                "routing_profile",
                "routing_status",
                "routing_confidence",
                "routing_source",
                "routing_role_family",
                "routing_seniority",
                "routing_years_min",
                "routing_years_max",
                "routing_country_mode",
                "routing_country_codes",
                "routing_work_mode",
                "routing_visa_sponsorship",
                "routing_work_authorization",
                "routing_work_auth_category",
                "routing_employment_terms",
                "routing_clearance_required",
                "routing_warnings",
                "routing_hash",
                "routing_model",
                "routing_prompt_version",
                "routing_schema_version",
                "routing_extracted_at",
            ]
        )
    return profile


def effective_routing_profile(job) -> dict[str, Any]:
    if getattr(job, "routing_profile", None):
        profile = dict(job.routing_profile)
        if getattr(job, "routing_override", None):
            profile = apply_routing_override(profile, job.routing_override)
            profile["status"] = job.RoutingStatus.OVERRIDDEN
        return profile
    return build_routing_profile(job)

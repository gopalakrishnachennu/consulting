from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.core.cache import cache
from django.db.models import Count, Sum
from django.utils import timezone

from jobs.classifier import country as country_classifier

from .models import HarvestEngineConfig, LocationCache, RawJob


logger = logging.getLogger(__name__)


DEFAULT_TARGET_COUNTRIES = ["US", "IN", "CA", "GB", "AU"]

COUNTRY_NAME_TO_CODE = {
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "us": "US",
    "america": "US",
    "india": "IN",
    "bharat": "IN",
    "united kingdom": "GB",
    "uk": "GB",
    "u.k.": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "great britain": "GB",
    "australia": "AU",
    "canada": "CA",
    "germany": "DE",
    "france": "FR",
    "netherlands": "NL",
    "ireland": "IE",
    "singapore": "SG",
    "new zealand": "NZ",
    "brazil": "BR",
    "mexico": "MX",
    "poland": "PL",
    "sweden": "SE",
    "switzerland": "CH",
    "united arab emirates": "AE",
    "uae": "AE",
}

COUNTRY_CODE_TO_NAME = {
    "US": "United States",
    "IN": "India",
    "GB": "United Kingdom",
    "AU": "Australia",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "NL": "Netherlands",
    "IE": "Ireland",
    "SG": "Singapore",
    "NZ": "New Zealand",
    "BR": "Brazil",
    "MX": "Mexico",
    "PL": "Poland",
    "SE": "Sweden",
    "CH": "Switzerland",
    "AE": "United Arab Emirates",
}
_KNOWN_COUNTRY_CODES = set(COUNTRY_CODE_TO_NAME)

_PLACEHOLDER_LOCATION_VALUES = {
    "remote",
    "hybrid",
    "hybrid remote",
    "remote hybrid",
    "onsite",
    "on site",
    "on-site",
    "multiple locations",
    "various locations",
    "various",
    "global",
    "worldwide",
    "anywhere",
    "not specified",
    "unspecified",
    "n a",
    "na",
    "n/a",
    "emea",
    "apac",
    "europe",
}
_LOCATION_COUNT_RE = re.compile(r"^\d+\s+locations?$", re.I)
_LOCATION_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:job\s+)?(?:work\s+)?(?:primary\s+)?(?:office|location|worksite|site)\s*[-:]\s*",
    re.I,
)
# Trailing office/HQ/campus labels that ATS feeds bolt onto a city name, e.g.
# "San Francisco-HQ", "NYC Global HQ", "Madrid Office", "Pune Research Campus".
_LOCATION_LABEL_SUFFIX_RE = re.compile(
    r"\s*[-:,]?\s*(?:global\s+|corporate\s+|regional\s+|main\s+)?"
    r"(?:office|location|worksite|site|hq|headquarters|campus|research\s+campus)\s*$",
    re.I,
)

TARGET_DOMAIN_SLUGS = {
    # IT
    "software-developer",
    "backend-developer",
    "frontend-developer",
    "full-stack-developer",
    "mobile-developer",
    "ml-ai-engineer",
    "data-engineer",
    "data-analyst",
    "data-scientist",
    "devops-engineer",
    "cloud-engineer",
    "sre-platform-engineer",
    "security-engineer",
    "cybersecurity-engineer",
    "qa-test-engineer",
    "servicenow-developer",
    "servicenow-admin",
    "salesforce-developer",
    "sap-consultant",
    "oracle-consultant",
    "workday-consultant",
    "it-support-helpdesk",
    "network-systems-engineer",
    "systems-administrator",
    "database-administrator",
    "business-analyst-it",
    "systems-analyst",
    "it-project-manager",
    "scrum-master-agile-coach",
    "product-manager",
    "general-it",
    # Non-IT engineering
    "civil-engineer",
    "mechanical-engineer",
    "electrical-engineer",
    "structural-engineer",
    "manufacturing-engineer",
    "embedded-systems-engineer",
    "general-engineering",
}


@dataclass(frozen=True)
class LocationResolution:
    raw_text: str
    normalized_text: str
    country_code: str = ""
    country_name: str = ""
    region_code: str = ""
    region_name: str = ""
    city: str = ""
    confidence: float = 0.0
    source: str = "unknown"
    status: str = LocationCache.Status.UNKNOWN
    provider: str = ""
    provider_place_id: str = ""


def normalize_location_text(*parts: str) -> str:
    text = " ".join(str(part or "").strip() for part in parts if str(part or "").strip())
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\b(remote|hybrid|onsite|on-site)\b", " ", text, flags=re.I)
    text = re.sub(r"[\|\u2022;]+", ",", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,").lower()
    return text[:512]


def _location_token(value: str) -> str:
    token = re.sub(r"<[^>]+>", " ", str(value or ""))
    token = token.replace("&nbsp;", " ")
    token = re.sub(r"[^a-z0-9]+", " ", token.lower())
    return re.sub(r"\s+", " ", token).strip()


def is_placeholder_location_value(value: str) -> bool:
    """True for location placeholders that are not geocodable places."""
    token = _location_token(value)
    if not token:
        return False
    if token in _PLACEHOLDER_LOCATION_VALUES:
        return True
    if _LOCATION_COUNT_RE.match(token):
        return True
    if "locations" in token and any(word in token for word in ("remote", "hybrid", "multiple", "various")):
        return True
    return False


def _is_ambiguous_location_only(*parts: str) -> bool:
    values = [str(part or "").strip() for part in parts if str(part or "").strip()]
    return bool(values) and all(is_placeholder_location_value(value) for value in values)


def _safe_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _strip_location_label_noise(value: str) -> str:
    """Remove ATS office/location labels without treating them as countries."""
    text = re.sub(r"\s+", " ", _safe_text(value)).strip(" ,;|")
    if not text:
        return ""
    text = _LOCATION_LABEL_PREFIX_RE.sub("", text).strip(" ,;-")
    stripped = _LOCATION_LABEL_SUFFIX_RE.sub("", text).strip(" ,;-")
    return stripped if stripped else text


def _dedupe_append(values: list[str], value: str) -> None:
    value = re.sub(r"\s+", " ", _safe_text(value)).strip(" ,;|")
    if not value or is_placeholder_location_value(value):
        return
    key = _location_token(value)
    if not key or key in {_location_token(v) for v in values}:
        return
    values.append(value[:255])


def _candidate_has_geo_signal(value: str) -> bool:
    text = _safe_text(value)
    if not text or is_placeholder_location_value(text):
        return False
    cleaned = _strip_location_label_noise(text)
    low = text.lower()
    if any(name in low for name in COUNTRY_NAME_TO_CODE):
        return True
    if _state_prefix_parts(text):
        return True
    if cleaned != text and _state_prefix_parts(cleaned):
        return True
    parts = _split_location_parts(text)
    if len(parts) >= 2:
        return True
    first = parts[0] if parts else text
    if _city_country_code(first):
        return True
    if cleaned != text and _city_country_code(cleaned):
        return True
    return False


def split_multi_location_text(text: str) -> list[str]:
    """Split vendor/detail multi-location strings into geocodable candidates."""
    text = re.sub(r"<[^>]+>", " ", _safe_text(text))
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    candidates: list[str] = []
    # Most ATS detail pages separate locations with bullets/pipes/semicolons.
    chunks = [p.strip(" ,") for p in re.split(r"\s*(?:\||\u2022|·|;|\n)\s*", text) if p.strip(" ,")]
    if len(chunks) > 1:
        for chunk in chunks:
            subparts = [p.strip() for p in chunk.split(",") if p.strip()]
            while subparts and is_placeholder_location_value(subparts[0]):
                subparts.pop(0)
            cleaned = ", ".join(subparts)
            if _candidate_has_geo_signal(cleaned):
                _dedupe_append(candidates, cleaned)
        if candidates:
            return candidates

    # Handle strings like "Hybrid Remote, Seattle, Washington, Los Angeles, California".
    comma_parts = [p.strip() for p in text.split(",") if p.strip()]
    while comma_parts and is_placeholder_location_value(comma_parts[0]):
        comma_parts.pop(0)
    if len(comma_parts) >= 4:
        idx = 0
        while idx < len(comma_parts):
            group = comma_parts[idx:idx + 3]
            if len(group) >= 3 and _code_for_country(group[2]):
                candidate = ", ".join(group)
                idx += 3
            else:
                group = comma_parts[idx:idx + 2]
                candidate = ", ".join(group)
                idx += 2
            if _candidate_has_geo_signal(candidate):
                _dedupe_append(candidates, candidate)
        if candidates:
            return candidates

    if _candidate_has_geo_signal(text):
        _dedupe_append(candidates, text)
    return candidates


def _location_from_mapping(loc: dict) -> str:
    if not isinstance(loc, dict):
        return _safe_text(loc)
    nested = loc.get("address") or loc.get("Address") or loc.get("location") or loc.get("Location")
    if isinstance(nested, dict):
        nested_text = _location_from_mapping(nested)
        if nested_text:
            return nested_text

    name = (
        loc.get("locationName")
        or loc.get("name")
        or loc.get("LocalizedName")
        or loc.get("LocalizedLocation")
        or loc.get("descriptor")
        or loc.get("Description")
        or ""
    )
    if _candidate_has_geo_signal(_safe_text(name)):
        return _safe_text(name)

    city = (
        loc.get("city")
        or loc.get("City")
        or loc.get("cityName")
        or loc.get("addressLocality")
        or loc.get("TownOrCity")
        or loc.get("AddressCity")
        or ""
    )
    state = (
        loc.get("state")
        or loc.get("State")
        or loc.get("region")
        or loc.get("Region2")
        or loc.get("stateCode")
        or loc.get("addressRegion")
        or loc.get("AddressState")
        or ""
    )
    country = (
        loc.get("country")
        or loc.get("Country")
        or loc.get("countryCode")
        or loc.get("isoCountryCode")
        or loc.get("addressCountry")
        or loc.get("AddressCountry")
        or ""
    )
    return ", ".join(_safe_text(part) for part in (city, state, country) if _safe_text(part))


_LOCATION_PAYLOAD_KEYS = {
    "postinglocations",
    "locations",
    "secondarylocations",
    "offices",
    "worklocation",
    "joblocation",
    "location",
    "address",
    "primarylocation",
    "primaryworklocation",
    "locationaddress",
    "locationname",
    "locationtext",
    "locationstext",
}


def _looks_like_location_payload_key(key: str) -> bool:
    token = re.sub(r"[^a-z]", "", (key or "").lower())
    if not token or "relocation" in token:
        return False
    if token in _LOCATION_PAYLOAD_KEYS:
        return True
    return any(marker in token for marker in ("location", "address"))


def _payload_location_values(payload, *, _depth: int = 0, _inside_location_key: bool = False) -> list[str]:
    values: list[str] = []
    if _depth > 5:
        return values
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list)) or _inside_location_key:
                values.extend(
                    _payload_location_values(
                        item,
                        _depth=_depth + 1,
                        _inside_location_key=_inside_location_key,
                    )
                )
        return values
    if not isinstance(payload, dict):
        text = _safe_text(payload)
        if text and _inside_location_key:
            values.extend(split_multi_location_text(text))
        return values

    for key, raw in payload.items():
        is_location_key = _looks_like_location_payload_key(str(key))
        if is_location_key:
            items = raw if isinstance(raw, list) else ([raw] if raw else [])
            for item in items:
                text = _location_from_mapping(item)
                if text:
                    values.extend(split_multi_location_text(text))
                if isinstance(item, (dict, list)):
                    values.extend(_payload_location_values(item, _depth=_depth + 1, _inside_location_key=True))
        elif isinstance(raw, (dict, list)):
            values.extend(_payload_location_values(raw, _depth=_depth + 1))
    return values


def extract_location_candidates(
    *,
    location_raw: str = "",
    city: str = "",
    state: str = "",
    country: str = "",
    vendor_location_block: str = "",
    raw_payload=None,
) -> list[str]:
    candidates: list[str] = []
    structured = ", ".join(_safe_text(part) for part in (city, state, country) if _safe_text(part))
    for source in (
        *_payload_location_values(raw_payload),
        structured,
        vendor_location_block,
        location_raw,
    ):
        for candidate in split_multi_location_text(source):
            _dedupe_append(candidates, candidate)
    return candidates


def _code_for_country(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    upper = value.upper()
    if len(upper) == 2 and upper.isalpha():
        if upper in _KNOWN_COUNTRY_CODES:
            return upper
        # Do not blindly accept every two-letter token as a country code.
        # State/province abbreviations like WA/ON otherwise become fake
        # countries before the city/state resolver can disambiguate them.
        # Full country names still flow through country_converter below.
        return ""
    known = COUNTRY_NAME_TO_CODE.get(value.lower(), "")
    if known:
        return known
    # Skip obviously-not-country strings before calling coco — saves both noise
    # and CPU. coco prints "X not found in regex" to stderr for every miss.
    if (
        len(value) > 40
        or any(ch.isdigit() for ch in value)
        or any(ch in value for ch in "()[]{}/\\@#$%&*+=<>")
    ):
        return ""
    try:
        # Silence coco's stderr logging globally on first call.
        import country_converter as coco  # type: ignore
        import logging as _logging
        _coco_logger = _logging.getLogger("country_converter")
        if _coco_logger.level < _logging.CRITICAL:
            _coco_logger.setLevel(_logging.CRITICAL)
        result = coco.convert(names=[value], to="ISO2", not_found=None)
        if isinstance(result, list):
            result = result[0] if result else None
        if result and result != "not found":
            code = str(result).upper()
            if len(code) == 2:
                return code
    except Exception:
        pass
    return ""


def _split_location_parts(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,|/]+", text or "") if part.strip()]


def _city_country_code(city: str) -> str:
    city_map = getattr(country_classifier, "_CITY_COUNTRY", {})
    fold = getattr(country_classifier, "_fold_accents", lambda s: s)
    candidates = [
        (city or "").lower().strip(),
        _strip_location_label_noise(city).lower().strip(),
    ]
    # Also try accent-folded variants (São Paulo → sao paulo) against the
    # expanded global gazetteer.
    candidates += [fold(c) for c in list(candidates)]
    for key in candidates:
        country = city_map.get(key, "")
        if country:
            return _code_for_country(country)
    return ""


def _state_prefix_parts(text: str) -> tuple[str, str, str] | None:
    """Parse ATS strings like 'PA - Duquesne' or 'ON - Toronto'."""
    match = re.match(r"^\s*([A-Za-z]{2})\s*[-–]\s*(.+?)\s*$", text or "")
    if not match:
        return None
    region_code = match.group(1).upper()
    city = match.group(2).strip(" ,")
    if not city:
        return None
    us_states = getattr(country_classifier, "_US_STATES", set())
    ca_provinces = getattr(country_classifier, "_CA_PROVINCES", set())
    if region_code in ca_provinces:
        return "CA", region_code, city
    if region_code in us_states:
        return "US", region_code, city
    return None


def _has_region_hint(text: str) -> bool:
    parts = _split_location_parts(text)
    if len(parts) < 2:
        return False
    us_states = getattr(country_classifier, "_US_STATES", set())
    ca_provinces = getattr(country_classifier, "_CA_PROVINCES", set())
    for part in parts[1:]:
        token = re.sub(r"[^A-Za-z]", "", part).upper()
        if token in us_states or token in ca_provinces:
            return True
    return bool(_state_prefix_parts(text))


def _should_prefer_location_resolution(
    location_resolution: LocationResolution | None,
    explicit_resolution: LocationResolution | None,
    raw_text: str,
) -> bool:
    if not location_resolution:
        return False
    if not explicit_resolution:
        return True
    if location_resolution.country_code == explicit_resolution.country_code:
        return False
    # A concrete city+state/province signal is stronger than a previously
    # stored country field. This repairs rows like "Mountain View, CA" that
    # were once mis-saved as Canada when CA was treated as a country.
    return location_resolution.source == "state_region" or _has_region_hint(raw_text)


def _resolve_from_explicit_country(country: str, raw_text: str, normalized: str) -> LocationResolution | None:
    code = _code_for_country(country)
    if not code:
        return None
    return LocationResolution(
        raw_text=raw_text,
        normalized_text=normalized,
        country_code=code,
        country_name=COUNTRY_CODE_TO_NAME.get(code, country),
        confidence=0.98,
        source="ats_country",
        status=LocationCache.Status.RESOLVED,
    )


def _resolve_from_state_city(raw_text: str, normalized: str) -> LocationResolution | None:
    raw_text = _safe_text(raw_text)
    cleaned_text = _strip_location_label_noise(raw_text)
    parts = _split_location_parts(raw_text)
    if not parts:
        return None

    # Single-token location that is itself a country name ("United States", "India", etc.)
    if len(parts) == 1:
        code = _code_for_country(raw_text.strip())
        if code:
            return LocationResolution(
                raw_text=raw_text,
                normalized_text=normalized,
                country_code=code,
                country_name=COUNTRY_CODE_TO_NAME.get(code, raw_text.strip()),
                confidence=0.87,
                source="country_name_only",
                status=LocationCache.Status.RESOLVED,
            )

    if prefixed := _state_prefix_parts(raw_text):
        country_code, region_code, city = prefixed
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            country_code=country_code,
            country_name=COUNTRY_CODE_TO_NAME.get(country_code, ""),
            region_code=region_code,
            city=city,
            confidence=0.92,
            source="state_region",
            status=LocationCache.Status.RESOLVED,
        )
    if cleaned_text != raw_text and (prefixed := _state_prefix_parts(cleaned_text)):
        country_code, region_code, city = prefixed
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            country_code=country_code,
            country_name=COUNTRY_CODE_TO_NAME.get(country_code, ""),
            region_code=region_code,
            city=city,
            confidence=0.9,
            source="state_region",
            status=LocationCache.Status.RESOLVED,
        )

    city = parts[0]
    city_code = _city_country_code(city)
    if not city_code and cleaned_text != raw_text:
        city_code = _city_country_code(cleaned_text)
        if city_code:
            city = cleaned_text
    region_code = ""
    for part in reversed(parts[1:]):
        token = re.sub(r"[^A-Za-z]", "", part).upper()
        if len(token) == 2:
            region_code = token
            break

    us_states = getattr(country_classifier, "_US_STATES", set())
    ca_provinces = getattr(country_classifier, "_CA_PROVINCES", set())

    if city_code and region_code == "CA" and city_code == "CA":
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            country_code="CA",
            country_name="Canada",
            region_code="",
            city=city,
            confidence=0.93,
            source="city_dict",
            status=LocationCache.Status.RESOLVED,
        )

    if region_code in ca_provinces:
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            country_code="CA",
            country_name="Canada",
            region_code=region_code,
            city=city,
            confidence=0.94,
            source="state_region",
            status=LocationCache.Status.RESOLVED,
        )

    if region_code in us_states:
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            country_code="US",
            country_name="United States",
            region_code=region_code,
            city=city,
            confidence=0.94,
            source="state_region",
            status=LocationCache.Status.RESOLVED,
        )

    if city_code:
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            country_code=city_code,
            country_name=COUNTRY_CODE_TO_NAME.get(city_code, ""),
            city=city,
            confidence=0.9,
            source="city_dict",
            status=LocationCache.Status.RESOLVED,
        )

    return None


def _looks_remote(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t)
    pat = getattr(country_classifier, "_REMOTE_PAT", None)
    if pat is not None:
        try:
            return bool(pat.search(blob))
        except Exception:
            pass
    return bool(re.search(r"\b(remote|wfh|work\s*from\s*home|anywhere|distributed)\b", blob, re.I))


def _resolve_remote(
    location_raw: str, city: str, state: str, country: str,
    normalized: str, title: str = "", description: str = "",
) -> LocationResolution | None:
    """For remote postings, pull a country clue from the location, title, or JD
    ('Remote - US', 'US-based remote', visa/right-to-work hints). Only fires when
    the posting actually signals remote, so it can't mis-tag normal jobs.
    Returns None when no real country can be found (bare 'Remote')."""
    blob = " ".join(p for p in (location_raw, city, state, country) if (p or "").strip())
    if not _looks_remote(blob, title):
        return None
    country_name, _region = country_classifier.detect_country(blob, title=title, description=description)
    code = _code_for_country(country_name)
    if not code:
        return None
    return LocationResolution(
        raw_text=blob or location_raw,
        normalized_text=(normalized or _location_token(blob)[:512] or "remote"),
        country_code=code,
        country_name=COUNTRY_CODE_TO_NAME.get(code, country_name),
        region_name="Remote",
        confidence=0.8,
        source="remote_rules",
        status=LocationCache.Status.RESOLVED,
    )


def resolve_remote_scope(raw_job, cfg, target_countries) -> dict | None:
    """Scope updates for a remote job left with no resolved country.

    Order (the user's contract): scan the JD with the LLM for a real location and
    use it if found; otherwise apply remote_unknown_policy ('us' → assume USA).
    Returns None when the job isn't remote, or policy='review' (fall through to the
    normal review/cold handling).
    """
    if not _looks_remote(raw_job.location_raw or "", raw_job.title or ""):
        return None

    # 1. LLM JD location scan (opt-in via config) → resolve to a country.
    if getattr(cfg, "remote_llm_jd_scan", False):
        try:
            from .llm_classifier import extract_remote_location
            loc = extract_remote_location(
                raw_job.title or "",
                raw_job.description or raw_job.description_clean or "",
            )
        except Exception:
            loc = ""
        if loc:
            r = resolve_location(location_raw=loc, cfg=cfg)
            if r.country_code:
                is_target = r.country_code in target_countries
                return {
                    "country_code": r.country_code[:2],
                    "country": (r.country_name or "")[:128],
                    "country_source": "remote_jd_llm",
                    "country_confidence": 0.75,
                    "scope_status": (
                        RawJob.ScopeStatus.PRIORITY_TARGET if is_target
                        else RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY
                    ),
                    "is_priority": is_target,
                    "scope_reason": f"remote_jd_llm:{r.country_code}"[:128],
                }

    # 2. Policy fallback for bare remote (no location anywhere).
    policy = getattr(cfg, "remote_unknown_policy", "review")
    if policy == "us":
        return {
            "country_code": "US",
            "country": "United States",
            "country_source": "remote_default_us",
            "country_confidence": 0.5,
            "scope_status": RawJob.ScopeStatus.PRIORITY_TARGET,
            "is_priority": True,
            "scope_reason": "remote_default_us",
        }
    if policy == "target":
        return {
            "scope_status": RawJob.ScopeStatus.PRIORITY_TARGET,
            "scope_reason": "remote_no_country_policy_target",
            "is_priority": True,
        }
    if policy == "cold":
        return {
            "scope_status": RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY,
            "scope_reason": "remote_no_country_policy_cold",
            "is_priority": False,
        }
    return None  # 'review' → fall through to default handling


def _resolve_from_classifier(raw_text: str, normalized: str, title: str = "", description: str = "") -> LocationResolution | None:
    # Country resolution must be based on location fields only. Passing title or
    # JD text here caused bad rows where titles like "Manager - Greenwich, CT"
    # or description snippets were treated as location/country.
    country_name, region_name = country_classifier.detect_country(raw_text, title="", description="")
    code = _code_for_country(country_name)
    if not code:
        return None
    return LocationResolution(
        raw_text=raw_text,
        normalized_text=normalized,
        country_code=code,
        country_name=COUNTRY_CODE_TO_NAME.get(code, country_name),
        region_name=region_name if region_name not in {"Remote"} else "",
        confidence=0.86 if region_name else 0.88,
        source="rules",
        status=LocationCache.Status.RESOLVED,
    )


def _month_start():
    now = timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _hour_start():
    now = timezone.now()
    return now.replace(minute=0, second=0, microsecond=0)


def provider_requests_since(provider: str, since) -> int:
    if not provider or provider == "none":
        return 0
    qs = LocationCache.objects.filter(
        provider=provider,
        looked_up_at__gte=since,
    )
    attempts = qs.aggregate(total=Sum("request_count"))["total"] or 0
    legacy_successes = qs.filter(request_count=0).exclude(provider_place_id="").count()
    return int(attempts) + int(legacy_successes)


def provider_requests_this_month(provider: str) -> int:
    return provider_requests_since(provider, _month_start())


def provider_requests_this_hour(provider: str) -> int:
    return provider_requests_since(provider, _hour_start())


def provider_quota_status(cfg: HarvestEngineConfig, *, force: bool = False) -> dict:
    """force=True is used for explicit on-demand provider calls (the review
    'Re-evaluate with Mapbox' button and the backlog sweep): it treats the
    provider as enabled even when auto-during-harvest is off, so a token + caps
    are the only gate. Monthly/hourly caps are STILL enforced when forced."""
    provider = (cfg.geocoding_provider or "none").strip().lower()
    enabled = (bool(cfg.geocoding_provider_enabled) or force) and provider != "none"
    monthly_limit = int(cfg.geocoding_monthly_limit or 0)
    hourly_limit = int(getattr(cfg, "geocoding_hourly_limit", 0) or 0)
    warning_pct = int(getattr(cfg, "geocoding_warning_pct", 80) or 80)
    warning_pct = min(100, max(1, warning_pct))

    monthly_used = provider_requests_this_month(provider) if enabled else 0
    hourly_used = provider_requests_this_hour(provider) if enabled else 0
    monthly_pct = round((monthly_used / monthly_limit) * 100, 1) if monthly_limit else 0.0
    hourly_pct = round((hourly_used / hourly_limit) * 100, 1) if hourly_limit else 0.0
    monthly_exhausted = bool(enabled and (monthly_limit <= 0 or monthly_used >= monthly_limit))
    hourly_exhausted = bool(enabled and hourly_limit > 0 and hourly_used >= hourly_limit)
    status = {
        "provider": provider,
        "enabled": enabled,
        "warning_pct": warning_pct,
        "monthly_limit": monthly_limit,
        "monthly_used": monthly_used,
        "monthly_pct": monthly_pct,
        "monthly_warning": bool(enabled and monthly_limit and monthly_pct >= warning_pct),
        "monthly_exhausted": monthly_exhausted,
        "hourly_limit": hourly_limit,
        "hourly_used": hourly_used,
        "hourly_pct": hourly_pct,
        "hourly_warning": bool(enabled and hourly_limit and hourly_pct >= warning_pct),
        "hourly_exhausted": hourly_exhausted,
    }
    status["available"] = bool(enabled and not monthly_exhausted and not hourly_exhausted)
    return status


def _warn_provider_quota_once(status: dict) -> None:
    if not status.get("enabled"):
        return
    provider = status.get("provider") or "provider"
    reasons = []
    if status.get("monthly_exhausted"):
        reasons.append("monthly_exhausted")
    elif status.get("monthly_warning"):
        reasons.append("monthly_warning")
    if status.get("hourly_exhausted"):
        reasons.append("hourly_exhausted")
    elif status.get("hourly_warning"):
        reasons.append("hourly_warning")
    for reason in reasons:
        key = f"harvest:geocoding-quota-warning:{provider}:{reason}:{timezone.now():%Y%m%d%H}"
        if cache.add(key, True, timeout=3700):
            logger.warning(
                "Geocoding quota %s for %s: month=%s/%s (%.1f%%), hour=%s/%s (%.1f%%)",
                reason,
                provider,
                status.get("monthly_used", 0),
                status.get("monthly_limit", 0),
                status.get("monthly_pct", 0.0),
                status.get("hourly_used", 0),
                status.get("hourly_limit", 0),
                status.get("hourly_pct", 0.0),
            )


def _provider_quota_available(cfg: HarvestEngineConfig, *, force: bool = False) -> bool:
    status = provider_quota_status(cfg, force=force)
    _warn_provider_quota_once(status)
    return bool(status["available"])


def _resolve_provider_token(provider: str, cfg: HarvestEngineConfig) -> str:
    """Token resolution priority: DB token (set via GUI) → env var.

    DB storage allows rotating the token from the portal without SSH access.
    Env var remains the more secure default — used when DB field is blank.
    """
    db_token = (cfg.geocoding_provider_token or "").strip()
    if db_token:
        return db_token
    if provider == "mapbox":
        return os.getenv("MAPBOX_ACCESS_TOKEN", "").strip()
    if provider == "google":
        return os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    return ""


def _record_provider_attempt(provider: str, raw_text: str, normalized: str) -> None:
    if not normalized:
        return
    cache, _ = LocationCache.objects.get_or_create(
        normalized_text=normalized[:512],
        defaults={
            "raw_text": raw_text[:512],
            "source": "provider",
            "provider": provider,
            "status": LocationCache.Status.UNKNOWN,
        },
    )
    cache.raw_text = raw_text[:512]
    cache.source = "provider"
    cache.provider = provider
    cache.request_count = int(cache.request_count or 0) + 1
    if not cache.status:
        cache.status = LocationCache.Status.UNKNOWN
    cache.looked_up_at = timezone.now()
    cache.save(update_fields=[
        "raw_text",
        "source",
        "provider",
        "request_count",
        "status",
        "looked_up_at",
    ])


def _mapbox_geocode(raw_text: str, normalized: str, cfg: HarvestEngineConfig, *, force: bool = False) -> LocationResolution | None:
    if not _provider_quota_available(cfg, force=force):
        return None
    token = _resolve_provider_token("mapbox", cfg)
    if not token:
        return None
    _record_provider_attempt("mapbox", raw_text, normalized)

    params = urllib.parse.urlencode({
        "q": raw_text,
        "access_token": token,
        "limit": "1",
        "types": "address,place,locality,region,country",
    })
    url = f"https://api.mapbox.com/search/geocode/v6/forward?{params}"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("Mapbox geocode failed for %s: %s", normalized, exc)
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            confidence=0.0,
            source="provider",
            provider="mapbox",
            status=LocationCache.Status.FAILED,
        )

    features = payload.get("features") or []
    if not features:
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            confidence=0.0,
            source="provider",
            provider="mapbox",
            status=LocationCache.Status.UNKNOWN,
        )

    feature = features[0]
    props = feature.get("properties") or {}
    context = props.get("context") or {}
    country = context.get("country") or {}
    region = context.get("region") or {}
    place = context.get("place") or {}

    country_code = _code_for_country(country.get("country_code", ""))
    if not country_code:
        country_code = _code_for_country(country.get("name", ""))
    if not country_code:
        return LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            confidence=0.0,
            source="provider",
            provider="mapbox",
            provider_place_id=str(props.get("mapbox_id") or feature.get("id") or ""),
            status=LocationCache.Status.UNKNOWN,
        )

    return LocationResolution(
        raw_text=raw_text,
        normalized_text=normalized,
        country_code=country_code,
        country_name=COUNTRY_CODE_TO_NAME.get(country_code, country.get("name", "")),
        region_code=(region.get("region_code") or "").upper(),
        region_name=region.get("name", ""),
        city=place.get("name", ""),
        confidence=0.97,
        source="provider",
        provider="mapbox",
        provider_place_id=str(props.get("mapbox_id") or feature.get("id") or ""),
        status=LocationCache.Status.RESOLVED,
    )


def _cache_resolution(resolution: LocationResolution) -> LocationResolution:
    if not resolution.normalized_text:
        return resolution
    # Truncate every CharField to its model max_length. Mapbox's mapbox_id
    # can exceed 255 chars for some places; country_name / city / region_name
    # can also be unexpectedly long (e.g. 'The Federated States of …').
    LocationCache.objects.update_or_create(
        normalized_text=(resolution.normalized_text or "")[:512],
        defaults={
            "raw_text": (resolution.raw_text or "")[:512],
            "country_code": (resolution.country_code or "")[:2],
            "country_name": (resolution.country_name or "")[:128],
            "region_code": (resolution.region_code or "")[:16],
            "region_name": (resolution.region_name or "")[:128],
            "city": (resolution.city or "")[:128],
            "confidence": resolution.confidence,
            "source": (resolution.source or "")[:32],
            "provider": (resolution.provider or "")[:32],
            "provider_place_id": (resolution.provider_place_id or "")[:255],
            "status": (resolution.status or "")[:16],
        },
    )
    return resolution


def resolve_location(
    *,
    location_raw: str = "",
    city: str = "",
    state: str = "",
    country: str = "",
    title: str = "",
    description: str = "",
    cfg: HarvestEngineConfig | None = None,
    use_provider: bool = False,
    force_provider: bool = False,
) -> LocationResolution:
    cfg = cfg or HarvestEngineConfig.get()
    raw_text = ", ".join(part for part in [location_raw, city, state, country] if (part or "").strip())
    raw_text = raw_text or location_raw or city or state or country
    normalized = normalize_location_text(raw_text)
    if not normalized:
        # Location normalized to nothing — usually a bare "Remote". Try to pull a
        # country clue from the remote string, title, or JD before giving up.
        remote_resolution = _resolve_remote(
            location_raw, city, state, country, normalized="", title=title, description=description
        )
        if remote_resolution:
            return remote_resolution
        if _is_ambiguous_location_only(location_raw, city, state, country):
            return LocationResolution(
                raw_text=raw_text,
                normalized_text=_location_token(raw_text)[:512],
                confidence=0.0,
                source="ambiguous_multi_location",
                status=LocationCache.Status.UNKNOWN,
            )
        return LocationResolution(raw_text="", normalized_text="", source="empty")
    placeholder_only = _is_ambiguous_location_only(location_raw, city, state, country)
    country_for_rules = "" if is_placeholder_location_value(country) else country
    raw_text_for_rules = "" if placeholder_only else raw_text
    location_resolution = _resolve_from_state_city(raw_text_for_rules, normalized)

    if cfg.geocoding_cache_enabled:
        cached = LocationCache.objects.filter(normalized_text=normalized).first()
        # Skip cache hit if previous resolution was UNKNOWN AND provider is now
        # available — gives the upgraded resolver a chance to retry via Mapbox.
        # Otherwise UNKNOWN cache entries from earlier no-provider runs would
        # permanently shadow the provider call.
        cache_is_unknown = cached and cached.status == LocationCache.Status.UNKNOWN
        cache_country_code = _code_for_country(cached.country_code) if cached else ""
        cache_has_invalid_country = bool(cached and cached.country_code and not cache_country_code)
        cache_conflicts_location = bool(
            cached
            and cache_country_code
            and location_resolution
            and location_resolution.country_code
            and location_resolution.country_code != cache_country_code
            and _should_prefer_location_resolution(location_resolution, cached, raw_text_for_rules)
        )
        provider_now_available = (
            (use_provider or force_provider)
            and (cfg.geocoding_provider_enabled or force_provider)
            and cfg.geocoding_provider in {"mapbox", "google"}
            and bool(_resolve_provider_token(cfg.geocoding_provider, cfg))
        )
        if cached and not cache_has_invalid_country and not cache_conflicts_location and not (cache_is_unknown and provider_now_available):
            return LocationResolution(
                raw_text=cached.raw_text or raw_text,
                normalized_text=cached.normalized_text,
                country_code=cache_country_code,
                country_name=cached.country_name,
                region_code=cached.region_code,
                region_name=cached.region_name,
                city=cached.city,
                confidence=cached.confidence,
                source=cached.source,
                status=cached.status,
                provider=cached.provider,
                provider_place_id=cached.provider_place_id,
            )

    explicit_resolution = _resolve_from_explicit_country(country_for_rules, raw_text, normalized)
    if _should_prefer_location_resolution(location_resolution, explicit_resolution, raw_text_for_rules):
        resolution = location_resolution
    else:
        resolution = (
            explicit_resolution
            or location_resolution
            or _resolve_from_classifier(raw_text_for_rules, normalized, title=title, description=description)
        )

    if not resolution and (use_provider or force_provider) and cfg.geocoding_provider == "mapbox" and not placeholder_only:
        resolution = _mapbox_geocode(raw_text, normalized, cfg, force=force_provider)

    if not resolution:
        resolution = LocationResolution(
            raw_text=raw_text,
            normalized_text=normalized,
            confidence=0.0,
            source="ambiguous_multi_location" if placeholder_only else "unknown",
            status=LocationCache.Status.UNKNOWN,
        )

    if cfg.geocoding_cache_enabled:
        _cache_resolution(resolution)
    return resolution


def has_target_domain_signal(raw_job: RawJob) -> bool:
    if raw_job.job_domain and raw_job.job_domain in TARGET_DOMAIN_SLUGS:
        return True
    for slug in raw_job.job_domain_candidates or []:
        if slug in TARGET_DOMAIN_SLUGS:
            return True

    try:
        from .enrichments import detect_job_domains
        slugs = detect_job_domains(
            raw_job.title or "",
            raw_job.description or "",
            raw_job.job_category or "",
            raw_job.department_normalized or "",
            max_matches=3,
        )
    except Exception:
        slugs = []
    return any(slug in TARGET_DOMAIN_SLUGS for slug in slugs)


def evaluate_rawjob_scope(
    raw_job: RawJob,
    *,
    cfg: HarvestEngineConfig | None = None,
    use_provider: bool | None = False,
    force_provider: bool = False,
    save: bool = False,
) -> dict:
    cfg = cfg or HarvestEngineConfig.get()
    if use_provider is None:
        use_provider = bool(getattr(cfg, "geocoding_provider_enabled", False))
    if force_provider:
        use_provider = True
    location_candidates = extract_location_candidates(
        location_raw=raw_job.location_raw or "",
        city=raw_job.city or "",
        state=raw_job.state or "",
        country=raw_job.country or "",
        vendor_location_block=raw_job.vendor_location_block or "",
        raw_payload=raw_job.raw_payload or {},
    )
    for existing in raw_job.location_candidates or []:
        for candidate in split_multi_location_text(existing):
            _dedupe_append(location_candidates, candidate)

    resolution = resolve_location(
        location_raw=raw_job.location_raw or "",
        city=raw_job.city or "",
        state=raw_job.state or "",
        country=raw_job.country or "",
        title=raw_job.title or "",
        description=raw_job.description or raw_job.description_clean or "",
        cfg=cfg,
        use_provider=use_provider,
        force_provider=force_provider,
    )

    target_countries = set(cfg.get_target_countries() or DEFAULT_TARGET_COUNTRIES)
    candidate_resolutions: list[LocationResolution] = []
    country_codes: list[str] = []
    for candidate in location_candidates:
        candidate_resolution = resolve_location(
            location_raw=candidate,
            title=raw_job.title or "",
            description=raw_job.description or raw_job.description_clean or "",
            cfg=cfg,
            use_provider=use_provider,
            force_provider=force_provider,
        )
        if candidate_resolution.country_code:
            candidate_resolutions.append(candidate_resolution)
            if candidate_resolution.country_code not in country_codes:
                country_codes.append(candidate_resolution.country_code)

    target_resolution = next(
        (item for item in candidate_resolutions if item.country_code in target_countries),
        None,
    )
    if target_resolution:
        resolution = target_resolution
    elif candidate_resolutions and not resolution.country_code:
        resolution = candidate_resolutions[0]

    # Truncate to RawJob field limits — Mapbox returns can be unexpectedly long
    # for some places, and the model fields are: country_code(2),
    # country_source(32), country(128), state(128), city(128).
    updates = {
        "country_code": (resolution.country_code or "")[:2],
        "country_confidence": resolution.confidence,
        "country_source": ("multi_location" if candidate_resolutions else (resolution.source or ""))[:32],
        "country_codes": country_codes,
        "location_candidates": location_candidates,
        "last_scope_evaluated_at": timezone.now(),
    }

    country_is_placeholder = is_placeholder_location_value(raw_job.country or "")
    country_is_invalid = bool((raw_job.country or "").strip() and not _code_for_country(raw_job.country or ""))
    state_is_placeholder = is_placeholder_location_value(raw_job.state or "")
    city_is_placeholder = is_placeholder_location_value(raw_job.city or "")

    if country_is_placeholder or country_is_invalid:
        updates["country"] = ""
    if state_is_placeholder:
        updates["state"] = ""
    if city_is_placeholder:
        updates["city"] = ""
    if location_candidates and is_placeholder_location_value(raw_job.location_raw or ""):
        updates["location_raw"] = " | ".join(location_candidates)[:512]

    current_country_code = _code_for_country(raw_job.country or "")
    country_conflicts = bool(current_country_code and resolution.country_code and current_country_code != resolution.country_code)
    if resolution.country_name and (not raw_job.country or country_is_placeholder or country_is_invalid or country_conflicts):
        updates["country"] = resolution.country_name[:128]
    if resolution.region_code and (not raw_job.state or state_is_placeholder):
        updates["state"] = resolution.region_code[:128]
    if resolution.city and (not raw_job.city or city_is_placeholder):
        updates["city"] = resolution.city[:128]

    if resolution.country_code in target_countries:
        updates.update({
            "scope_status": RawJob.ScopeStatus.PRIORITY_TARGET,
            "scope_reason": (
                f"target_country_multi:{resolution.country_code}"
                if len(country_codes) > 1 or len(location_candidates) > 1
                else f"target_country:{resolution.country_code}"
            )[:128],
            "is_priority": True,
        })
    elif not resolution.country_code:
        remote_updates = resolve_remote_scope(raw_job, cfg, target_countries)
        if remote_updates is not None:
            updates.update(remote_updates)
        elif cfg.process_unknown_country_with_target_domain and has_target_domain_signal(raw_job):
            updates.update({
                "scope_status": RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY,
                "scope_reason": (
                    "ambiguous_multi_location_target_domain"
                    if resolution.source == "ambiguous_multi_location"
                    else "unknown_country_target_domain"
                )[:128],
                "is_priority": True,
            })
        else:
            updates.update({
                "scope_status": RawJob.ScopeStatus.COLD_NO_LOCATION if not resolution.normalized_text else RawJob.ScopeStatus.REVIEW_UNKNOWN_COUNTRY,
                "scope_reason": (
                    "ambiguous_multi_location"
                    if resolution.source == "ambiguous_multi_location"
                    else "country_unknown"
                )[:128],
                "is_priority": False,
            })
    else:
        updates.update({
            "scope_status": RawJob.ScopeStatus.COLD_NON_TARGET_COUNTRY,
            "scope_reason": f"non_target_country:{resolution.country_code}"[:128],
            "is_priority": False,
        })

    if save:
        for field, value in updates.items():
            setattr(raw_job, field, value)
        raw_job.save(update_fields=list(updates.keys()) + ["updated_at"])
    return updates


def scope_counts():
    return {
        row["scope_status"]: row["count"]
        for row in RawJob.objects.values("scope_status").annotate(count=Count("id"))
    }

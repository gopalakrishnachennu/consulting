from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


STRONG = "STRONG"
POSSIBLE = "POSSIBLE"
COLD = "COLD"
UNKNOWN = "UNKNOWN"
NO_MATCH = "NO_MATCH"

# ── Tier-1 title gate decisions (confidence-aware) ────────────────────────────
# These are the outputs of the new classify_title_v2() function.
# HARD_YES  → high confidence phrase match → goes straight to full JD backfill.
# HARD_NO   → hard negative (or zero signal) → store minimal row, no JD fetch.
# AMBIGUOUS → borderline match → sent to Tier-2 JD content gate (LLM snippet).
HARD_YES  = "HARD_YES"
HARD_NO   = "HARD_NO"
AMBIGUOUS = "AMBIGUOUS"

# Default confidence threshold: phrase match confidence below this → AMBIGUOUS.
# Overridable via HarvestEngineConfig.title_hard_yes_confidence.
DEFAULT_HARD_YES_CONFIDENCE = 0.80

SENIORITY_PATTERNS = [
    r"\bsenior\b",
    r"\bjunior\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bsr\.?\b",
    r"\bjr\.?\b",
    r"\bhead of\b",
    r"\bdirector of\b",
    r"\bvp of\b",
    r"\bdistinguished\b",
    r"\bassociate\b",
    r"\bmid-?level\b",
    r"\bentry-?level\b",
    r"\bl[3-9]\b",
    r"\bic[3-9]\b",
    r"\be[3-9]\b",
    r"\bi{1,3}v?\b",
    r"\b\d+\b",
]

TECH_DEPARTMENT_SIGNALS = [
    "engineering",
    "technology",
    "data",
    "cloud",
    "platform",
    "infrastructure",
    "security",
    "information technology",
    "software",
    "product engineering",
    "devops",
    "it",
    "systems",
    "computing",
    "artificial intelligence",
    "machine learning",
    "research and development",
    "r&d",
    "digital",
]

NON_TECH_DEPARTMENT_SIGNALS = [
    # Healthcare / clinical — bedside & hospital operations
    "nursing",
    "patient care",
    "patient services",
    "clinical operations",
    "clinical services",
    "pharmacy",
    "pharmacology",
    "dental",
    "radiology",
    "laboratory",
    "pathology",
    "oncology",
    "cardiology",
    "pediatrics",
    "orthopedics",
    "neurology",
    "rehabilitation",
    "therapy services",
    "surgical services",
    "emergency medicine",
    "primary care",
    "behavioral health",
    "mental health",
    "hospice",
    "home health",
    # Note: "clinical" alone is intentionally excluded here — "clinical informatics"
    # and "clinical systems" are IT departments.  Use hard_negative_phrases in
    # HarvestEngineConfig for title-level blocking of pure clinical roles.
    # Food / hospitality / facilities
    "food service",
    "culinary",
    "housekeeping",
    "environmental services",
    "facilities management",
    "laundry",
    # Retail / warehouse
    "retail operations",
    "warehouse operations",
    "store operations",
    "distribution center",
    # Other non-tech
    "human resources",
    "payroll",
    "legal",
    "compliance",
    "audit",
    "finance",
    "accounting",
    "marketing",
    "sales",
    "customer service",
]

GENERIC_TECH_SIGNALS = [
    # Last-resort POSSIBLE signals — job title has no category phrase match but
    # still looks like a tech role.  POSSIBLE still fetches the JD, only COLD
    # and NO_MATCH skip it.
    #
    # Rules:
    #   - Multi-word phrases only — single words are too broad.
    #   - Add here when a real-world title form isn't caught by any category
    #     phrase but is unambiguously a tech role.  Category phrases take
    #     priority (they fire first and return STRONG).
    "software engineer",
    "software developer",
    "data engineer",
    "data analyst",
    "data scientist",
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "artificial intelligence engineer",
    "quality engineer",
    "test engineer",
    "test automation engineer",
    "technical architect",
    "platform engineer",
    "systems engineer",
    "security engineer",
    "security analyst",           # catches "Security Operations Analyst" after ops expansion
    "security operations",        # broad fallback: "Security Operations Analyst" → POSSIBLE
    "secops",                     # catches "SecOps Analyst/Manager" after sec+ops compound join
    "network engineer",
    "network analyst",
    "cloud architect",
    "solutions architect",
    "product engineer",
    "research engineer",
    "engineering manager",
    # Enterprise platform roles (catch-all until dedicated category phrases match)
    "servicenow developer",
    "salesforce developer",
    "sap consultant",
    "workday consultant",
    "oracle consultant",
    # Healthcare IT
    "healthcare it",
    "ehr analyst",
    "emr analyst",
    "epic analyst",
    "cerner analyst",
]


# ── Canonical-form normalizations ────────────────────────────────────────────
# Job boards write the same role in wildly inconsistent ways.  Rather than
# enumerating thousands of phrase variants, we collapse known synonym families
# to a single canonical token BEFORE phrase matching.
#
# Rules applied in both normalize() (titles) and normalize_phrase() (phrases)
# so both sides always meet at the same form.
#
# Design principles:
#   1. Patterns must be SPECIFIC — "operations" alone is not collapsed (too
#      broad: "hospital operations", "retail operations" are non-tech).
#      Only tech-prefixed "operations" → "ops".
#   2. Longer / more specific patterns before shorter ones.
#   3. No pattern should introduce false positives for non-tech roles.
# ─────────────────────────────────────────────────────────────────────────────

# Step 1 — Verbose → ops  (e.g. "ml operations" → "mlops")
# Matches "[tech prefix] operations" and collapses to the ops portmanteau.
# The prefix list is intentionally narrow — only unambiguous tech abbreviations.
_OPS_VERBOSE = re.compile(
    # Matches "[tech prefix] operations" and collapses to the ops portmanteau.
    # "security" is intentionally excluded: "Security Operations Analyst" should
    # stay as "security operations analyst" so it can match "security analyst"
    # phrases and GENERIC_TECH_SIGNALS — transforming it to "secops analyst"
    # would break those matches.  "dev security operations" still becomes
    # "devsecops" via the three-word COMPOUND_JOINS rule.
    r"\b(dev\s+sec|dev(?:elopment)?|ml|machine\s+learning|ai|artificial\s+intelligence"
    r"|data|fin(?:ancial)?|cloud|git|platform|network"
    r"|it|information\s+technology)\s+operations\b"
)

def _ops_verbose_to_portmanteau(text: str) -> str:
    """'ml operations engineer' → 'mlops engineer', 'development operations' → 'devops'."""
    def _replace(m: re.Match) -> str:
        prefix = re.sub(r"\s+", "", m.group(1))  # collapse inner spaces
        # canonicalize verbose prefixes to their short form
        prefix = {
            "development":            "dev",
            "machinelearning":        "ml",
            "artificialintelligence": "ai",
            "financial":              "fin",
            "informationtechnology":  "it",
            "devsec":                 "devsec",  # handled by 3-word rule below
        }.get(prefix, prefix)
        return prefix + "ops"
    return _OPS_VERBOSE.sub(_replace, text)


# Step 2 — "[X] ops" / "[X] op" two-word portmanteau → single token
# Handles "dev ops" → "devops", "ml ops" → "mlops", etc.
COMPOUND_JOINS = [
    # Three-word ops (longest first)
    (r"\bdev\s+sec\s+ops\b",              "devsecops"),
    (r"\bsite\s+reliability\s+engineering\b", "sre"),
    # Two-word ops
    (r"\bdev\s+ops\b",                    "devops"),
    (r"\bml\s+ops\b",                     "mlops"),
    (r"\bdata\s+ops\b",                   "dataops"),
    (r"\bsec\s+ops\b",                    "secops"),
    (r"\bfin\s+ops\b",                    "finops"),
    (r"\bcloud\s+ops\b",                  "cloudops"),
    (r"\bit\s+ops\b",                     "itops"),
    (r"\bgit\s+ops\b",                    "gitops"),
    (r"\bplatform\s+ops\b",               "platformops"),
    # Common tech split-words
    (r"\bback\s+end\b",                   "backend"),
    (r"\bfront\s+end\b",                  "frontend"),
    (r"\bfull\s+stack\b",                 "full stack"),  # keep two-word
    (r"\bservice\s+now\b",                "servicenow"),
    (r"\bsales\s+force\b",                "salesforce"),
    (r"\bwork\s+day\b",                   "workday"),
    (r"\bopen\s+ai\b",                    "openai"),
    (r"\bno\s+sql\b",                     "nosql"),
    (r"\bci\s+cd\b",                      "cicd"),
    (r"\btype\s+script\b",                "typescript"),
    (r"\bjava\s+script\b",                "javascript"),
    (r"\bpower\s+bi\b",                   "power bi"),    # keep two-word (it's a product name)
    (r"\bmachine\s+learning\b",           "machine learning"),  # keep — it's already a phrase
]


def _apply_compound_joins(text: str) -> str:
    text = _ops_verbose_to_portmanteau(text)       # "ml operations" → "mlops"
    for pattern, replacement in COMPOUND_JOINS:    # "ml ops" → "mlops"
        text = re.sub(pattern, replacement, text)
    return text


@dataclass(frozen=True)
class ClassifyResult:
    decision: str
    category: str | None
    matched_phrase: str | None
    matched_negative: str | None
    reason: str
    snapshot_id: str | None
    # Phrase-match confidence: 1.0 for exact include-phrase match, lower for
    # fallback signals (generic tech → 0.5, department-only → 0.4).
    # Used by classify_title_v2() to route HARD_YES vs AMBIGUOUS.
    confidence: float = 1.0


@dataclass(frozen=True)
class TitleGateResult:
    """Output of classify_title_v2() — the Tier-1 confidence-aware title gate."""
    gate_decision: str          # HARD_YES | HARD_NO | AMBIGUOUS
    gate_confidence: float      # 0.0–1.0
    classify_result: ClassifyResult   # full original classify result for audit


def normalize(text: str) -> str:
    """Normalize a job TITLE for matching.

    Steps:
      1. Lowercase + strip
      2. Separators (- _ / | backslash) → space
      3. Punctuation → space
      4. Compound-join ops portmanteaus: "ml ops" → "mlops", "dev ops" → "devops"
      5. Strip seniority prefixes/suffixes so "Senior ML Ops Engineer" and
         "MLOps Engineer" both collapse to "mlops engineer"
    """
    if not text:
        return ""
    text = str(text).lower().strip()
    text = re.sub(r"[-_/\\|]", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _apply_compound_joins(text)
    for pattern in SENIORITY_PATTERNS:
        text = re.sub(pattern, " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_phrase(phrase: str) -> str:
    """Normalize a phrase from the category include/exclude bank.

    Same basic cleanup as normalize() + compound joins, but intentionally does
    NOT strip seniority words.  This means:
      - Phrases should be written without seniority prefixes ("mlops engineer"
        not "senior mlops engineer").
      - "ml ops engineer" entered as a phrase normalizes to "mlops engineer"
        so it matches titles written either way.
    """
    if not phrase:
        return ""
    text = str(phrase).lower().strip()
    text = re.sub(r"[-_/\\|]", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _apply_compound_joins(text)
    return re.sub(r"\s+", " ", text).strip()


EQUIVALENT_PHRASE_VARIANTS = {
    "servicenow": ("service now",),
    "salesforce": ("sales force",),
    "workday": ("work day",),
}
_INVERSE_EQUIVALENT_VARIANTS = {
    alias: canonical
    for canonical, aliases in EQUIVALENT_PHRASE_VARIANTS.items()
    for alias in aliases
}


def phrase_search_variants(phrase: str) -> list[str]:
    """Equivalent normalized variants used for UI impact searches on old rows.

    Runtime title classification normalizes both sides, so one canonical form is
    enough there. The preview UI also needs to match historical RawJob
    ``normalized_title`` values that may still contain pre-canonical forms such
    as ``service now``. This helper returns all safe equivalents.
    """
    normalized = normalize_phrase(phrase)
    if not normalized:
        return []
    canonical = _INVERSE_EQUIVALENT_VARIANTS.get(normalized, normalized)
    variants = {canonical, normalized}
    variants.update(EQUIVALENT_PHRASE_VARIANTS.get(canonical, ()))
    return sorted(variants)


def phrase_match(normalized_text: str, phrase: str) -> bool:
    """Return True if *phrase* appears as a whole word in *normalized_text*.

    *normalized_text* must already be the output of ``normalize()`` (title with
    seniority stripped).  The phrase is normalized with ``normalize_phrase()``
    which keeps the phrase words intact so that "staff engineer" only matches
    titles that literally say "staff engineer" after basic cleanup — not every
    title containing the single word "engineer".
    """
    p = normalize_phrase(phrase)
    if not p:
        return False
    pattern = r"(?<!\w)" + re.escape(p) + r"(?!\w)"
    return bool(re.search(pattern, normalized_text))


def _first_phrase_match(normalized_text: str, phrases: list[str]) -> str | None:
    for phrase in phrases or []:
        if phrase_match(normalized_text, str(phrase)):
            return str(phrase)
    return None


def _category_list(categories: list[Any]) -> list[dict]:
    return [c for c in (categories or []) if isinstance(c, dict)]


def compute_phrase_hash(payload: dict) -> str:
    body = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def classify_title(
    *,
    title: str,
    department: str = "",
    categories: list[dict] | None = None,
    hard_negatives: list[str] | None = None,
    custom_phrases: list[str] | None = None,
    snapshot_id: str | None = None,
) -> ClassifyResult:
    title_raw = title or ""
    if not title_raw.strip():
        return ClassifyResult(UNKNOWN, None, None, None, "empty or null title - cannot classify", snapshot_id, confidence=0.0)

    if not re.search(r"[A-Za-z]", title_raw):
        return ClassifyResult(UNKNOWN, None, None, None, "non-ASCII title - cannot match English phrases", snapshot_id, confidence=0.0)

    normalized_title = normalize(title_raw)
    normalized_department = normalize(department or "")
    cats = _category_list(categories or [])

    include_hit: tuple[dict, str] | None = None
    for category in cats:
        phrase = _first_phrase_match(normalized_title, category.get("include_phrases") or [])
        if phrase:
            include_hit = (category, phrase)
            break

    negative = _first_phrase_match(normalized_title, hard_negatives or [])
    if negative and include_hit is None:
        return ClassifyResult(NO_MATCH, None, None, negative, f"matched hard negative: {negative}", snapshot_id, confidence=0.0)

    custom_hit = _first_phrase_match(normalized_title, custom_phrases or [])
    if custom_hit:
        return ClassifyResult(STRONG, None, custom_hit, negative, f"company-specific phrase: {custom_hit}", snapshot_id, confidence=1.0)

    if include_hit is not None:
        category, phrase = include_hit
        category_exclude = _first_phrase_match(normalized_title, category.get("exclude_phrases") or [])
        category_slug = str(category.get("slug") or "") or None
        category_name = str(category.get("name") or category_slug or "")
        if category_exclude:
            return ClassifyResult(
                POSSIBLE,
                category_slug,
                phrase,
                negative or category_exclude,
                f"include '{phrase}' and exclude '{category_exclude}' both matched - keeping as POSSIBLE",
                snapshot_id,
                confidence=0.60,   # ambiguous: include + exclude both fire
            )
        reason = f"matched phrase: {phrase} | category: {category_name}"
        if negative:
            reason = f"ambiguous: negative '{negative}' and include phrase '{phrase}' both matched - keeping"
            return ClassifyResult(STRONG, category_slug, phrase, negative, reason, snapshot_id, confidence=0.75)
        return ClassifyResult(STRONG, category_slug, phrase, negative, reason, snapshot_id, confidence=1.0)

    tech_department = _first_phrase_match(normalized_department, TECH_DEPARTMENT_SIGNALS)
    non_tech_department = _first_phrase_match(normalized_department, NON_TECH_DEPARTMENT_SIGNALS)
    generic_hit = _first_phrase_match(normalized_title, GENERIC_TECH_SIGNALS)

    if generic_hit and non_tech_department:
        return ClassifyResult(
            COLD,
            None,
            generic_hit,
            None,
            f"generic title but non-tech department: {department}",
            snapshot_id,
            confidence=0.1,
        )

    if tech_department:
        return ClassifyResult(
            POSSIBLE,
            None,
            tech_department,
            None,
            f"no title match but department signals tech: {department}",
            snapshot_id,
            confidence=0.40,  # department signal only — worth a JD look
        )

    if generic_hit:
        return ClassifyResult(POSSIBLE, None, generic_hit, None, f"generic tech signal: {generic_hit}", snapshot_id, confidence=0.50)

    return ClassifyResult(COLD, None, None, None, "no tech signal in title or department", snapshot_id, confidence=0.0)


def classify_title_v2(
    *,
    title: str,
    department: str = "",
    categories: list[dict] | None = None,
    hard_negatives: list[str] | None = None,
    custom_phrases: list[str] | None = None,
    snapshot_id: str | None = None,
    hard_yes_threshold: float = DEFAULT_HARD_YES_CONFIDENCE,
) -> TitleGateResult:
    """
    Tier-1 confidence-aware title gate.

    Wraps classify_title() and maps its output to one of three gate decisions:

      HARD_YES  — confidence >= hard_yes_threshold AND decision is STRONG.
                  High-confidence phrase match. Skip JD gate, go straight to
                  full JD backfill.

      HARD_NO   — decision is NO_MATCH or COLD (no tech signal).
                  Store minimal row. No JD fetch. Done.

      AMBIGUOUS — everything else (STRONG below threshold, POSSIBLE, UNKNOWN).
                  Send to Tier-2 JD content gate for LLM confirmation.

    Args:
        hard_yes_threshold: phrase-match confidence floor for HARD_YES.
                            Controlled by HarvestEngineConfig.title_hard_yes_confidence.
    """
    result = classify_title(
        title=title,
        department=department,
        categories=categories,
        hard_negatives=hard_negatives,
        custom_phrases=custom_phrases,
        snapshot_id=snapshot_id,
    )

    decision = result.decision
    conf = result.confidence

    # ── Route to gate bin ─────────────────────────────────────────────────────
    if decision == NO_MATCH:
        # Explicit hard negative hit — definitively not our target.
        gate = HARD_NO
    elif decision == COLD and conf < 0.2:
        # True COLD: zero signal in title or department.
        gate = HARD_NO
    elif decision == STRONG and conf >= hard_yes_threshold:
        # High-confidence phrase match — skip JD gate.
        gate = HARD_YES
    else:
        # Everything borderline: POSSIBLE, low-confidence STRONG, UNKNOWN, COLD-with-signal.
        # Send to JD gate for LLM confirmation.
        gate = AMBIGUOUS

    return TitleGateResult(
        gate_decision=gate,
        gate_confidence=conf,
        classify_result=result,
    )

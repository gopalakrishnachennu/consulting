"""
Country detection engine — 5-tier pipeline, zero paid APIs.

Tier 0: Remote detection (remote/wfh/anywhere → "Remote")
Tier 1: country-converter on location string
Tier 2: US state / CA province abbreviation lookup
Tier 2b: Major city → country lookup (cities not resolved by country-converter)
Tier 3: Currency / visa / right-to-work signals in description
Tier 4: APAC / EMEA / LATAM / region keywords (location only, not title)
Tier 5: Scan description[:500] for country names

Edge cases covered:
- "Remote (US only)" / "Remote - US" / "Remote, US" → country=United States, region=Remote
- "Remote - Chicago, IL" → United States, Remote (state extracted before remote exit)
- "London" ambiguity → UK (more common than London, Ontario)
- "CA" → California (USA) not Canada
- APAC/EMEA → country="Multiple", region="APAC"
- Currency £/€ signals (€ → Europe region, not Germany)
- Visa/right-to-work keywords
- HTML stripped before scanning
- Region keywords only checked in location field, not job title
- Major city lookup covers top 120 tech-hub cities globally
- Remote description country extraction when location is empty
"""
from __future__ import annotations

import re
from html.parser import HTMLParser


# ── HTML stripping ────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(text: str) -> str:
    if not text or "<" not in text:
        return text
    s = _HTMLStripper()
    s.feed(text)
    return " ".join(s.parts)


# ── US states + CA provinces (abbreviation → country) ────────────────────────

_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}
_CA_PROVINCES = {"AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT"}

# ── Major city → country lookup (country-converter doesn't resolve cities) ───

_CITY_COUNTRY: dict[str, str] = {
    # United States — top tech hubs + major metros
    "new york": "United States", "new york city": "United States", "nyc": "United States",
    "los angeles": "United States", "la": "United States",
    "san francisco": "United States", "sf": "United States",
    "san francisco bay area": "United States", "bay area": "United States",
    "silicon valley": "United States",
    "seattle": "United States", "chicago": "United States",
    "austin": "United States", "dallas": "United States",
    "houston": "United States", "denver": "United States",
    "boise": "United States", "spokane": "United States",
    "salinas": "United States", "rio rancho": "United States",
    "lecanto": "United States", "beaufort": "United States",
    "nampa": "United States", "eden prairie": "United States",
    "grand prairie": "United States", "gadsden": "United States",
    "laredo": "United States", "auburn": "United States",
    "boston": "United States", "atlanta": "United States",
    "miami": "United States", "washington": "United States",
    "washington dc": "United States", "dc": "United States",
    "phoenix": "United States", "san diego": "United States",
    "minneapolis": "United States", "portland": "United States",
    "charlotte": "United States", "nashville": "United States",
    "raleigh": "United States", "pittsburgh": "United States",
    "detroit": "United States", "philadelphia": "United States",
    "columbus": "United States", "indianapolis": "United States",
    "san jose": "United States", "salt lake city": "United States",
    "kansas city": "United States", "orlando": "United States",
    "tampa": "United States", "las vegas": "United States",
    "st louis": "United States", "saint louis": "United States",
    "baltimore": "United States", "richmond": "United States",
    "research triangle": "United States",
    "greater new york": "United States", "greater boston": "United States",
    "greater chicago": "United States", "greater seattle": "United States",
    "greater atlanta": "United States", "greater dallas": "United States",
    "greater denver": "United States", "greater washington": "United States",
    "greater philadelphia": "United States", "greater houston": "United States",
    "greater los angeles": "United States",
    # Canada
    "toronto": "Canada", "greater toronto": "Canada", "gta": "Canada",
    "vancouver": "Canada", "calgary": "Canada", "edmonton": "Canada",
    "montreal": "Canada", "ottawa": "Canada", "winnipeg": "Canada",
    "quebec city": "Canada", "hamilton": "Canada", "kitchener": "Canada",
    # United Kingdom
    "london": "United Kingdom", "greater london": "United Kingdom",
    "manchester": "United Kingdom", "birmingham": "United Kingdom",
    "leeds": "United Kingdom", "glasgow": "United Kingdom",
    "edinburgh": "United Kingdom", "bristol": "United Kingdom",
    "liverpool": "United Kingdom", "sheffield": "United Kingdom",
    "cardiff": "United Kingdom", "belfast": "United Kingdom",
    "cambridge": "United Kingdom", "oxford": "United Kingdom",
    "reading": "United Kingdom", "coventry": "United Kingdom",
    # India — IT hubs
    "bangalore": "India", "bengaluru": "India",
    "hyderabad": "India", "mumbai": "India", "bombay": "India",
    "pune": "India", "chennai": "India", "madras": "India",
    "delhi": "India", "new delhi": "India", "ncr": "India",
    "kolkata": "India", "ahmedabad": "India", "noida": "India",
    "gurgaon": "India", "gurugram": "India", "indore": "India",
    "coimbatore": "India",
    # Germany
    "berlin": "Germany", "munich": "Germany", "münchen": "Germany",
    "hamburg": "Germany", "frankfurt": "Germany", "cologne": "Germany",
    "köln": "Germany", "düsseldorf": "Germany", "stuttgart": "Germany",
    # Netherlands
    "amsterdam": "Netherlands", "rotterdam": "Netherlands",
    "the hague": "Netherlands", "utrecht": "Netherlands",
    # France
    "paris": "France", "lyon": "France", "marseille": "France",
    # Ireland
    "dublin": "Ireland",
    # Sweden
    "stockholm": "Sweden", "gothenburg": "Sweden",
    # Singapore
    "singapore": "Singapore",
    # Australia
    "sydney": "Australia", "melbourne": "Australia",
    "brisbane": "Australia", "perth": "Australia", "adelaide": "Australia",
    # Poland
    "warsaw": "Poland", "krakow": "Poland", "kraków": "Poland",
    "wroclaw": "Poland", "wrocław": "Poland",
    # Israel
    "tel aviv": "Israel", "tel-aviv": "Israel",
    # UAE
    "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates",
    # Spain / Mexico — keep key backlog cities even when geonamescache is not installed.
    "madrid": "Spain",
    "barcelona": "Spain",
    "tijuana": "Mexico",
    "mexico city": "Mexico",
    # Brazil
    "são paulo": "Brazil", "sao paulo": "Brazil",
    "rio de janeiro": "Brazil",
}


# ── Global city → country gazetteer (geonamescache, fully offline) ───────────
# The curated dict above owns aliases/overrides ("nyc", "bay area", "bangalore").
# geonamescache fills in thousands more real cities so international locations
# (Madrid→Spain, Tijuana→Mexico) resolve with no paid API and no network call.
_MIN_CITY_POP = 15000
# Common English words that are also city names — excluded to avoid false hits
# (e.g. a "Mobile" or "Industry" location string is almost never the city).
_AMBIGUOUS_CITY_NAMES = {
    "mobile", "industry", "commerce", "sale", "reading", "remote", "global",
    "international", "national", "central", "north", "south", "east", "west",
    "data", "sales", "general", "the city", "city", "metro", "county", "of",
}


def _fold_accents(text: str) -> str:
    """São Paulo → Sao Paulo, Kraków → Krakow (so ASCII input matches)."""
    import unicodedata
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "")
        if not unicodedata.combining(ch)
    )


def _build_global_city_country() -> dict[str, str]:
    """city-name(lower, accent-folded + raw) → full country name. Empty if the
    library is unavailable (degrades gracefully to the curated dict)."""
    try:
        import geonamescache
    except Exception:
        return {}
    try:
        gc = geonamescache.GeonamesCache()
        countries = gc.get_countries()  # iso2 → {"name": ...}
        best: dict[str, tuple[str, int]] = {}  # city key → (iso2, population)
        for c in gc.get_cities().values():
            pop = c.get("population") or 0
            if pop < _MIN_CITY_POP:
                continue
            cc = c.get("countrycode") or ""
            if cc not in countries:
                continue
            name = (c.get("name") or "").strip()
            for variant in {name.lower(), _fold_accents(name).lower()}:
                variant = variant.strip()
                if not variant or variant in _AMBIGUOUS_CITY_NAMES:
                    continue
                cur = best.get(variant)
                if cur is None or pop > cur[1]:
                    best[variant] = (cc, pop)
        return {key: countries[cc]["name"] for key, (cc, _pop) in best.items()}
    except Exception:
        return {}


# Curated aliases/overrides win on any conflict; global fills the rest.
_CURATED_CITY_COUNTRY = _CITY_COUNTRY
_CITY_COUNTRY = {**_build_global_city_country(), **_CURATED_CITY_COUNTRY}


# Regions — NOT countries
_REGIONS = {
    "apac": "APAC",
    "asia pacific": "APAC",
    "emea": "EMEA",
    "europe middle east africa": "EMEA",
    "latam": "LATAM",
    "latin america": "LATAM",
    "worldwide": "Worldwide",
    "global": "Worldwide",
    "anywhere": "Worldwide",
    "international": "Worldwide",
}

# Remote patterns
_REMOTE_PAT = re.compile(
    r"\b(remote|work from home|wfh|fully remote|100%\s*remote|distributed team|telecommute)\b",
    re.I,
)

# "Remote (US only)" / "Remote - EST" / "Remote, US" / "Remote / USA"
# No closing delimiter required — just grabs what follows the separator
_REMOTE_COUNTRY_PAT = re.compile(
    r"\bremote\b[^a-z0-9]*[\(\-–,/]\s*([a-zA-Z][a-zA-Z0-9 ,\.]*?)(?:\)|$|\s*only\b|\s*-|\s*–)",
    re.I,
)

# Currency signals → country hint
# NOTE: EUR is NOT mapped to Germany — euro is used in 20 countries.
#       £/GBP is unambiguous (UK only).
_CURRENCY_COUNTRY = [
    (re.compile(r"£\s*\d|gbp", re.I), "United Kingdom"),
    (re.compile(r"\baud\b|a\$\s*\d", re.I), "Australia"),
    (re.compile(r"\bcad\b|c\$\s*\d", re.I), "Canada"),
    (re.compile(r"\binr\b|₹\s*\d|lpa\b|lakh", re.I), "India"),
    (re.compile(r"\bsgd\b|s\$\s*\d", re.I), "Singapore"),
    (re.compile(r"\bnzd\b|nz\$\s*\d", re.I), "New Zealand"),
    (re.compile(r"\bchf\b", re.I), "Switzerland"),
    (re.compile(r"\bzar\b|r\s*\d{3,}", re.I), "South Africa"),
]

# EUR → Europe region (not a country assignment, used separately)
_EUR_PAT = re.compile(r"€\s*\d|(?<!\w)eur(?!\w)", re.I)

# Visa / right-to-work signals → country
_VISA_COUNTRY = [
    (re.compile(r"\bh[-\s]?1b\b|opt\b|stem opt\b|ead\b|green card\b|us citizen", re.I), "United States"),
    (re.compile(r"right to work in the uk|uk visa|uk work permit|british citizen|tier 2", re.I), "United Kingdom"),
    (re.compile(r"canadian citizen|pr canada|work permit canada", re.I), "Canada"),
    (re.compile(r"australian citizen|aus work permit|457 visa|482 visa", re.I), "Australia"),
    (re.compile(r"indian citizen|valid indian passport", re.I), "India"),
]

# Region keywords compiled for word-boundary matching (used on location only)
_REGION_PATS = [
    (re.compile(r"\b" + re.escape(kw) + r"\b", re.I), region)
    for kw, region in _REGIONS.items()
]

_COUNTRY_HINTS = {
    "us": "United States",
    "u.s.": "United States",
    "usa": "United States",
    "u.s.a.": "United States",
    "united states": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "gb": "United Kingdom",
    "great britain": "United Kingdom",
    "united kingdom": "United Kingdom",
    "in": "India",
    "india": "India",
    "ca": "Canada",
    "canada": "Canada",
    "au": "Australia",
    "australia": "Australia",
    "de": "Germany",
    "germany": "Germany",
    "es": "Spain",
    "spain": "Spain",
    "mx": "Mexico",
    "mexico": "Mexico",
    "pl": "Poland",
    "poland": "Poland",
    "il": "Israel",
    "israel": "Israel",
}


def _try_country_converter(text: str) -> str | None:
    """Use country-converter to resolve a location string. Returns full country name or None."""
    key = re.sub(r"\s+", " ", (text or "").strip().lower())
    if key in _COUNTRY_HINTS:
        return _COUNTRY_HINTS[key]
    try:
        import country_converter as coco  # type: ignore
        result = coco.convert(names=[text], to="name_short", not_found=None)
        if isinstance(result, list):
            result = result[0] if result else None
        if result and result != "not found":
            return str(result)
    except Exception:
        pass
    return None


def _city_lookup(text: str) -> str | None:
    """Check the city → country table. Handles 'Greater X'/'X Area' prefixes and
    accented spellings (São Paulo → sao paulo)."""
    raw = text.lower().strip()
    for key in (raw, _fold_accents(raw)):
        if key in _CITY_COUNTRY:
            return _CITY_COUNTRY[key]
        # Strip common prefixes/suffixes: "Greater Seattle" → "seattle"
        key2 = re.sub(r"^greater\s+", "", key)
        key2 = re.sub(r"\s+area$", "", key2)
        key2 = re.sub(r"\s+metro$", "", key2)
        key2 = re.sub(r"\s+region$", "", key2)
        if key2 != key and key2 in _CITY_COUNTRY:
            return _CITY_COUNTRY[key2]
    return None


def _scan_text_for_country(text: str) -> str | None:
    """Scan location text for a country name — tries each comma segment."""
    try:
        parts = [p.strip() for p in text.split(",")]
        for part in reversed(parts):
            if len(part) > 2:
                # City lookup first (faster, no external call)
                c = _city_lookup(part)
                if c:
                    return c
                r = _try_country_converter(part)
                if r:
                    return r
    except Exception:
        pass
    return None


def _extract_country_from_remote_location(loc: str, ttl: str) -> str | None:
    """
    Try to extract country/state from a remote location string.
    Handles: "Remote - US", "Remote (US only)", "Remote, Chicago IL",
             "Remote - EST", "US Remote", "Remote / USA"
    """
    # Pattern 1: "Remote <sep> <country/state>" with optional closing
    m = _REMOTE_COUNTRY_PAT.search(loc) or _REMOTE_COUNTRY_PAT.search(ttl)
    if m:
        hint = m.group(1).strip().rstrip(".")
        resolved = _try_country_converter(hint) or _city_lookup(hint)
        if resolved:
            return resolved
        if hint.upper() in _US_STATES:
            return "United States"
        if hint.upper() in _CA_PROVINCES:
            return "Canada"

    # Pattern 2: state/country abbreviation anywhere in the location
    # e.g. "Remote - Chicago, IL" → find "IL"
    for part in re.split(r"[\s,\(\)\-–/]+", loc):
        up = part.upper()
        if len(up) == 2 and up.isalpha():
            if up in _US_STATES:
                return "United States"
            if up in _CA_PROVINCES:
                return "Canada"

    # Pattern 3: "US Remote" / "USA Remote" — country before the word remote
    m2 = re.search(r"^([a-z]{2,15})\s+remote\b", loc.strip(), re.I)
    if m2:
        hint = m2.group(1).strip()
        resolved = _try_country_converter(hint) or _city_lookup(hint)
        if resolved:
            return resolved

    return None


def detect_country(location: str, title: str = "", description: str = "") -> tuple[str, str]:
    """
    Returns (country, region).
    country: full name like "United States" | "Remote" | "Multiple" | ""
    region:  "APAC" | "EMEA" | "LATAM" | "Worldwide" | "Remote" | ""
    """
    loc = (location or "").strip()
    ttl = (title or "").strip()
    desc_raw = (description or "")[:500]
    desc = strip_html(desc_raw)

    # NOTE: region keyword check uses loc only (not title) to avoid false positives
    # like "Global Account Manager" or "International Corp" triggering Worldwide.
    loc_lower = loc.lower()
    combined_loc_title = f"{loc} {ttl}".lower()

    # ── Tier 0: Remote detection ──────────────────────────────────────────────
    if _REMOTE_PAT.search(combined_loc_title):
        country_hint = _extract_country_from_remote_location(loc, ttl)
        if country_hint:
            return country_hint, "Remote"
        # Check region keywords in location only
        for pat, region_name in _REGION_PATS:
            if pat.search(loc_lower):
                return "Multiple", region_name
        # No country clue in location/title — try description for country
        if desc:
            desc_country = _scan_text_for_country(desc[:300])
            if desc_country and desc_country not in ("Remote",):
                return desc_country, "Remote"
            for pat, country in _VISA_COUNTRY:
                if pat.search(desc):
                    return country, "Remote"
        return "Remote", "Remote"

    # ── Tier 1: Region keywords in location only (NOT title) ─────────────────
    for pat, region_name in _REGION_PATS:
        if pat.search(loc_lower):
            return "Multiple", region_name

    # ── Tier 2: country-converter on full location ────────────────────────────
    if loc:
        r = _try_country_converter(loc)
        if r:
            return r, ""

        # City lookup + comma-segment scan
        r = _scan_text_for_country(loc)
        if r:
            return r, ""

        # 2-char abbreviation: US state → USA, CA province → Canada
        parts = [p.strip() for p in re.split(r"[\s,]+", loc)]
        for part in reversed(parts):
            if len(part) == 2 and part.isalpha():
                up = part.upper()
                if up in _US_STATES:
                    return "United States", ""
                if up in _CA_PROVINCES:
                    return "Canada", ""

    # ── Tier 3: Title scan ────────────────────────────────────────────────────
    if ttl:
        r = _scan_text_for_country(ttl)
        if r:
            return r, ""

    # ── Tier 4: Currency signals in description ───────────────────────────────
    for pat, country in _CURRENCY_COUNTRY:
        if pat.search(desc):
            return country, ""

    # EUR signals Europe but not a specific country — use as region hint only
    if _EUR_PAT.search(desc):
        return "Multiple", "EMEA"

    # ── Tier 4b: Visa/right-to-work signals ──────────────────────────────────
    for pat, country in _VISA_COUNTRY:
        if pat.search(desc):
            return country, ""

    # ── Tier 5: Scan description[:500] for country names ─────────────────────
    if desc:
        r = _scan_text_for_country(desc[:500])
        if r:
            return r, ""

    return "", ""

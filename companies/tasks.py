from celery import shared_task
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import csv
import io
import json
import re
import ssl

from django.utils import timezone

from .models import Company
from .services import normalize_company_name, normalize_domain


# ─── Company-type keyword classifier (zero LLM cost) ─────────────────────────

# Words that strongly suggest each company type.
# Scored: first match wins; ties broken by order (product > service > consultancy > staffing).
_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("product", [
        "saas", "software as a service", "cloud platform", "our platform", "our product",
        "b2b software", "enterprise software", "developer tool", "devtool",
        "api platform", "our app", "mobile app", "web application",
        "product-led", "product company", "product-first",
        "open source", "open-source", "oss", "freemium",
        "marketplace", "e-commerce", "ecommerce", "cloud-native",
    ]),
    ("service", [
        "managed services", "it services", "technology services", "professional services",
        "systems integrator", "system integrator", "digital services",
        "outsourcing", "offshore", "nearshore", "it solutions",
        "service provider", "technology partner", "implementation partner",
        "we deliver services", "we provide services",
    ]),
    ("consultancy", [
        "consulting firm", "consultancy", "advisory", "advisors",
        "management consulting", "strategy consulting", "business consulting",
        "digital transformation", "change management", "trusted advisor",
        "we advise", "our consultants", "our advisors",
    ]),
    ("staffing", [
        "staffing", "staff augmentation", "talent solutions", "talent acquisition",
        "body shop", "it staffing", "recruiting", "recruitment agency",
        "we place", "we connect talent", "hire engineers", "hire developers",
        "contract staffing", "contingent workforce",
    ]),
]

# Industry → keyword sets (all lowercase, partial match against description)
_INDUSTRY_RULES: list[tuple[str, list[str]]] = [
    ("Cloud Infrastructure",  ["cloud infrastructure", "cloud platform", "aws", "azure", "gcp", "kubernetes", "cloud computing"]),
    ("Cybersecurity",         ["cybersecurity", "security platform", "threat detection", "endpoint security", "soc", "siem"]),
    ("Financial Services",    ["fintech", "financial services", "banking", "insurance", "payments", "trading", "investment"]),
    ("Healthcare",            ["healthcare", "health tech", "medical", "pharma", "biotech", "clinical", "ehr", "emr"]),
    ("E-commerce / Retail",   ["e-commerce", "ecommerce", "retail", "shopping", "marketplace", "fulfillment"]),
    ("Data & Analytics",      ["data analytics", "business intelligence", "data platform", "ml platform", "machine learning", "ai platform"]),
    ("Enterprise Software",   ["erp", "crm", "hrms", "hcm", "scm", "supply chain", "enterprise resource"]),
    ("DevOps / Dev Tools",    ["devops", "developer tool", "devtool", "ci/cd", "devsecops", "code review", "observability"]),
    ("IT Services",           ["it services", "managed services", "outsourcing", "staff augmentation", "staffing"]),
    ("Logistics",             ["logistics", "supply chain", "freight", "shipping", "last-mile", "fleet"]),
    ("Education / EdTech",    ["edtech", "education", "learning platform", "lms", "e-learning", "university"]),
    ("Media / Entertainment", ["media", "entertainment", "streaming", "content platform", "gaming", "music"]),
    ("Telecom",               ["telecom", "telecommunications", "carrier", "5g", "network services", "isp"]),
]

# Headcount range → size band mapping
_SIZE_MAP: list[tuple[str, str]] = [
    ("1-10",       "startup"),
    ("1-50",       "startup"),
    ("11-50",      "startup"),
    ("51-200",     "small"),
    ("201-500",    "medium"),
    ("501-1,000",  "medium"),
    ("1,001-5,000","large"),
    ("5,001+",     "enterprise"),
    ("10,001+",    "enterprise"),
]


def _classify_company_type(text: str) -> str:
    """
    Keyword-based company type classifier.
    Scans description/about text (lowercased). Returns CompanyType value.
    Zero LLM cost — pure heuristic.
    """
    if not text:
        return "unknown"
    low = text.lower()
    scores: dict[str, int] = {}
    for ctype, keywords in _TYPE_RULES:
        for kw in keywords:
            if kw in low:
                scores[ctype] = scores.get(ctype, 0) + 1
    if not scores:
        return "unknown"
    return max(scores, key=lambda k: scores[k])


def _classify_industry(text: str) -> str:
    """Keyword-based industry classifier. Returns best-matching industry label or ''."""
    if not text:
        return ""
    low = text.lower()
    best_label, best_count = "", 0
    for label, keywords in _INDUSTRY_RULES:
        count = sum(1 for kw in keywords if kw in low)
        if count > best_count:
            best_count = count
            best_label = label
    return best_label


def _headcount_to_size_band(headcount: str) -> str:
    """Map '51-200' → 'small' etc. Returns '' if no match."""
    if not headcount:
        return ""
    for pattern, band in _SIZE_MAP:
        if pattern.lower() in headcount.lower() or headcount.lower() in pattern.lower():
            return band
    # Try parsing raw numbers
    m = re.search(r'(\d[\d,]*)', headcount.replace(",", ""))
    if m:
        n = int(m.group(1).replace(",", ""))
        if n <= 50:    return "startup"
        if n <= 200:   return "small"
        if n <= 1000:  return "medium"
        if n <= 5000:  return "large"
        return "enterprise"
    return ""


# ─── Free DuckDuckGo Instant Answer discovery ────────────────────────────────

def _fetch_ddg_instant(query: str) -> dict:
    """
    Free DuckDuckGo Instant Answer API — no key, no rate limit on reasonable use.
    Returns dict with keys: abstract, abstract_url, website (from Results[0]).
    """
    out: dict = {}
    if not query:
        return out
    try:
        params = urlencode({
            "q": query,
            "format": "json",
            "no_redirect": "1",
            "no_html": "1",
            "skip_disambig": "1",
        })
        url = f"https://api.duckduckgo.com/?{params}"
        ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        resp = urlopen(req, context=ctx, timeout=8)
        data = json.loads(resp.read().decode("utf-8"))

        if data.get("Abstract"):
            out["abstract"] = data["Abstract"]
        if data.get("AbstractURL"):
            out["abstract_url"] = data["AbstractURL"]
        if data.get("AbstractSource"):
            out["source"] = data["AbstractSource"]

        # Official website from Results
        for result in data.get("Results") or []:
            first_url = result.get("FirstURL") or ""
            if first_url and "wikipedia" not in first_url.lower():
                out["website"] = first_url
                break

        # Infobox: headcount, industry, location
        for item in (data.get("Infobox") or {}).get("content", []):
            label = (item.get("label") or "").lower()
            value = item.get("value") or ""
            if "employee" in label or "headcount" in label:
                out["headcount_range"] = str(value)
            elif "industry" in label or "sector" in label:
                out["industry"] = str(value)
            elif "headquarters" in label or "location" in label:
                out["hq_location"] = str(value)
            elif "founded" in label:
                out["founded"] = str(value)
            elif "type" in label and "company" in label:
                out["org_type"] = str(value)

    except Exception:
        pass
    return out


def _search_ddg_for_website(company_name: str) -> str:
    """
    Search DuckDuckGo for '<company_name> official website' and return first non-wiki URL.
    Used when the company has no website stored yet.
    """
    try:
        params = urlencode({
            "q": f"{company_name} official website",
            "format": "json",
            "no_redirect": "1",
            "no_html": "1",
            "skip_disambig": "1",
        })
        url = f"https://api.duckduckgo.com/?{params}"
        ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        resp = urlopen(req, context=ctx, timeout=8)
        data = json.loads(resp.read().decode("utf-8"))

        for result in data.get("Results") or []:
            u = result.get("FirstURL") or ""
            if u and "wikipedia" not in u.lower() and "duckduckgo" not in u.lower():
                return u

        # Fall back to AbstractURL
        if data.get("AbstractURL") and "wikipedia" not in (data.get("AbstractURL") or "").lower():
            return data["AbstractURL"]

    except Exception:
        pass
    return ""


def _log_pipeline_run(task_name: str, result: dict) -> None:
    """Record pipeline task run for Settings UI."""
    try:
        from core.models import PipelineRunLog

        obj, _ = PipelineRunLog.objects.update_or_create(
            task_name=task_name,
            defaults={"last_run_at": timezone.now(), "last_run_result": result},
        )
    except Exception:
        pass


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    if not urlparse(url).scheme:
        url = "https://" + url
    return url


def _apply_link_validation(company: Company) -> None:
    """HEAD/GET check for website + LinkedIn; updates *_is_valid and *_last_checked_at."""
    now = timezone.now()
    if company.website:
        company.website = _normalize_url(company.website)
        company.website_is_valid = _check_url(company.website)
        company.website_last_checked_at = now
    if company.linkedin_url:
        company.linkedin_url = _normalize_url(company.linkedin_url)
        if "linkedin.com/company/" in company.linkedin_url.lower():
            company.linkedin_is_valid = _check_url(company.linkedin_url)
        else:
            company.linkedin_is_valid = False
        company.linkedin_last_checked_at = now


def _check_url(url: str) -> bool:
    if not url:
        return False
    url = _normalize_url(url)
    try:
        ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "GoCareers-link-checker/1.0"})
        # Use HEAD if possible; fallback to GET
        req.get_method = lambda: "HEAD"
        try:
            resp = urlopen(req, context=ctx, timeout=5)
        except Exception:
            # Retry with GET
            req.get_method = lambda: "GET"
            resp = urlopen(req, context=ctx, timeout=5)
        status = getattr(resp, "status", None) or getattr(resp, "code", None)
        return status is None or (200 <= int(status) < 400)
    except Exception:
        return False


@shared_task
def validate_company_links_task(batch_size: int = 50):
    """
    Periodically validate Company.website and linkedin_url.
    Only checks companies that have never been checked or were last checked >24h ago.
    """
    now = timezone.now()
    cutoff = now - timezone.timedelta(hours=24)

    qs = Company.objects.all()
    qs = qs.filter(
        website__isnull=False
    ) | qs.filter(
        linkedin_url__isnull=False
    )
    qs = qs.filter(
        website_last_checked_at__lt=cutoff
    ) | qs.filter(
        website_last_checked_at__isnull=True
    ) | qs.filter(
        linkedin_last_checked_at__lt=cutoff
    ) | qs.filter(
        linkedin_last_checked_at__isnull=True
    )

    processed = 0
    for company in qs[:batch_size]:
        changed = False
        if company.website:
            company.website = _normalize_url(company.website)
            company.website_is_valid = _check_url(company.website)
            company.website_last_checked_at = now
            changed = True
        if company.linkedin_url:
            company.linkedin_url = _normalize_url(company.linkedin_url)
            # Simple LinkedIn company URL pattern
            if "linkedin.com/company/" in company.linkedin_url:
                company.linkedin_is_valid = _check_url(company.linkedin_url)
            else:
                company.linkedin_is_valid = False
            company.linkedin_last_checked_at = now
            changed = True
        if changed:
            company.save(update_fields=[
                "website",
                "website_is_valid",
                "website_last_checked_at",
                "linkedin_url",
                "linkedin_is_valid",
                "linkedin_last_checked_at",
            ])
            processed += 1

    result = {"processed": processed}
    _log_pipeline_run("validate_company_links", result)
    return result


@shared_task
def import_companies_from_csv_task(csv_bytes: bytes) -> dict:
    """
    Bulk import companies from a CSV.
    Expected columns: name, website, linkedin_url, industry, alias, size_band, hq_location.
    """
    created = 0
    updated = 0
    skipped_duplicates = 0
    f = io.StringIO(csv_bytes.decode("utf-8", errors="ignore"))
    reader = csv.DictReader(f)
    for row in reader:
        raw_name = (row.get("name") or "").strip()
        if not raw_name:
            continue
        name = normalize_company_name(raw_name)
        website = (row.get("website") or "").strip()
        linkedin_url = (row.get("linkedin_url") or "").strip()
        industry = (row.get("industry") or "").strip()
        alias = (row.get("alias") or "").strip()
        size_band = (row.get("size_band") or "").strip()
        hq_location = (row.get("hq_location") or "").strip()
        domain = normalize_domain(website) if website else ""

        existing = None
        if domain:
            existing = Company.objects.filter(domain=domain).first()
        if not existing:
            existing = Company.objects.filter(name__iexact=name).first()

        if existing:
            # Update a few safe fields
            updated += 1
            if website and not existing.website:
                existing.website = website
            if linkedin_url and not existing.linkedin_url:
                existing.linkedin_url = linkedin_url
            if industry and not existing.industry:
                existing.industry = industry
            if alias and not existing.alias:
                existing.alias = alias
            if size_band and not existing.size_band:
                existing.size_band = size_band
            if hq_location and not existing.hq_location:
                existing.hq_location = hq_location
            if domain and not existing.domain:
                existing.domain = domain
            existing.save()
            continue

        Company.objects.create(
            name=name,
            alias=alias,
            website=website,
            domain=domain,
            linkedin_url=linkedin_url,
            industry=industry,
            size_band=size_band,
            hq_location=hq_location,
        )
        created += 1

    return {
        "created": created,
        "updated": updated,
        "skipped_duplicates": skipped_duplicates,
    }


@shared_task
def import_companies_from_domains_task(text: str) -> dict:
    """
    Import companies from a newline-separated list of domains/URLs.
    """
    created = 0
    existing = 0
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        domain = normalize_domain(raw)
        if not domain:
            continue
        obj, was_created = Company.objects.get_or_create(
            domain=domain,
            defaults={
                "name": domain,
                "website": "https://" + domain,
            },
        )
        if was_created:
            created += 1
        else:
            existing += 1
    return {"created": created, "existing": existing}


@shared_task
def import_companies_from_linkedin_task(text: str) -> dict:
    """
    Import companies from a newline-separated list of LinkedIn company URLs.
    """
    created = 0
    existing = 0
    invalid = 0
    for line in text.splitlines():
        url = (line or "").strip()
        if not url:
            continue
        if "linkedin.com/company/" not in url:
            invalid += 1
            continue
        # Normalize LinkedIn URL a bit
        if not urlparse(url).scheme:
            url = "https://" + url
        qs = Company.objects.filter(linkedin_url__iexact=url)
        if qs.exists():
            existing += 1
            continue
        # Use slug as a fallback name
        slug = url.split("linkedin.com/company/")[-1].strip("/").replace("-", " ").title() or url
        Company.objects.create(
            name=slug,
            linkedin_url=url,
        )
        created += 1
    return {"created": created, "existing": existing, "invalid": invalid}


def _extract_domain_for_enrichment(company: Company) -> str:
    """Domain for Clearbit/OG: company.domain or derived from website."""
    if company.domain and company.domain.strip():
        return company.domain.strip().lower()
    if company.website:
        return normalize_domain(company.website)
    return ""


def _fetch_clearbit_logo(domain: str) -> str | None:
    """Return Clearbit logo URL if 200, else None."""
    if not domain:
        return None
    url = f"https://logo.clearbit.com/{domain}"
    try:
        ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        req.get_method = lambda: "HEAD"
        resp = urlopen(req, context=ctx, timeout=5)
        status = getattr(resp, "status", None) or getattr(resp, "code", None)
        if status == 200:
            return url
    except (HTTPError, URLError, OSError):
        pass
    return None


def _fetch_og_meta(page_url: str, max_bytes: int = 100_000) -> dict:
    """
    Fetch HTML and extract og:title, og:description, og:image, meta name=description, <title>.
    Returns dict with keys: title, description, image (values may be empty).
    """
    out = {"title": "", "description": "", "image": ""}
    page_url = _normalize_url(page_url)
    if not page_url:
        return out
    try:
        ctx = ssl.create_default_context()
        req = Request(page_url, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        resp = urlopen(req, context=ctx, timeout=10)
        raw = resp.read(max_bytes).decode("utf-8", errors="ignore")
    except Exception:
        return out

    # og:title
    m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']*)["\']', raw, re.I)
    if m:
        out["title"] = m.group(1).strip()
    if not out["title"]:
        m = re.search(r"<title[^>]*>([^<]+)</title>", raw, re.I)
        if m:
            out["title"] = m.group(1).strip()

    # og:description then meta name=description
    m = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']*)["\']', raw, re.I)
    if m:
        out["description"] = m.group(1).strip()
    if not out["description"]:
        m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']', raw, re.I)
        if m:
            out["description"] = m.group(1).strip()

    # og:image
    m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']*)["\']', raw, re.I)
    if m:
        out["image"] = m.group(1).strip()
    # For career / LinkedIn discovery on company homepage (trim for memory)
    out["_raw_html"] = raw[:250000]
    return out


def _resolve_google_kg_api_key(config) -> str:
    """Platform Config field wins; else Django GOOGLE_KG_API_KEY from settings."""
    if config:
        k = (getattr(config, "google_kg_api_key", None) or "").strip()
        if k:
            return k
    try:
        from django.conf import settings

        return (getattr(settings, "GOOGLE_KG_API_KEY", None) or "").strip()
    except Exception:
        return ""


def _kg_image_url(image_field) -> str:
    if not image_field:
        return ""
    if isinstance(image_field, dict):
        return (image_field.get("contentUrl") or image_field.get("url") or "").strip()
    if isinstance(image_field, str):
        return image_field.strip()
    return ""


def _kg_location_string(result: dict) -> str:
    """Best-effort HQ string from Knowledge Graph location / address."""
    loc = result.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc.strip()[:255]
    if not isinstance(loc, dict):
        return ""
    addr = loc.get("address")
    if isinstance(addr, dict):
        parts = [
            addr.get("streetAddress"),
            addr.get("addressLocality"),
            addr.get("addressRegion"),
            addr.get("addressCountry"),
        ]
        line = ", ".join(str(p).strip() for p in parts if p)
        if line:
            return line[:255]
    name = loc.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:255]
    return ""


def _fetch_google_kg(api_key: str, query: str) -> dict:
    """
    Google Knowledge Graph Search API — Organization-focused.
    Returns: name, description (long), image, url (website), linkedin_url, alias, hq_location,
    industry_label (short KG description line, often sector-like).
    """
    out: dict = {}
    if not api_key or not (query or "").strip():
        return out
    q = (query or "").strip()[:200]
    try:
        params = urlencode(
            {
                "query": q,
                "key": api_key,
                "types": "Organization",
                "limit": "10",
            }
        )
        url = f"https://kgsearch.googleapis.com/v1/entities:search?{params}"
        req = Request(url, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        resp = urlopen(req, timeout=12)
        data = json.loads(resp.read().decode("utf-8"))
        items = data.get("itemListElement") or []
        if not items:
            return out

        best_result = None
        best_score = -1.0
        for el in items:
            res = el.get("result") or {}
            if not res:
                continue
            score = float(el.get("resultScore") or 0)
            types = res.get("@type") or []
            if isinstance(types, str):
                types = [types]
            if types and not any(t in ("Organization", "Corporation", "Company") for t in types):
                continue
            if score >= best_score:
                best_score = score
                best_result = res

        if not best_result and items:
            best_result = (items[0].get("result") or {})

        result = best_result or {}
        if result.get("name"):
            out["name"] = result["name"]

        dd = result.get("detailedDescription") or {}
        body = (dd.get("articleBody") or "").strip()
        short = (result.get("description") or "").strip()
        if body:
            out["description"] = body[:8000]
        elif short:
            out["description"] = short[:8000]

        if short:
            out["industry_label"] = short[:255]

        img = _kg_image_url(result.get("image"))
        if img:
            out["image"] = img

        if result.get("url"):
            out["url"] = (result["url"] or "").strip()

        same_as = result.get("sameAs") or []
        if isinstance(same_as, str):
            same_as = [same_as]
        for u in same_as:
            if not u:
                continue
            low = u.lower()
            if "linkedin.com/company/" in low:
                out["linkedin_url"] = u.split("?")[0]
                break

        an = result.get("alternateName")
        if isinstance(an, list) and an:
            out["alias"] = str(an[0])[:255]
        elif isinstance(an, str) and an.strip():
            out["alias"] = an.strip()[:255]

        hq = _kg_location_string(result)
        if hq:
            out["hq_location"] = hq

        return out
    except Exception:
        return out


def merge_google_kg_dicts(a: dict, b: dict) -> dict:
    """Merge two KG result dicts; prefer longer description and any missing fields."""
    if not a:
        return dict(b or {})
    if not b:
        return dict(a or {})
    m = dict(a)
    for k, v in (b or {}).items():
        if v is None or v == "":
            continue
        if k == "description":
            prev = (m.get("description") or "")
            nv = str(v)
            if len(nv) > len(prev):
                m["description"] = nv
        elif k not in m or m[k] in ("", None):
            m[k] = v
    return m


def _fetch_hunter(api_key: str, domain: str) -> dict:
    """Fetch company info from Hunter.io. Returns dict with name, description, industry, logo, location, etc."""
    out = {}
    if not api_key or not domain:
        return out
    try:
        params = urlencode({"domain": domain, "api_key": api_key})
        url = f"https://api.hunter.io/v2/companies/find?{params}"
        req = Request(url, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        d = data.get("data") or {}
        if d.get("name"):
            out["name"] = d["name"]
        if d.get("description"):
            out["description"] = (d["description"][:2000] + "…") if len(d["description"]) > 2000 else d["description"]
        cat = d.get("category") or {}
        if cat.get("industry"):
            out["industry"] = cat["industry"]
        if d.get("logo"):
            out["logo"] = d["logo"]
        if d.get("location"):
            out["hq_location"] = d["location"]
        metrics = d.get("metrics") or {}
        if metrics.get("employees"):
            out["headcount_range"] = metrics["employees"]
        if d.get("phone"):
            out["primary_contact_phone"] = d["phone"]
        return out
    except Exception:
        return out


def _fetch_apollo(api_key: str, domain: str) -> dict:
    """Fetch organization from Apollo.io by domain. Returns dict with industry, size, etc."""
    out = {}
    if not api_key or not domain:
        return out
    try:
        params = urlencode({"domain": domain})
        url = f"https://api.apollo.io/api/v1/organizations/enrich?{params}"
        req = Request(
            url,
            headers={
                "User-Agent": "GoCareers-enrichment/1.0",
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        org = data.get("organization") or data.get("data") or {}
        if org.get("name"):
            out["name"] = org["name"]
        if org.get("short_description") or org.get("description"):
            desc = org.get("short_description") or org.get("description") or ""
            out["description"] = (desc[:2000] + "…") if len(desc) > 2000 else desc
        if org.get("industry"):
            out["industry"] = org["industry"]
        if org.get("estimated_num_employees"):
            out["headcount_range"] = str(org["estimated_num_employees"])
        if org.get("primary_domain"):
            out["domain"] = org["primary_domain"]
        return out
    except Exception:
        return out


def _log_enrichment(company_id: int, source: str, fields_updated: list, success: bool) -> None:
    """Create an EnrichmentLog entry for this run."""
    try:
        from .models import EnrichmentLog

        EnrichmentLog.objects.create(
            company_id=company_id,
            source=source,
            fields_updated={"fields": fields_updated},
            success=success,
        )
    except Exception:
        pass


def _compute_data_quality_score(company: Company) -> int:
    """Simple 0–100 score from filled fields."""
    fields = [
        bool(company.name),
        bool(company.domain),
        bool(company.website),
        bool(company.logo_url),
        bool(company.description),
        bool(company.industry),
        bool(company.size_band or company.headcount_range),
        bool(company.hq_location),
        bool(company.linkedin_url),
        bool(company.career_site_url),
        bool(company.alias),
        bool(company.relationship_status),
    ]
    n = len(fields)
    return min(100, sum(fields) * 100 // n) if n else 0


@shared_task
def enrich_company_task(company_id: int) -> dict:
    """
    Enrich a company: Clearbit logo + OG/meta scrape from website.
    Sets enrichment_status, enriched_at, enrichment_source, data_quality_score, description, logo_url.
    """
    try:
        company = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        return {"ok": False, "reason": "company_not_found"}

    now = timezone.now()
    try:
        from .enrichment_helpers import apply_free_enrichment

        _, src_tags = apply_free_enrichment(company)

        domain = _extract_domain_for_enrichment(company)
        if not domain and not company.website:
            company.enrichment_status = Company.EnrichmentStatus.FAILED
            company.enriched_at = now
            company.enrichment_source = ""
            company.data_quality_score = _compute_data_quality_score(company)
            company.save(update_fields=[
                "enrichment_status", "enriched_at", "enrichment_source", "data_quality_score",
            ])
            _log_enrichment(company.pk, "", [], success=False)
            return {"ok": False, "reason": "no_domain_or_website"}

        sources = list(src_tags)
        updates: dict = {}

        try:
            config = __import__("core.models", fromlist=["PlatformConfig"]).PlatformConfig.load()
        except Exception:
            config = None
        domain = _extract_domain_for_enrichment(company)
        if config:
            hunter_key = (getattr(config, "hunter_api_key", None) or "").strip()
            if hunter_key and domain:
                h_data = _fetch_hunter(hunter_key, domain)
                if h_data and "hunter" not in sources:
                    sources.append("hunter")
                for k, v in h_data.items():
                    if v and not getattr(company, k, None) and k not in updates:
                        updates[k] = v
                if h_data.get("logo") and not company.logo_url:
                    updates["logo_url"] = h_data["logo"]

            apollo_key = (getattr(config, "apollo_api_key", None) or "").strip()
            if apollo_key and domain:
                a_data = _fetch_apollo(apollo_key, domain)
                if a_data and "apollo" not in sources:
                    sources.append("apollo")
                for k, v in a_data.items():
                    if v and not getattr(company, k, None) and k not in updates:
                        updates[k] = v

        classify_text = " ".join(filter(None, [
            updates.get("description") or company.description,
            updates.get("industry") or company.industry,
            company.name,
        ]))
        if classify_text.strip():
            if company.company_type in ("unknown", "", None):
                detected_type = _classify_company_type(classify_text)
                if detected_type != "unknown":
                    updates["company_type"] = detected_type
            if not (updates.get("industry") or company.industry):
                detected_industry = _classify_industry(classify_text)
                if detected_industry:
                    updates["industry"] = detected_industry
        hc = updates.get("headcount_range") or company.headcount_range
        if hc and not company.size_band:
            sb = _headcount_to_size_band(hc)
            if sb:
                updates["size_band"] = sb

        if updates:
            for k, v in updates.items():
                if hasattr(company, k):
                    setattr(company, k, v)

        _apply_link_validation(company)

        company.enrichment_status = Company.EnrichmentStatus.ENRICHED
        company.enriched_at = now
        company.enrichment_source = "+".join(sources) if sources else "none"
        company.data_quality_score = _compute_data_quality_score(company)
        company.save()
        _log_enrichment(company.pk, company.enrichment_source, list(updates.keys()), success=True)
        return {"ok": True, "enrichment_source": company.enrichment_source}
    except Exception:
        company.enrichment_status = Company.EnrichmentStatus.FAILED
        company.enriched_at = now
        company.enrichment_source = ""
        company.data_quality_score = _compute_data_quality_score(company)
        company.save(update_fields=[
            "enrichment_status", "enriched_at", "enrichment_source", "data_quality_score", "updated_at",
        ])
        _log_enrichment(company.pk, "", [], success=False)
        raise


@shared_task
def re_enrich_stale_companies_task(stale_days: int = 30) -> dict:
    """
    Queue enrich_company_task for companies enriched more than stale_days ago (or status=stale).
    Celery Beat runs this every 30 days.
    """
    from django.db.models import Q

    cutoff = timezone.now() - timezone.timedelta(days=stale_days)
    stale_ids = list(
        Company.objects.filter(
            Q(enrichment_status=Company.EnrichmentStatus.ENRICHED, enriched_at__lt=cutoff)
            | Q(enrichment_status=Company.EnrichmentStatus.STALE)
        ).values_list("pk", flat=True)
    )
    for pk in stale_ids:
        enrich_company_task.delay(pk)
    result = {"queued": len(stale_ids)}
    _log_pipeline_run("re_enrich_stale", result)
    return result


@shared_task
def full_re_enrich_companies_task() -> dict:
    """
    Queue enrich_company_task for all companies. Celery Beat runs this every 90 days (optional).
    """
    ids = list(Company.objects.values_list("pk", flat=True))
    for pk in ids:
        enrich_company_task.delay(pk)
    result = {"queued": len(ids)}
    _log_pipeline_run("full_re_enrich", result)
    return result


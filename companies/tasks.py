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


def _check_url(url: str) -> bool:
    if not url:
        return False
    url = _normalize_url(url)
    try:
        ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "CHENN-link-checker/1.0"})
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
        req = Request(url, headers={"User-Agent": "CHENN-enrichment/1.0"})
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
        req = Request(page_url, headers={"User-Agent": "CHENN-enrichment/1.0"})
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
    return out


def _fetch_google_kg(api_key: str, query: str) -> dict:
    """Fetch company info from Google Knowledge Graph. Returns dict with name, description, image, url."""
    out = {}
    if not api_key or not query:
        return out
    try:
        params = urlencode({"query": query[:100], "key": api_key, "types": "Organization", "limit": 1})
        url = f"https://kgsearch.googleapis.com/v1/entities:search?{params}"
        req = Request(url, headers={"User-Agent": "CHENN-enrichment/1.0"})
        resp = urlopen(req, timeout=8)
        data = json.loads(resp.read().decode("utf-8"))
        items = data.get("itemListElement") or []
        if not items:
            return out
        result = items[0].get("result") or {}
        if result.get("name"):
            out["name"] = result["name"]
        desc = result.get("description") or (result.get("detailedDescription") or {}).get("articleBody")
        if desc:
            out["description"] = (desc[:2000] + "…") if len(desc) > 2000 else desc
        img = result.get("image") or {}
        if isinstance(img, dict) and img.get("contentUrl"):
            out["image"] = img["contentUrl"]
        elif isinstance(img, str):
            out["image"] = img
        if result.get("url"):
            out["url"] = result["url"]
        return out
    except Exception:
        return out


def _fetch_hunter(api_key: str, domain: str) -> dict:
    """Fetch company info from Hunter.io. Returns dict with name, description, industry, logo, location, etc."""
    out = {}
    if not api_key or not domain:
        return out
    try:
        params = urlencode({"domain": domain, "api_key": api_key})
        url = f"https://api.hunter.io/v2/companies/find?{params}"
        req = Request(url, headers={"User-Agent": "CHENN-enrichment/1.0"})
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
                "User-Agent": "CHENN-enrichment/1.0",
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
    ]
    return min(100, sum(fields) * 100 // 9)


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

        sources = []
        updates = {}

        # Clearbit logo
        logo_url = _fetch_clearbit_logo(domain)
        if logo_url:
            updates["logo_url"] = logo_url
            sources.append("clearbit")

        # OG / meta from website
        page_url = company.website or (f"https://{domain}" if domain else "")
        if page_url:
            og = _fetch_og_meta(page_url)
            if og["description"]:
                updates["description"] = (og["description"][:2000] + "…") if len(og["description"]) > 2000 else og["description"]
                if "og" not in sources:
                    sources.append("og")
            if og["image"] and not updates.get("logo_url"):
                updates["logo_url"] = og["image"]
                if "og" not in sources:
                    sources.append("og")

        # Optional APIs (Phase 4)
        try:
            config = __import__("core.models", fromlist=["PlatformConfig"]).PlatformConfig.load()
        except Exception:
            config = None
        if config:
            # Google Knowledge Graph (by name or domain)
            kg_key = (getattr(config, "google_kg_api_key", None) or "").strip()
            if kg_key:
                kg_query = company.name or domain
                if kg_query:
                    kg_data = _fetch_google_kg(kg_key, kg_query)
                    if kg_data and "kg" not in sources:
                        sources.append("kg")
                    if kg_data.get("description") and not updates.get("description"):
                        updates["description"] = kg_data["description"]
                    if kg_data.get("image") and not updates.get("logo_url"):
                        updates["logo_url"] = kg_data["image"]
                    if kg_data.get("name") and not company.name:
                        updates["name"] = kg_data["name"]

            # Hunter (by domain)
            hunter_key = (getattr(config, "hunter_api_key", None) or "").strip()
            if hunter_key and domain:
                h_data = _fetch_hunter(hunter_key, domain)
                if h_data and "hunter" not in sources:
                    sources.append("hunter")
                for k, v in h_data.items():
                    if v and not getattr(company, k, None) and k not in updates:
                        updates[k] = v
                if h_data.get("logo") and not updates.get("logo_url"):
                    updates["logo_url"] = h_data["logo"]

            # Apollo (by domain)
            apollo_key = (getattr(config, "apollo_api_key", None) or "").strip()
            if apollo_key and domain:
                a_data = _fetch_apollo(apollo_key, domain)
                if a_data and "apollo" not in sources:
                    sources.append("apollo")
                for k, v in a_data.items():
                    if v and not getattr(company, k, None) and k not in updates:
                        updates[k] = v

        company.enrichment_status = Company.EnrichmentStatus.ENRICHED
        company.enriched_at = now
        company.enrichment_source = "+".join(sources) if sources else "none"
        if updates:
            for k, v in updates.items():
                if hasattr(company, k):
                    setattr(company, k, v)
        company.data_quality_score = _compute_data_quality_score(company)
        update_fields = ["enrichment_status", "enriched_at", "enrichment_source", "data_quality_score", "updated_at"]
        valid_fields = {f.name for f in Company._meta.get_fields() if hasattr(f, "name")}
        for k in updates:
            if k in valid_fields:
                update_fields.append(k)
        company.save(update_fields=list(dict.fromkeys(update_fields)))
        _log_enrichment(company.pk, "+".join(sources) if sources else "none", list(updates.keys()), success=True)
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


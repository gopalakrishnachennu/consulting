"""
Shared free company enrichment (no Celery): DDG, Wikipedia REST, Clearbit logo,
OG/meta + homepage link discovery (careers, LinkedIn). Used by quick-fill and enrich task.
Lazy-imports `.tasks` inside `apply_free_enrichment` to avoid circular imports.
"""
from __future__ import annotations

import json
import re
import ssl
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from .models import Company
from .services import normalize_domain


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if not url:
        return ""
    if not urlparse(url).scheme:
        url = "https://" + url
    return url


def _title_from_wikipedia_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"wikipedia\.org/wiki/([^?#]+)", url, re.I)
    if not m:
        return ""
    from urllib.parse import unquote

    return unquote(m.group(1).replace("_", " "))


def fetch_wikipedia_rest_summary(wiki_url_or_title: str) -> dict:
    """
    Free Wikipedia REST summary (longer extract + thumbnail).
    Pass full AbstractURL or page title.
    """
    out: dict = {}
    title = ""
    if "wikipedia.org" in (wiki_url_or_title or "").lower():
        title = _title_from_wikipedia_url(wiki_url_or_title)
    else:
        title = (wiki_url_or_title or "").strip()
    if not title:
        return out
    enc = title.replace(" ", "_")
    safe = quote(enc, safe="_")
    api = f"https://en.wikipedia.org/api/rest_v1/page/summary/{safe}"
    try:
        ctx = ssl.create_default_context()
        req = Request(api, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        resp = urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("extract"):
            out["extract"] = data["extract"]
        if isinstance(data.get("thumbnail"), dict) and data["thumbnail"].get("source"):
            out["thumbnail"] = data["thumbnail"]["source"]
        if data.get("description"):
            out["wikidata_desc"] = data["description"]
    except Exception:
        pass
    return out


def search_ddg_for_linkedin_company(company_name: str) -> str:
    """Find first linkedin.com/company/ URL from DDG instant results."""
    if not company_name:
        return ""
    try:
        params = urlencode(
            {
                "q": f"{company_name} site:linkedin.com/company",
                "format": "json",
                "no_redirect": "1",
                "no_html": "1",
                "skip_disambig": "1",
            }
        )
        url = f"https://api.duckduckgo.com/?{params}"
        ctx = ssl.create_default_context()
        req = Request(url, headers={"User-Agent": "GoCareers-enrichment/1.0"})
        resp = urlopen(req, context=ctx, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        for result in data.get("Results") or []:
            u = (result.get("FirstURL") or "").strip()
            if "linkedin.com/company/" in u.lower():
                return _normalize_url(u.split("?")[0])
        for t in data.get("RelatedTopics") or []:
            if isinstance(t, dict):
                u = (t.get("FirstURL") or "").strip()
                if "linkedin.com/company/" in u.lower():
                    return _normalize_url(u.split("?")[0])
    except Exception:
        pass
    return ""


def _parse_homepage_for_career_and_linkedin(html: str, base_url: str) -> dict:
    """Scan anchor hrefs for careers/jobs pages and LinkedIn company URLs."""
    out = {"career_url": "", "linkedin_url": ""}
    if not html or not base_url:
        return out
    base = _normalize_url(base_url)
    try:
        host = urlparse(base).netloc.lower()
    except Exception:
        host = ""
    career_score = -1
    career_patterns = (
        ("/careers", 4),
        ("/jobs", 3),
        ("careers.", 2),
        ("jobs.", 2),
        ("/join", 1),
        ("/opportunities", 1),
    )
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
        href = (m.group(1) or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            continue
        full = urljoin(base, href)
        low = full.lower()
        if "linkedin.com/company/" in low and not out["linkedin_url"]:
            out["linkedin_url"] = _normalize_url(full.split("?")[0])
            continue
        try:
            link_host = urlparse(full).netloc.lower()
        except Exception:
            continue
        if link_host != host and not link_host.endswith("." + host):
            continue
        score = 0
        for pat, sc in career_patterns:
            if pat in low:
                score = max(score, sc)
        if score > career_score:
            career_score = score
            out["career_url"] = _normalize_url(full.split("?")[0])
    return out


def apply_google_kg_enrichment(company: Company, t) -> tuple[list[str], list[str]]:
    """
    Fill Company fields from Google Knowledge Graph when GOOGLE_KG_API_KEY or Platform Config key is set.
    Merges results from a name query and a domain query when domain is known.
    """
    filled: list[str] = []
    sources: list[str] = []
    try:
        config = __import__("core.models", fromlist=["PlatformConfig"]).PlatformConfig.load()
    except Exception:
        config = None
    kg_key = t._resolve_google_kg_api_key(config)
    if not kg_key or not (company.name or "").strip():
        return filled, sources

    kg = t._fetch_google_kg(kg_key, company.name.strip())
    domain_hint = (t._extract_domain_for_enrichment(company) or "").strip()
    if domain_hint:
        kg2 = t._fetch_google_kg(kg_key, domain_hint)
        kg = t.merge_google_kg_dicts(kg, kg2)
    if not kg:
        return filled, sources

    if kg.get("url") and not company.website:
        company.website = _normalize_url(kg["url"])
        d = normalize_domain(company.website)
        if d:
            company.domain = d
        filled.append("website (Knowledge Graph)")
    if kg.get("image") and not company.logo_url:
        company.logo_url = kg["image"]
        filled.append("logo (Knowledge Graph)")
    desc = kg.get("description") or ""
    if desc and len(desc) > len(company.description or ""):
        company.description = desc[:8000]
        filled.append("description (Knowledge Graph)")
    if kg.get("industry_label") and not company.industry:
        company.industry = kg["industry_label"][:255]
        filled.append("industry (Knowledge Graph)")
    if kg.get("linkedin_url") and not company.linkedin_url:
        company.linkedin_url = kg["linkedin_url"]
        filled.append("LinkedIn (Knowledge Graph)")
    if kg.get("alias") and not company.alias:
        company.alias = kg["alias"][:255]
        filled.append("alias (Knowledge Graph)")
    if kg.get("hq_location") and not company.hq_location:
        company.hq_location = kg["hq_location"][:255]
        filled.append("HQ (Knowledge Graph)")
    sources.append("kg")
    return filled, sources


def apply_free_enrichment(company: Company) -> tuple[list[str], list[str]]:
    """
    Fill empty Company fields using free sources. Mutates company in memory.
    Returns (filled_messages, source_tags).
    """
    from . import tasks as t

    filled: list[str] = []
    sources: list[str] = []

    domain = t._extract_domain_for_enrichment(company)

    # 1) Website discovery
    if not company.website and not domain and company.name:
        found_url = t._search_ddg_for_website(company.name)
        if found_url:
            company.website = found_url
            domain = normalize_domain(found_url) or domain
            if domain:
                company.domain = domain
            filled.append(f"website ({found_url})")
            sources.append("ddg-search")

    # 1.5) Google Knowledge Graph (optional: Platform Config or GOOGLE_KG_API_KEY)
    kg_filled, kg_sources = apply_google_kg_enrichment(company, t)
    filled.extend(kg_filled)
    sources.extend(kg_sources)
    domain = t._extract_domain_for_enrichment(company)

    # 2) DDG instant (Wikipedia infobox)
    ddg: dict = {}
    if company.name:
        ddg = t._fetch_ddg_instant(company.name)
        if ddg:
            sources.append("ddg")
        if ddg.get("abstract") and not company.description:
            company.description = ddg["abstract"][:8000]
            filled.append("description (DDG)")
        if ddg.get("website") and not company.website:
            company.website = ddg["website"]
            d = normalize_domain(ddg["website"])
            if d:
                company.domain = d
                domain = d
            filled.append(f"website ({ddg['website']})")
        if ddg.get("headcount_range") and not company.headcount_range:
            company.headcount_range = ddg["headcount_range"]
            filled.append(f"headcount ({ddg['headcount_range']})")
        if ddg.get("industry") and not company.industry:
            company.industry = ddg["industry"]
            filled.append(f"industry ({ddg['industry']})")
        if ddg.get("hq_location") and not company.hq_location:
            company.hq_location = ddg["hq_location"]
            filled.append(f"HQ ({ddg['hq_location']})")

        # 3) Wikipedia REST — longer extract + optional thumb
        wiki_src = ddg.get("abstract_url") or ""
        wiki = fetch_wikipedia_rest_summary(wiki_src) if wiki_src else {}
        if not wiki.get("extract") and company.name:
            wiki = fetch_wikipedia_rest_summary(company.name)
        if wiki.get("extract"):
            ex = wiki["extract"]
            if len(ex) > len(company.description or ""):
                company.description = ex[:8000]
                filled.append("description (Wikipedia)")
                sources.append("wikipedia")
            if wiki.get("thumbnail") and not company.logo_url:
                company.logo_url = wiki["thumbnail"]
                filled.append("logo (Wikipedia)")
        if wiki.get("wikidata_desc") and not company.alias:
            wd = (wiki["wikidata_desc"] or "").strip()
            if wd:
                company.alias = wd[:120]
                filled.append("alias (Wikipedia short desc)")

    # 4) Clearbit logo
    domain = t._extract_domain_for_enrichment(company)
    if domain and not company.logo_url:
        logo = t._fetch_clearbit_logo(domain)
        if logo:
            company.logo_url = logo
            filled.append("logo (Clearbit)")
            sources.append("clearbit")

    # 5) Homepage: OG + careers + LinkedIn from same HTML
    page_url = company.website or (f"https://{domain}" if domain else "")
    if page_url:
        og = t._fetch_og_meta(page_url)
        if og.get("description") and not company.description:
            company.description = (og["description"][:8000]) if len(og["description"]) <= 8000 else og["description"][:8000]
            filled.append("description (site meta)")
            sources.append("og")
        if og.get("image") and not company.logo_url:
            company.logo_url = og["image"]
            filled.append("logo (OG)")
        raw = og.get("_raw_html") or ""
        if raw:
            extras = _parse_homepage_for_career_and_linkedin(raw, page_url)
            if extras.get("linkedin_url") and not company.linkedin_url:
                company.linkedin_url = extras["linkedin_url"]
                filled.append("LinkedIn (homepage)")
            if extras.get("career_url") and not company.career_site_url:
                company.career_site_url = extras["career_url"]
                filled.append("career site (homepage)")
        if og.get("title") and not company.alias:
            short = (og["title"] or "").split("|")[0].strip()
            if short and short.lower() != (company.name or "").lower():
                company.alias = short[:255]
                filled.append("alias (page title)")

    # 6) DDG LinkedIn if still missing
    if company.name and not company.linkedin_url:
        li = search_ddg_for_linkedin_company(company.name)
        if li:
            company.linkedin_url = li
            filled.append("LinkedIn (web search)")
            sources.append("ddg-linkedin")

    # 7) Default career URL pattern from domain
    if domain and not company.career_site_url:
        for path in ("/careers", "/jobs", "/careers/", "/jobs/"):
            guess = _normalize_url(f"https://{domain}{path}")
            # Light probe — only set if HEAD-ish works (use tasks._check_url)
            if t._check_url(guess):
                company.career_site_url = guess
                filled.append(f"career site ({path})")
                break

    # 8) Classifiers
    classify_text = " ".join(filter(None, [company.description, company.industry, company.name]))
    if classify_text.strip():
        if company.company_type in ("unknown", "", None):
            ct = t._classify_company_type(classify_text)
            if ct != "unknown":
                company.company_type = ct
                filled.append(f"company type ({ct})")
        if not company.industry:
            ind = t._classify_industry(classify_text)
            if ind:
                company.industry = ind
                filled.append(f"industry ({ind})")

    hc = company.headcount_range
    if hc and not company.size_band:
        sb = t._headcount_to_size_band(hc)
        if sb:
            company.size_band = sb
            filled.append(f"size band ({sb})")

    # 9) Relationship placeholder for CRM (only if empty)
    if not (company.relationship_status or "").strip():
        company.relationship_status = "Prospect"
        filled.append("relationship status (Prospect)")

    return filled, sources

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def compute_url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()


def strip_html(html: str) -> str:
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html).strip()


def extract_salary(raw: str) -> tuple[Optional[float], Optional[float], str]:
    if not raw:
        return None, None, "USD"

    currency = "USD"
    if "£" in raw:
        currency = "GBP"
    elif "€" in raw:
        currency = "EUR"

    nums = re.findall(r"[\d,]+(?:\.\d+)?[kK]?", raw)
    parsed = []
    for n in nums:
        n = n.replace(",", "")
        try:
            if n.lower().endswith("k"):
                parsed.append(float(n[:-1]) * 1000)
            else:
                v = float(n)
                if v > 0:
                    parsed.append(v)
        except ValueError:
            pass

    if len(parsed) >= 2:
        return min(parsed), max(parsed), currency
    elif len(parsed) == 1:
        return parsed[0], parsed[0], currency
    return None, None, currency


def detect_remote(text: str) -> Optional[bool]:
    if not text:
        return None
    lower = text.lower()
    if any(k in lower for k in ["remote", "work from home", "wfh", "anywhere", "distributed"]):
        return True
    if any(k in lower for k in ["on-site", "onsite", "in-office", "on site"]):
        return False
    return None


def normalize_job_data(
    raw_job: dict[str, Any],
    platform,
    company,
    harvest_run,
) -> dict[str, Any]:
    """Convert raw harvester output dict to HarvestedJob field values."""
    from .models import HarvestedJob

    original_url = raw_job.get("original_url", "").strip()
    url_hash = compute_url_hash(original_url) if original_url else ""

    title = raw_job.get("title", "").strip()
    company_name = raw_job.get("company_name", company.name if company else "").strip()
    location = raw_job.get("location", "").strip()

    salary_raw = raw_job.get("salary_raw", "")
    sal_min, sal_max, currency = extract_salary(salary_raw)

    is_remote = raw_job.get("is_remote")
    if is_remote is None:
        is_remote = detect_remote(location) or detect_remote(title)

    description_html = raw_job.get("description_html", "")
    description_text = raw_job.get("description_text", "") or strip_html(description_html)

    valid_types = {c[0] for c in HarvestedJob.JobType.choices}
    job_type = raw_job.get("job_type", "UNKNOWN")
    if job_type not in valid_types:
        job_type = "UNKNOWN"

    expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=24)

    posted_date = None
    posted_raw = raw_job.get("posted_date_raw", "")
    if posted_raw:
        try:
            if "T" in posted_raw or "+" in posted_raw or "Z" in posted_raw:
                posted_date = datetime.fromisoformat(
                    posted_raw.replace("Z", "+00:00")
                ).date()
        except Exception:
            pass

    return {
        "harvest_run": harvest_run,
        "company": company,
        "platform": platform,
        "external_id": str(raw_job.get("external_id", ""))[:500],
        "url_hash": url_hash,
        "original_url": original_url[:1000],
        "title": title[:300],
        "company_name": company_name[:255],
        "location": location[:255],
        "is_remote": is_remote,
        "job_type": job_type,
        "department": str(raw_job.get("department", ""))[:255],
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_currency": currency,
        "salary_raw": salary_raw[:200],
        "description_html": description_html,
        "description_text": description_text[:50000],
        "requirements_text": raw_job.get("requirements_text", ""),
        "benefits_text": raw_job.get("benefits_text", ""),
        "posted_date": posted_date,
        "expires_at": expires_at,
        "is_active": True,
        "sync_status": "PENDING",
        "raw_payload": raw_job.get("raw_payload", {}),
    }

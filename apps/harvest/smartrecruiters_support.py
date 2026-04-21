"""
SmartRecruiters — shared URL + id helpers.

Root cause (bulk jobs + backfill):
  The public REST API uses **case-sensitive** paths:
  ``/v1/companies/{identifier}/postings/{id}``.
  Company labels sometimes store a tenant slug that does not exactly match the API
  ``company.identifier`` returned on each posting. Using the wrong slug → HTTP 400/404,
  empty JD, and backfill that "never works" for those rows.

  The list payload includes ``company.identifier`` per posting — that is the
  authoritative slug for detail calls. We persist it on ``raw_payload`` and use it
  when building fetch URLs for backfill.
"""
from __future__ import annotations

from .jarvis import _smartrecruiters_normalize_posting_id


def backfill_fetch_url_for_raw_job(job) -> str:
    """
    Prefer a canonical detail API URL built from ``raw_payload`` + ``external_id``.

    Falls back to ``original_url`` when identifiers are missing (manual/Jarvis rows).
    """
    original = (getattr(job, "original_url", None) or "").strip()
    payload = getattr(job, "raw_payload", None)
    if not isinstance(payload, dict):
        payload = {}

    co = payload.get("company")
    ident = ""
    if isinstance(co, dict):
        ident = (co.get("identifier") or "").strip()

    eid = str(getattr(job, "external_id", "") or payload.get("id") or "").strip()
    posting_id = _smartrecruiters_normalize_posting_id(eid) if eid else ""

    if ident and posting_id:
        return (
            f"https://api.smartrecruiters.com/v1/companies/{ident}/postings/{posting_id}"
        )
    return original

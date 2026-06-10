"""
Deterministic truth guardrails for generated resumes.

Runs AFTER the LLM writes the resume and checks the output against the candidate's
REAL data — independent of which model produced it. This is what makes swapping
generation models (gpt-4o, deepseek-chat, …) safe: the prompt can be ignored by a
model, but these code checks can't.

Returns {status: pass|review|block, errors: [...], warnings: [...]}.
- errors  → hard, unambiguous fabrication (BLOCK): unknown employer, JD company as
            employer, invented certification.
- warnings→ soft / fuzzy (REVIEW): a metric not found in the source, missing heading.
"""
import re


def _norm(s: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy identity matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def run_guardrails(content: str, consultant, jd_intel: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if not content or not content.strip():
        return {"status": "pass", "errors": [], "warnings": []}

    # ── Candidate's REAL data ────────────────────────────────────────
    try:
        real_companies = [e.company for e in consultant.experience.all() if e.company]
    except Exception:
        real_companies = []
    real_company_norms = {_norm(c) for c in real_companies if _norm(c)}

    base = consultant.base_resume_text or ""
    base_norm = _norm(base)

    try:
        real_certs = [c.name for c in consultant.certifications.all()]
    except Exception:
        real_certs = []
    real_cert_norms = {_norm(c) for c in real_certs if _norm(c)}

    jd_company = (jd_intel.get("company") or "").strip()
    jd_company_norm = _norm(jd_company)

    # ── Parse the generated resume back into structure (same parser as the editor) ──
    from ..parser import parse_resume
    parsed = parse_resume(content)
    gen_exp = parsed.get("experience", [])
    gen_employers = [e.get("company", "") for e in gen_exp if e.get("company")]
    gen_certs = parsed.get("certifications", [])

    def _known_company(emp_norm: str) -> bool:
        if not emp_norm:
            return True
        if any(emp_norm == rc or emp_norm in rc or rc in emp_norm for rc in real_company_norms):
            return True
        return emp_norm in base_norm  # employer mentioned in the pasted base resume

    # 1. Employers must be real (BLOCK)
    for emp in gen_employers:
        if not _known_company(_norm(emp)):
            errors.append(f"Employer “{emp}” is not in the candidate's real work history.")

    # 2. JD/target company must not appear as an employer (BLOCK) unless it really is one
    if jd_company_norm:
        for emp in gen_employers:
            if _norm(emp) == jd_company_norm and jd_company_norm not in real_company_norms:
                errors.append(
                    f"The target job's company “{jd_company}” appears as an employer — "
                    f"the candidate does not work there."
                )
                break

    # 3. Certifications must be real (BLOCK)
    for cert in gen_certs:
        cn = _norm(cert)
        if not cn:
            continue
        if not (any(cn in rc or rc in cn for rc in real_cert_norms) or cn in base_norm):
            errors.append(f"Certification “{cert[:60]}” is not in the candidate's real certifications.")

    # 4. Percentage metrics not present in the source (REVIEW — fuzzy)
    src_numbers = set(re.findall(r"\d+(?:\.\d+)?", base))
    for e in gen_exp:
        for bl in e.get("bullets", []):
            for num in re.findall(r"(\d+(?:\.\d+)?)\s?%", bl):
                if num not in src_numbers and len(warnings) < 12:
                    warnings.append(f"Metric “{num}%” isn't in the source data — verify: \"{bl[:70]}…\"")

    # 5. Required headings present (REVIEW)
    up = content.upper()
    if "PROFESSIONAL SUMMARY" not in up and "SUMMARY" not in up:
        warnings.append("Missing a Professional Summary heading.")
    if "EXPERIENCE" not in up:
        warnings.append("Missing a Professional Experience heading.")

    # de-dupe + cap
    errors = list(dict.fromkeys(errors))[:20]
    warnings = list(dict.fromkeys(warnings))[:20]
    status = "block" if errors else ("review" if warnings else "pass")
    return {"status": status, "errors": errors, "warnings": warnings}

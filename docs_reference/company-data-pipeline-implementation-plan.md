# Company Data Ingestion Pipeline — Implementation Plan

This document is the **concrete implementation plan** for a corporate-grade company data pipeline. It maps the 7-layer pipeline to our existing architecture and defines phased deliverables. Nothing from the target design is dropped unless we already have it.

---

## 1. Current State (What We Already Have)

### 1.1 Models & storage
- **Company** model with: `name`, `alias`, `website`, `career_site_url`, `linkedin_url`, `logo_url`, `industry`, `size_band`, `headcount_range`, `hq_location`, `locations`, `relationship_status`, `primary_contact_*`, `notes`, totals (submissions/interviews/offers/placements), `last_activity_at`, `website_last_checked_at`, `website_is_valid`, `linkedin_last_checked_at`, `linkedin_is_valid`, `is_blacklisted`, `blacklist_reason`.
- **CompanyDoNotSubmit** for consultant-level DND.
- **Job** has `company` (legacy text) and `company_obj` FK to Company; job URL fields: `original_link_last_checked_at`, `original_link_is_live`, `possibly_filled`.

### 1.2 Deduplication
- **companies.services**: `find_potential_duplicate_companies(name, website, threshold, limit)` using Jaccard name + domain match.
- **Admin**: On Company create, warning if potential duplicates found. **No merge queue, no Merge/Keep Separate UI.**

### 1.3 Validation & refresh
- **companies.tasks**: `validate_company_links_task` — checks `website` and `linkedin_url`, normalizes URL, sets `*_is_valid`, `*_last_checked_at`. LinkedIn pattern check for `linkedin.com/company/`.
- **jobs.tasks**: `validate_job_urls_task` — re-checks `Job.original_link`, sets `original_link_is_live`, `possibly_filled`.
- **Celery Beat** (apps/core/signals.py): Company link validator daily 03:00; Job URL validator daily 04:00.

### 1.4 Ingestion channels
- **Manual**: Company create/edit form; job form has “Company profile” dropdown + “Manage companies”. **No typeahead/fuzzy search on job form.**
- **Auto from job**: Job form can set `company_obj`; no automatic “create company from job company string” pipeline step.
- **No**: CSV bulk import for companies, LinkedIn URL list import, domain list upload, or public API for company ingestion.

### 1.5 Normalization
- **URL**: `companies.tasks._normalize_url` (add https if missing). **No** domain-only storage, no strip-www/path for a canonical `domain` field.
- **Name**: **No** canonical name normalization (strip Inc/LLC/Corp, title-case, alias mapping) on input.

### 1.6 Enrichment
- **None**: No Clearbit, OG scraping, Wikipedia, Knowledge Graph, Apollo, Hunter. No `enrichment_status`, `enriched_at`, `enrichment_source`, or `data_quality_score`.

### 1.7 Settings / UI
- **Settings**: Platform config (branding, IMAP, etc.); LLM; Help. **No** “Data Pipeline” or “Company Ingestion” section.
- **Company UI**: List (filters, sort, pagination, export, preview), detail (timeline, jobs, link health), edit/create. **No** duplicate review queue UI, no enrichment status dashboard.

---

## 2. Gap Matrix (Pipeline Layer vs Current State)

| Layer | Have | Missing |
|-------|------|--------|
| **1. Raw input** | Manual create; job form company dropdown | Typeahead/fuzzy on job form; CSV bulk import; LinkedIn URL import; domain list upload; API ingestion |
| **2. Normalize** | URL add-https in validator | Name: strip legal suffixes, title-case, alias map; Domain: canonical field, strip protocol/www/path |
| **3. Dedupe** | Fuzzy + domain in services; admin warning on create | Duplicate review queue (list + Merge/Keep Separate); merge logic (transfer jobs/subs); run normalization before dedupe |
| **4. Enrich** | — | enrichment_status, enriched_at, source, data_quality_score; Clearbit logo; OG meta; optional Knowledge Graph / Apollo; Celery task + rate limits |
| **5. Validate** | Company website/LinkedIn check; job URL 24h recheck | Optional: SSL check, redirect tracking; LinkedIn strict pattern; re-enrich if stale (after enrichment exists) |
| **6. Storage** | Single Company model with all fields | Optional: canonical_name vs name; domain unique index; separate CompanyProfile/CompanyLinks only if we outgrow single model — not required for Phase 1–4 |
| **7. Refresh** | Company links daily; job URLs daily | 7-day company re-validate; 30/90-day re-enrich (after enrichment); “Re-enrich stale” button |
| **Settings** | — | Data Pipeline section: Ingestion (CSV, domain list, API keys), Enrichment Status, Duplicate Queue, URL Validation summary, Enrichment Logs |

---

## 3. Architecture Principles

- **Django**: All company CRUD and pipeline triggers live in `companies` app (and core for settings). No new app unless we add a separate “enrichment” service.
- **Celery**: All heavy or external I/O (validation, enrichment, bulk import) are Celery tasks. Use `shared_task` and existing Celery Beat setup.
- **Company model**: Extend in place (new fields, indexes). Introduce separate models (e.g. CompanyProfile, EnrichmentLog) only when clearly needed.
- **Settings**: New “Data Pipeline” or “Company pipeline” section under existing Settings dashboard; new views/urls in `core` or under a dedicated `settings` namespace.
- **APIs**: If we add API ingestion, use Django REST Framework or a minimal JSON view with auth (e.g. token or session); document in this plan.

---

## 4. Implementation Phases

### Phase 1 — Normalization & deduplication (foundation)

**Goal:** Clean, consistent input and a visible duplicate review process. No new ingestion channels yet.

**1.1 Name normalization (companies.services + form/view)**  
- Add `normalize_company_name(raw: str) -> str`: strip legal suffixes (Inc, LLC, Corp, Ltd, Co., etc.), collapse whitespace, optional title-case.  
- Call it in CompanyForm.clean_name and in any place we set `Company.name` (create view, bulk import later).  
- **Optional:** Store normalized name in a separate field `name_normalized` for indexing (or keep deriving on the fly).

**1.2 Domain normalization and storage**  
- Add `Company.domain` (CharField, blank, unique=True, db_index=True). Max length 253.  
- Add `normalize_domain(url_or_domain: str) -> str` in companies.services: strip protocol, www, path, query; lowercase; store only registrable domain (e.g. `google.com`).  
- Data migration: backfill `domain` from existing `website` using `normalize_domain`.  
- On Company save, if `website` is set, set `domain = normalize_domain(website)`.  
- Use `domain` in `find_potential_duplicate_companies` as primary key for exact match.

**1.3 Duplicate review queue (UI + merge)**  
- New model: `CompanyDuplicateCandidate` (optional) or reuse “pending” state: store two company FKs or one “proposed” company name/domain and one “existing” company.  
- **Simpler approach:** No new model. New view: “Duplicate review” list that runs `find_potential_duplicate_companies` for all companies (or for recently added) and shows pairs above threshold.  
- **UI:** Settings → Data Pipeline → tab “Duplicate review”. List of pairs (name A, name B, domain A, domain B, score). Actions: [Merge] [Keep separate].  
- **Merge action:** Merge B into A: move all Job.company_obj pointing to B to A; move all ApplicationSubmission (via job), CompanyDoNotSubmit, etc. to A; delete B (or mark merged_into=A).  
- **Dedupe on create:** When creating a company (form or later bulk), after normalization run duplicate check; if high confidence, redirect to “possible duplicate” page with [Use existing] [Create anyway].

**Deliverables:**  
- Name normalization in services + form.  
- `Company.domain` + backfill + normalization on save.  
- Duplicate review queue view + merge logic.  
- Dedupe check on company create with “Use existing / Create anyway”.

---

### Phase 2 — Multiple ingestion channels

**Goal:** Companies enter the system via CSV, domain list, LinkedIn list, and (optional) API; all go through normalize → dedupe.

**2.1 CSV bulk import**  
- Settings → Data Pipeline → Company ingestion → “Bulk import (CSV)”.  
- Upload CSV with columns: name, website, linkedin_url, industry, (optional) alias, size_band, hq_location.  
- Parse CSV, for each row: normalize name and domain, run duplicate check; if duplicate, add to “skipped” list with reason; else create Company (or queue for approval).  
- Use Celery task for large files (e.g. >50 rows). Return report: created, skipped (duplicate), failed (validation errors).  
- **Duplicate handling:** “Create anyway” vs “Skip and link to existing” per row or globally.

**2.2 Domain list upload**  
- Same tab: “Import from domain list”. Textarea or file upload: one domain per line (e.g. `google.com`, `https://stripe.com`).  
- For each: `normalize_domain` → lookup by `Company.domain`; if exists, skip or count; else create Company with `website = https://<domain>`, `domain = domain`, `name = domain` (or leave name for enrichment).  
- Same Celery + report pattern as CSV.

**2.3 LinkedIn company URL list**  
- “Import from LinkedIn URLs”. Textarea: one URL per line.  
- Parse: extract `linkedin.com/company/<slug>`, validate pattern; for each slug, create or get company with `linkedin_url` set, `name = slug` or “Unknown” until enriched.  
- Duplicate check by linkedin_url or by domain if we later resolve domain from LinkedIn.

**2.4 Job form typeahead**  
- On job form, when user types in “Company name” or a company search box: AJAX endpoint that returns companies matching query (fuzzy or icontains) with `id`, `name`, `domain`.  
- User selects existing company → set `company_obj`, fill name; or “Add new” → open company create modal/redirect with return URL back to job form.

**2.5 API ingestion (optional)**  
- **Endpoint**: `POST /companies/api/create/` (session-authenticated; same permissions as company create: superuser, ADMIN, EMPLOYEE).  
- **Payload (JSON)**:  
  - Required: `name` (string)  
  - Optional: `website`, `alias`, `industry` (strings)  
- **Behaviour**:  
  - Normalize `name` via `normalize_company_name`.  
  - Derive `domain` from `website` via `normalize_domain` (if provided).  
  - Look up existing company by `domain` first, then by case-insensitive `name`.  
  - If found → return existing record; if not → create new `Company` and, if `auto_enrich_on_create` is enabled, queue `enrich_company_task`.  
- **Response**:  
  - `200 OK` when an existing company is returned.  
  - `201 Created` when a new company is created.  
  - Body shape for both:  
    `{ "id": <int>, "name": <str>, "domain": <str>, "website": <str>, "created": <bool> }`  
- **Errors**:  
  - `400` with `{ "error": "name is required" }` when `name` is missing/blank.  
  - `400` with `{ "error": "Invalid JSON" }` when the request body is not valid JSON.

**Deliverables:**  
- CSV bulk import (form + task + report).  
- Domain list import (form + task + report).  
- LinkedIn URL list import (form + task + report).  
- Job form company typeahead + “Add new” flow.  
- (Optional) Public or internal API for company create.

---

### Phase 3 — Enrichment (free tier first)

**Goal:** Auto-fill company data from external sources; track status and quality.

**3.1 Enrichment fields on Company**  
- Add: `enrichment_status` (choices: pending, enriched, failed, stale), `enriched_at` (DateTimeField, null), `enrichment_source` (CharField, blank), `data_quality_score` (0–100, default 0).  
- Optional: `description` (TextField) for scraped or API description.

**3.2 Clearbit logo**  
- In enrichment task: if `domain` present, `GET https://logo.clearbit.com/<domain>`. If 200, set `logo_url` to that URL. No API key.  
- Run in Celery task; rate limit (e.g. 1 req/sec) to avoid blocking.

**3.3 OG / meta scraping**  
- Fetch `website` URL; parse HTML; extract `<title>`, `og:title`, `og:description`, `meta[name=description]`, `og:image`.  
- Fill `description` (or a short summary), optionally `logo_url` from og:image if Clearbit failed.  
- Use requests + BeautifulSoup or minimal regex; run in Celery.

**3.4 Enrichment task and trigger**  
- `companies.tasks.enrich_company_task(company_id)`: load company; if no domain skip; call Clearbit logo; call OG scrape; set enrichment_status, enriched_at, enrichment_source, data_quality_score (simple heuristic: how many fields filled).  
- Trigger: (1) After company create (signal or in create view). (2) “Re-enrich” button on company detail. (3) Scheduled: “Re-enrich stale” (enriched_at older than 90 days).

**3.5 Enrichment status in Settings**  
- Settings → Data Pipeline → “Enrichment status”: counts (pending, enriched, failed, stale); “Re-enrich all stale” button (queues Celery task for each).  
- Optional: “Enrichment log” table (new model: company_id, source, timestamp, fields_updated, success/fail) for debugging.

**Deliverables:**  
- New fields on Company; migration.  
- Clearbit logo + OG scrape in `enrich_company_task`.  
- Trigger on create + Re-enrich button + “Re-enrich stale” batch.  
- Enrichment status tab and optional EnrichmentLog.

---

### Phase 4 — Enrichment tier 2 (optional APIs) & refresh schedule

**Goal:** Use Google Knowledge Graph and/or Apollo/Hunter when available; formalize refresh cadence.

**4.1 Google Knowledge Graph (optional)**  
- Add settings: `GOOGLE_KG_API_KEY` (or in PlatformConfig).  
- In `enrich_company_task`: if API key and name/domain, call Knowledge Graph API; map result to name, description, founding year, hq.  
- Rate limit and handle quota.

**4.2 Apollo / Hunter (optional)**  
- Settings: API keys for Apollo, Hunter.  
- Enrichment task: by domain, fetch company/contact data; fill industry, size, primary_contact_*.  
- Prefer free tier; document limits.

**4.3 Refresh pipeline (Celery Beat)**  
- Already have: Company links daily 03:00; Job URLs daily 04:00.  
- Add: “Re-validate company websites” every 7 days (same `validate_company_links_task`, already runs daily — optionally increase batch or run twice).  
- Add: “Re-enrich stale companies” every 30 days (crontab): enqueue `enrich_company_task` for companies where `enriched_at < 30 days ago` or `enrichment_status = stale`.  
- Add: Optional “Full re-enrich” every 90 days (same task, all companies).  
- All in `core.signals` or a single “pipeline schedule” that creates/updates PeriodicTasks.

**Deliverables:**  
- (Optional) Knowledge Graph + Apollo/Hunter integration in enrichment task.  
- Celery Beat: 30-day re-enrich stale; optional 90-day full re-enrich.  
- Settings UI for API keys and “Last run” for each pipeline step.

---

### Phase 5 — Settings UI: Data Pipeline dashboard

**Goal:** Single place to operate and monitor the pipeline.

**5.1 Settings → Data Pipeline**  
- New entry in Settings dashboard: “Data Pipeline” or “Company pipeline”.  
- **Tab 1 — Company ingestion:** Links to CSV upload, domain list, LinkedIn import (from Phase 2). Toggles: “Auto-enrich on create” (default True). Optional fields for API keys (Apollo, Hunter, Google KG).  
- **Tab 2 — Enrichment status:** Total companies; counts by enrichment_status; “Re-enrich all stale” button; link to Enrichment log (if implemented).  
- **Tab 3 — Duplicate review:** Link to duplicate queue view (Phase 1); show count of “pending review” if we store that.  
- **Tab 4 — URL validation:** Summary: last run of company link validator and job URL validator; counts of invalid company websites and “possibly filled” jobs; links to filtered lists.  
- **Tab 5 — Enrichment logs (optional):** Paginated list of EnrichmentLog or last N enrichment runs.

**Deliverables:**  
- Data Pipeline section with 4–5 tabs as above.  
- Reuse existing company list/detail for “invalid websites” / “possibly filled” links (filters).

---

## 5. Build Order Summary

| Order | Item | Phase |
|-------|------|--------|
| 1 | Name normalization (services + form) | 1 |
| 2 | Company.domain + normalize_domain + backfill | 1 |
| 3 | Duplicate review queue UI + merge logic | 1 |
| 4 | Dedupe on company create (Use existing / Create anyway) | 1 |
| 5 | CSV bulk import | 2 |
| 6 | Domain list import | 2 |
| 7 | LinkedIn URL list import | 2 |
| 8 | Job form company typeahead | 2 |
| 9 | (Optional) Company create API | 2 |
| 10 | Enrichment fields + enrich_company_task (Clearbit + OG) | 3 |
| 11 | Enrich on create + Re-enrich + Re-enrich stale | 3 |
| 12 | Enrichment status tab | 3 |
| 13 | (Optional) Knowledge Graph / Apollo / Hunter | 4 |
| 14 | Celery Beat: 30/90-day re-enrich | 4 |
| 15 | Data Pipeline settings dashboard (all tabs) | 5 |

---

## 6. Dependencies and Risks

- **Phase 1** is required for Phase 2 (CSV/domain/LinkedIn imports should normalize and dedupe).  
- **Phase 2** can be split: CSV first, then domain list, then LinkedIn, then typeahead.  
- **Phase 3** depends only on Company model; can run in parallel with Phase 2.  
- **Phase 4** depends on Phase 3.  
- **Phase 5** depends on Phases 1–3 at least; Tabs 4–5 reuse existing validators and optional EnrichmentLog.  
- **Risks:** External APIs (Clearbit, KG, Apollo) may rate-limit or require paid plans; design so enrichment degrades gracefully (e.g. logo only, or manual fill).  
- **Data migration:** Backfilling `domain` and optional `name_normalized` must be idempotent and run on a copy in staging first.

---

## 7. File and Component Map

- **companies/models.py:** Company.domain, enrichment_status, enriched_at, enrichment_source, data_quality_score, description (optional); CompanyDuplicateCandidate (optional).  
- **companies/services.py:** normalize_company_name, normalize_domain; extend find_potential_duplicate_companies to use domain; merge_companies(from_id, to_id).  
- **companies/tasks.py:** validate_company_links_task (existing); enrich_company_task; bulk_import_companies_from_csv_task; import_companies_from_domains_task; import_companies_from_linkedin_urls_task.  
- **companies/views.py:** Duplicate review list + merge action; company create with dedupe redirect.  
- **companies/forms.py:** CompanyForm clean_name normalization; CSV upload form; domain list form; LinkedIn URL form.  
- **core/views.py (or settings):** Data Pipeline dashboard view; tabs for Ingestion, Enrichment status, Duplicate queue, URL validation, Logs.  
- **core/urls.py:** Data pipeline routes.  
- **config/settings.py:** Optional API keys (or use PlatformConfig).  
- **apps/core/signals.py:** Add periodic tasks for 30-day re-enrich, 90-day full re-enrich.  
- **jobs/forms.py or job form template:** Company typeahead endpoint + JS.

---

This plan is the single reference for implementing the corporate company data pipeline. Implement in order Phase 1 → 2 → 3 → 4 → 5, with optional items (API, paid enrichment) deferred as needed.

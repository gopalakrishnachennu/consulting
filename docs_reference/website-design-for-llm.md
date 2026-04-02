# Website Design Document — For Explaining to Another LLM

Use this document to give another LLM a complete, structured picture of the platform: purpose, audience, features, tech stack, design, security, performance, and architecture.


Short answer: nothing from the “phase 1–3” Companies work is missing; all of that is actually implemented.

From your newer wishlist, these are still not implemented in code (only specced in our discussion):

Deep duplicate company detection (fuzzy name + domain).
Job URL auto‑revalidation every 24 hours with a “Possibly filled” flag.
Blacklist / Do‑Not‑Submit (company blacklist + consultant‑level blocks).
Watchlist + external job scraping and alerts.
Global “Top Companies by health / revenue / placements” widget in Admin Analytics.
Full company interaction timeline (submissions + interviews + email events + placements) instead of just counts.
Those are big enough that they need to be broken into smaller phases (and tested) before I start editing more files; if you tell me which one of these you want first (e.g. “just implement job URL revalidation + possibly filled status” or “start with company blacklist logic”), I can safely write and wire that piece next.
---

## 1. Purpose & Concept

### 1.1 What the Website Is
- **Product name (configurable):** Default branding is "EduConsult" / "GoCareers"; the platform name is configurable via **Settings → Platform config** (e.g. "CHENN"). All user-facing strings (login heading, home welcome, nav) use this configured site name.
- **One-line description:** A full-stack **consulting/talent platform** that connects **employees** (internal recruiters/hiring managers) with **consultants** (candidates). Employees post jobs, generate **AI-powered, ATS-validated resumes** for consultants, submit applications on their behalf, and track submissions and interviews.
- **Core value:** Centralized job management, AI resume generation with ATS scoring, submission tracking with proof upload, and role-based dashboards (Admin, Employee, Consultant).

### 1.2 High-Level Workflow
1. **Employees** create job postings (manual or bulk CSV), browse consultants, and generate resume drafts per consultant–job pair using configurable LLM prompts.
2. **Resume drafts** are scored for ATS compatibility; users see validation warnings/errors and can edit, approve, or regenerate.
3. **Employees** submit approved resumes to external portals, upload proof (screenshots/PDFs), and track status (Submitted, Under Review, Interview Scheduled, Rejected, Hired, etc.).
4. **Consultants** see their applications, interviews, and saved jobs on a personal dashboard; they can message employees and view generated resumes and proof.
5. **Admins** manage users, platform config, feature flags, LLM settings, prompt library, audit logs, and system health.

---

## 2. Target Audience

| Role | Who They Are | Primary Use |
|------|----------------|-------------|
| **Admin** | Platform operators | User management, platform config, feature flags, LLM config, prompt library, audit logs, analytics, impersonation, system health |
| **Employee** | Internal recruiters / hiring managers | Post jobs, bulk upload, find consultants, generate AI resumes, submit applications, upload proof, track submissions, messaging, interviews |
| **Consultant** | Candidates / freelancers | View applications and status, view interviews and calendar, saved jobs, profile management, messaging |

---

## 3. Core Features (Implemented)

### 3.1 Authentication & Users
- Django auth (login, logout, password change); `AUTH_USER_MODEL = 'users.User'`.
- **User roles:** `ADMIN`, `EMPLOYEE`, `CONSULTANT` (stored on `User.role`).
- **Consultant profiles:** `ConsultantProfile` — bio, base_resume_text, skills (JSON), hourly_rate, marketing_roles (M2M), status (ACTIVE/BENCH/INACTIVE/PLACED), match_jd_title_override, timezone.
- **Employees:** Linked to `User` with role EMPLOYEE; can have department, etc.
- **Impersonation:** Admins can "view as" another user; sticky amber banner and audit trail.

### 3.2 Platform Configuration (Singleton)
- **Model:** `core.PlatformConfig` (singleton, pk=1), cached after load.
- **Branding:** site_name, site_tagline, logo_url — used in base template and context processor so login heading, home welcome, and nav show the configured brand.
- **Feature flags:** enable_consultant_registration, enable_job_applications, enable_public_consultant_view, match_jd_title_default, enable_consultant_global_interview_calendar.
- **System:** maintenance_mode, maintenance_message, session_timeout_minutes, max_upload_size_mb.
- **SEO/Contact:** meta_description, meta_keywords, contact_email, support_phone, address; social URLs (twitter, linkedin, github); ToS and privacy policy URLs.

### 3.3 Jobs
- **Model:** `jobs.Job` — title, company, location, description, original_link, salary_range, job_type (FULL_TIME/PART_TIME/CONTRACT/INTERNSHIP), status (OPEN/CLOSED/DRAFT), marketing_roles (M2M), posted_by, last_edited_by, last_edited_at.
- **Optional parsing:** parsed_jd (JSON), parsed_jd_status, parsed_jd_error, parsed_jd_updated_at for future JD parsing.
- **Actions:** CRUD, list with search/filter (HTMX live search), bulk CSV upload, export CSV.

### 3.4 AI Resume Generation (Resumes App)
- **ResumeDraft:** consultant + job + version; content (markdown), status (PROCESSING/DRAFT/REVIEW/FINAL/ERROR), ATS score, validation_errors, validation_warnings, tokens_used, llm_* fields (system prompt, user prompt, input summary, request payload), created_by, created_at.
- **Flow:** Employee selects consultant and job; chooses prompt template (from prompt library) and optional LLM input preferences; system calls LLM, stores draft, runs ATS validation, shows score and warnings.
- **LLM config:** Stored in `core.LLMConfig` (model, temperature, max_tokens, etc.); API key encrypted via `core.security` (Fernet, key from `LLM_ENCRYPTION_KEY` or derived from `SECRET_KEY`).
- **LLM usage logging:** Every generation logged (tokens, model, request payload) for audit and cost tracking.
- **Templates:** Resume templates and template packs tied to marketing roles; prompt library in `prompts_app` (CRUD).

### 3.5 Submissions
- **ApplicationSubmission:** Links resume draft (or consultant + job) to submission status; proof uploads; portal name/URL, confirmation number, notes; status pipeline (e.g. Pending, Submitted, Under Review, Interview Scheduled, Rejected, Hired).
- **SubmissionResponse:** For tracking employer responses (optional).
- Employees create/update submissions, upload proof files; consultants view their submissions and documents.

### 3.6 Interviews
- **App:** `interviews_app` — interview scheduling and calendar.
- **Config:** `enable_consultant_global_interview_calendar` in PlatformConfig controls whether consultants see only their interviews or the full calendar.

### 3.7 Messaging
- **App:** `messaging` — internal messaging between employees and consultants (inbox, threads).

### 3.8 Analytics
- **App:** `analytics` — dashboards and metrics (e.g. for admin/employee).

### 3.9 Admin & Settings
- **Admin dashboard:** KPIs (jobs, consultants, employees, applications), recent jobs/applications, quick actions.
- **Employee dashboard:** My jobs, open jobs, applications received, pending review, quick actions.
- **Consultant dashboard:** Application counts, pipeline snapshot (draft/in progress/submitted, etc.), recent applications, interviews, saved jobs, quick actions.
- **Settings area:** Platform config, LLM config, LLM logs (with request payload inspection), system status, audit log.

### 3.10 Audit & Compliance
- **AuditLog:** actor, action, target model/object_id, timestamp, metadata — for critical actions and impersonation.
- **Middleware:** `core.middleware.AuditMiddleware` logs configured actions.

---

## 4. Technology Stack

| Layer | Technology |
|-------|------------|
| **Backend** | Python 3.10+, Django 5.x |
| **Frontend** | Django templates, HTMX (django-htmx), django-browser-reload |
| **Styling** | Tailwind CSS via django-tailwind (theme app) |
| **Database** | SQLite (dev) / PostgreSQL (prod via dj-database-url) |
| **AI/LLM** | OpenAI API (e.g. GPT-4o-mini); configurable model, temperature, max tokens |
| **Async/tasks** | Celery + Redis (optional; for background jobs) |
| **Static/media** | WhiteNoise for static files |
| **Security** | cryptography (Fernet) for encrypting LLM API keys; Django auth, RBAC |
| **Deployment** | Gunicorn, Docker/Docker Compose (as per README) |

**Key dependencies:** django-htmx, django-tailwind, django-widget-tweaks, python-decouple, python-docx, openai, whitenoise, gunicorn, django-extensions, pytest-django, cryptography.

---

## 5. Design (UI/UX)

### 5.1 Global Theme
- **Framework:** Tailwind CSS; base template includes `{% tailwind_css %}` and a consistent container (e.g. `max-w-7xl mx-auto`, `bg-gray-100` body).
- **Look:** Admin-style SaaS — white cards on pale gray background, primary blue (`bg-blue-600/700`, `text-blue-600/800`).
- **Typography:** Sans-serif, clean; headings in gray-800, body in gray-600/700.

### 5.2 Base Template (`templates/base.html`)
- **Top bar:** Full-width blue header with `PLATFORM_CONFIG.site_name` and logo (if `PLATFORM_CONFIG.logo_url`); role-based nav links; "Welcome, {{ user.username }}", Change Password, Logout.
- **Banners:** Maintenance mode (red) when `PLATFORM_CONFIG.maintenance_mode`; impersonation (amber) when active.
- **Messages:** Django messages in blue alert style.

### 5.3 Color System
- **Blue:** Primary (buttons, headers, links).
- **Gray:** Backgrounds (50/100), text (600/800/900).
- **Semantic:** Green (success/active), Yellow/Amber (pending/warning), Red (error/closed); Indigo/Purple/Teal for analytics, LLM, prompts.

### 5.4 Patterns
- **Cards:** `bg-white rounded-xl shadow-sm border border-gray-200`; optional left border (`border-l-4 border-*-500`) for category.
- **Status badges:** Rounded pills, color by status (OPEN/CLOSED, ACTIVE/BENCH/PLACED, submission statuses).
- **Lists/tables:** White container, `divide-y`, hover row highlight; action icons on hover.
- **Forms:** `border border-gray-300 rounded-lg`, `focus:ring-blue-500/20 focus:border-blue-500`.
- **Pagination:** Previous/Next, current page indicator.

### 5.5 Key Screens
- **Home:** Landing + login CTA; uses `MSG_HOME_WELCOME` and `SITE_TAGLINE` (from platform config or constants).
- **Login:** Card with `MSG_LOGIN_HEADING` (e.g. "Login to CHENN"), username/password, Sign In, Forgot Password.
- **Admin dashboard:** KPI cards (jobs, consultants, employees, applications), recent jobs/applications, quick actions (Post Job, Add Consultant, Analytics, Employees).
- **Employee dashboard:** My jobs, applications for my jobs, quick actions (Post Job, Bulk Upload, All Applications, Analytics).
- **Consultant dashboard:** Application counts, pipeline snapshot (Draft / In Progress / Submitted), application tracking columns, recent applications, interviews, saved jobs, quick actions.
- **Consultant list:** Search and filter (HTMX live search), role filter; cards with avatar, name, rate, status, bio snippet, skills pills; Message, View Profile.
- **Job list:** Search and filter (HTMX), role filter; bulk upload, Post Job; rows with title, company, location, job type, status, posted date; View/Edit on hover.
- **Resume flow:** Generate draft → view ATS score and validation → edit/regenerate/approve → download DOCX.
- **Submissions:** List and detail; proof upload; status updates.

---

## 6. Security

- **Authentication:** Django session-based; password change flow; no social login in core description.
- **Authorization:** Role-based; views use mixins/checks for ADMIN/EMPLOYEE/CONSULTANT; consultants see only their data; employees see their jobs and related submissions.
- **Sensitive data:** LLM API key stored encrypted (Fernet); key from `LLM_ENCRYPTION_KEY` or derived from `SECRET_KEY`; decryption in `core.security`.
- **Audit:** AuditLog and AuditMiddleware for critical actions and impersonation.
- **Config:** SECRET_KEY and ALLOWED_HOSTS via environment (e.g. python-decouple); DEBUG configurable.

---

## 7. Performance & Optimizations

- **PlatformConfig:** Singleton loaded once and cached (`cache.set('platform_config', obj)`); cache invalidated on save.
- **Context processors:** `core.context_processors.platform_settings` exposes `PLATFORM_CONFIG`; `config.context_processors.site_config` exposes branding and message strings (site name from PlatformConfig).
- **HTMX:** List pages (consultants, jobs) use HTMX for search/filter without full page reload.
- **Static:** WhiteNoise for efficient static serving in production.
- **DB:** Indexes and query patterns per model; use of select_related/prefetch_related where relevant (inspect views for N+1).

---

## 8. Project Structure (Key Paths)

```
consulting/
├── apps/
│   ├── core/           # PlatformConfig, AuditLog, LLMConfig, LLMUsageLog, services, context_processors, middleware
│   ├── users/          # User, ConsultantProfile, MarketingRole, Employee; views for consultants/employees
│   ├── jobs/           # Job model; list, CRUD, bulk upload, export
│   ├── resumes/        # ResumeDraft, LLMInputPreference; AI generation, ATS, DOCX
│   ├── submissions/    # ApplicationSubmission, SubmissionResponse; proof upload, status
│   ├── interviews_app/ # Interview scheduling, calendar
│   ├── messaging/      # Inbox, threads
│   ├── analytics/      # Dashboards
│   └── prompts_app/    # Prompt library CRUD
├── config/             # settings, urls, context_processors, middleware
├── templates/          # base.html, registration/, core/, users/, jobs/, resumes/, submissions/, etc.
├── theme/              # Tailwind theme
├── docs_reference/     # ui-architecture.md, this file
└── manage.py
```

**URL namespace:** `/` (home), `/accounts/` (auth), `/jobs/`, `/resumes/`, `/submissions/`, `/interviews/`, `/messages/`, `/consultants/`, `/employees/`, `/analytics/`, `/core/`, `/prompts/`, `/admin-dashboard/`, `/employee-dashboard/`, `/impersonate/...`.

---

## 9. Configuration & Environment

- **Environment variables:** SECRET_KEY, DEBUG, ALLOWED_HOSTS, LLM_ENCRYPTION_KEY, database URL (for production).
- **INSTALLED_APPS:** Django apps + theme, tailwind, django_htmx, django_browser_reload, widget_tweaks, users, core, jobs, resumes, submissions, messaging, analytics, interviews_app, prompts_app.
- **Python path:** `sys.path.append(BASE_DIR / 'apps')` so apps are imported as `core`, `users`, `jobs`, etc. (not `apps.core`).

---

## 10. Future / Roadmap (From features.txt)

- **Phased feature list:** 350+ features outlined (admin, employee, consultant); many already implemented (jobs, resumes, submissions, messaging, dashboards, platform config, LLM config, audit).
- **Planned/optional:** Advanced analytics, A/B testing, email templates, scheduled reports, support tickets, 2FA enforcement, GDPR export, calendar sync, reviews/ratings, mobile app.

### 10.1 Future: Multi-tenant / White-Label Organisations

This is **not fully implemented yet**, but partial scaffolding exists and the rest should follow this plan.

- **Current scaffolding**
  - `core.Organisation` model:
    - Fields: `name`, `slug`, `logo_url`, `primary_color`, `accent_color`, `is_active`, timestamps.
    - Not yet used in queries; safe to ignore when unused.
  - `users.User.organisation`:
    - Optional FK to `core.Organisation` (nullable, `on_delete=SET_NULL`).
    - All existing users still work; superusers can be attached to an organisation later.

- **Planned next steps (Phase A – data wiring)**
  - Add `organisation = ForeignKey(Organisation, null=True, blank=True)` to:
    - `jobs.Job`
    - `submissions.ApplicationSubmission`
    - `resumes.ResumeDraft`
    - `interviews_app.Interview` (and any other core models we want tenant-scoped).
  - Backfill migration:
    - Create a default `Organisation` (e.g. "CHENN Default").
    - Set `User.organisation` where empty → default org.
    - Set:
      - `Job.organisation` → `job.posted_by.organisation` or default.
      - `ApplicationSubmission.organisation` → `submission.job.organisation`.
      - `ResumeDraft.organisation` → `draft.job.organisation`.
      - `Interview.organisation` → `interview.submission.organisation`.
  - Ensure create flows always set organisation:
    - Job create / bulk upload: from `request.user.organisation`.
    - Submission / ResumeDraft / Interview create: from related job / submission.

- **Planned next steps (Phase B – query scoping)**
  - For non-superusers:
    - Derive current organisation from `request.user.organisation`.
    - Apply `organisation=<current_org>` filter to all reads in:
      - Jobs, submissions, resumes, interviews, analytics, dashboards.
  - For superusers:
    - Either see all orgs, or explicitly select an organisation (admin "switch org" control).

- **Planned later (Phase C – per-org branding and domains)**
  - Extend `PlatformConfig` to be per-organisation instead of singleton:
    - One `PlatformConfig` per `Organisation`.
    - `PlatformConfigService.get_config()` becomes `get_for_org(org)` and the context processor uses `request.user.organisation` or host-based resolution.
  - Add `OrganisationDomain` (org FK + domain + is_primary) so:
    - `foo.agency.com` → Org Foo, `bar.agency.com` → Org Bar.
  - Theming:
    - Use `Organisation.primary_color` / `accent_color` to drive CSS variables or Tailwind utility overrides, so each org has its own brand colors, logo, and title, without changing templates.

This section should be the reference for any future work to turn CHENN into a multi-tenant, white-label SaaS offering. No code today assumes multiple organisations, so the current behavior is still single-tenant until Phase A/B are implemented.

### 10.2 Future: IMAP Email Parsing & Auto-Status Updates

This is the planned design for integrating a Gmail inbox (or similar IMAP account) to automatically update submissions based on employer emails, with **rules-first parsing and optional AI as a fallback**.

- **Concept**
  - A dedicated inbox (e.g. `updates@...`) receives forwarded employer emails.
  - A background worker polls that inbox via IMAP every N seconds, processes **unread** messages, logs them, and (optionally) updates submission statuses.

- **Gmail / IMAP account setup (external)**
  - Create a dedicated mailbox (e.g. `chenn.updates@gmail.com`).
  - Enable 2FA and generate an **App Password** for “Mail”.
  - Enable IMAP in Gmail settings.
  - These credentials are entered once into the platform’s settings (see below).

- **Planned settings UI**
  - New section under Settings → Platform Config (e.g. **Email Parsing** tab):
    - IMAP host (default: `imap.gmail.com`).
    - Port (default: 993, SSL).
    - Username (email address).
    - App password (encrypted via the same mechanism as LLM API key in `core.security`).
    - Poll interval (seconds, default 60).
    - Feature toggle: enable/disable parsing.
    - Optional: AI fallback toggle + confidence threshold (for future).

- **Email ingestion (Phase 1 – rules-only, no AI)**
  - **New service module** (e.g. `emails/services.py` or `core/email_ingest.py`) using Python’s `imaplib` or `imapclient`:
    - Connects to IMAP using settings from PlatformConfig.
    - Selects INBOX, searches for `UNSEEN` messages.
    - For each message:
      - Extracts headers (`From`, `To`, `Subject`, `Date`) and plain-text / HTML body.
      - Logs a normalized record into an `EmailEvent` model (see below).
      - Marks the message as `\Seen` so it’s not reprocessed.
  - **Model: `EmailEvent`** (planned):
    - `id`, `received_at`, `from_address`, `to_address`, `subject`, `body_snippet`, `raw_message_id`.
    - Parsing result fields: `detected_status`, `detected_candidate_name`, `detected_company`, `detected_job_title`, `confidence`, `matched_submission` FK (nullable), `applied_action` (`"none"`, `"auto_updated"`, `"needs_review"`).
  - **Rules-based detection** (no tokens):
    - Simple regex + keyword checks on subject/body, e.g.:
      - Contains `"interview"` / `"schedule"` → candidate likely moving to INTERVIEW.
      - Contains `"offer"` / `"congratulations"` → OFFER.
      - Contains `"regret"` / `"unfortunately"` / `"not moving forward"` → REJECTED.
    - Matching to a submission:
      - Scope candidates by the **employee** who forwarded the email (derived from `To`/`From` or a special alias).
      - Use job company + job title tokens vs JD.
      - Use consultant full name / email found in body.
    - Only mark as auto-detect when:
      - There is a **single clear match** to an `ApplicationSubmission`.
      - Status implied by the email is consistent with the current pipeline (e.g. not going from REJECTED to INTERVIEW).

- **Auto-update rules (Phase 1)**
  - For high-confidence matches:
    - Update the `ApplicationSubmission.status` (e.g. to INTERVIEW or REJECTED).
    - Append to `SubmissionStatusHistory` using `record_submission_status_change()` with a note like `"Email parser: INTERVIEW (rules)"`.
    - Optionally create a `SubmissionResponse` or timeline entry with email snippet.
  - For low-confidence or ambiguous matches:
    - Do **not** auto-update.
    - Flag `EmailEvent.applied_action = "needs_review"` to appear in an admin review UI.

- **Email Log / Review UI (planned)**
  - New admin page (e.g. under Settings or a dedicated `emails/` section) listing `EmailEvent` rows:
    - Columns: Received time, From, Subject, `detected_status`, `confidence`, `matched_submission`, `applied_action`.
    - Filters: date range, status (auto-updated / needs-review / ignored).
    - For `"needs_review"` entries:
      - Show a “Link to submission” dropdown or search.
      - Allow admin/employee to manually apply a status change to a chosen submission.

- **Phase 2 (optional) – AI fallback**
  - When rules return `detected_status = unknown` or confidence below threshold:
    - Send a **small** prompt to the existing `LLMService` (cheap model, low tokens) with email subject+body.
    - Ask for:
      - Classification: {IN_PROGRESS, APPLIED, INTERVIEW, OFFER, REJECTED, OTHER}.
      - Candidate name, company, job title.
      - Confidence 0–100.
    - Only apply when confidence ≥ configured threshold; otherwise leave as review-only.
  - This phase is **optional** and can be turned off entirely to keep token usage near-zero.

This section should guide any future implementation of IMAP-based email parsing and auto-status updates, ensuring it is introduced in a **rules-first, low-token** way that matches CHENN’s existing submissions, status history, and analytics. No email parsing code is currently active; all of the above is a design for later phases.

---

## Summary for the Other LLM

When explaining this to another LLM, stress:

1. **What it is:** A consulting/talent platform with three roles (Admin, Employee, Consultant); employees post jobs and generate AI resumes for consultants, then submit and track applications with proof upload.
2. **Branding:** Fully configurable via PlatformConfig (site name, tagline, logo); used in nav, login, and home so no hardcoded product name in user-facing copy.
3. **Tech:** Django 5, Tailwind, HTMX, OpenAI for resume generation; encrypted LLM keys; singleton platform config cached; role-based access everywhere.
4. **Design:** Card-based, blue primary, semantic status colors; role-specific dashboards and lists; HTMX for live search on key list pages.
5. **Security:** RBAC, audit logging, impersonation with banner, encrypted API keys.
6. **Key apps:** core (config, audit, LLM), users (profiles, consultants, employees), jobs, resumes (AI + ATS), submissions, interviews_app, messaging, analytics, prompts_app.

Use this document as the single source of truth when asking another LLM to reason about or extend the website.

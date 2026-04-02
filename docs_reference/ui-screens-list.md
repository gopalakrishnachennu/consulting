# Complete UI Screens List

Every screen, tab, and small UI surface in the app. **URL name** and **template** are listed where applicable. For minimalist UI/UX design rules and a per-screen checklist, see **§18**.

---

## 1. Global / Layout

| Screen / UI element | Template | Notes |
|---------------------|----------|--------|
| **Base layout** (nav, footer, blocks) | `templates/base.html` | Wraps all authenticated pages |
| **Maintenance banner** | In `base.html` | Top red bar when `maintenance_mode` is on |
| **Impersonation banner** | In `base.html` | Sticky amber bar when impersonating; link to stop |
| **Top navigation** (role-based links) | In `base.html` | Admin / Employee / Consultant menus |
| **Mobile nav toggle** (hamburger) | In `base.html` | Alpine.js `.nav-open` / `.nav-menu` |
| **Flash messages** | In `base.html` | Blue alert style for success/error |

---

## 2. Home & Auth

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Home / landing** | `/` | `home` | `home.html` |
| **Login** | `/accounts/login/` | `login` | `registration/login.html` |
| **Logout** | `/accounts/logout/` | `logout` | (Django auth, redirect) |
| **Change password** | `/accounts/password_change/` | `password_change` | `registration/password_change_form.html` |
| **Password change done** | `/accounts/password_change/done/` | `password_change_done` | `registration/password_change_done.html` |

*(Password reset flow uses Django default if present: `password_reset`, `password_reset_done`, `password_reset_confirm`, `password_reset_complete` — no custom templates in this project.)*

---

## 3. Dashboards

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Admin dashboard** | `/admin-dashboard/` | `admin-dashboard` | `core/admin_dashboard.html` |
| **Employee dashboard** | `/employee-dashboard/` | `employee-dashboard` | `core/employee_dashboard.html` |
| **Consultant dashboard** | `/consultants/dashboard/` | `consultant-dashboard` | `users/consultant_dashboard.html` |

---

## 4. Jobs

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Job list** | `/jobs/` | `job-list` | `jobs/job_list.html` |
| **Job list (HTMX partial)** | Same, HTMX response | — | `jobs/_job_list_partial.html` |
| **Job detail** | `/jobs/<pk>/` | `job-detail` | `jobs/job_detail.html` |
| **Create job** | `/jobs/new/` | `job-create` | `jobs/job_form.html` |
| **Edit job** | `/jobs/<pk>/edit/` | `job-update` | `jobs/job_form.html` |
| **Delete job (confirm)** | `/jobs/<pk>/delete/` | `job-delete` | `jobs/job_confirm_delete.html` |
| **Bulk upload jobs** | `/jobs/bulk-upload/` | `job-bulk-upload` | `jobs/job_bulk_upload.html` |
| **Job export CSV** | `/jobs/export/` | `job-export-csv` | (HTTP response, no template) |

---

## 5. Applications (Submissions)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Application list** | `/submissions/` | `submission-list` | `submissions/submission_list.html` |
| **Application detail** | `/submissions/<pk>/` | `submission-detail` | `submissions/submission_detail.html` |
| **Log / create application** | `/submissions/log/` | `submission-create` | `submissions/submission_form.html` |
| **Update application** | `/submissions/<pk>/update/` | `submission-update` | `submissions/submission_form.html` |
| **Claim draft (submission)** | `/submissions/claim/<draft_id>/` | `submission-claim` | (redirect/view) |
| **Submission export CSV** | `/submissions/export/` | `submission-export-csv` | (HTTP response) |
| **Bulk status (POST)** | `/submissions/bulk-status/` | `submission-bulk-status` | (redirect back to list) |

---

## 6. Interviews

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Interview list** | `/interviews/` | `interview-list` | `interviews/interview_list.html` |
| **Interview detail** | `/interviews/<pk>/` | `interview-detail` | `interviews/interview_detail.html` |
| **Add interview** | `/interviews/add/` | `interview-add` | `interviews/interview_form.html` |
| **Edit interview** | `/interviews/<pk>/edit/` | `interview-edit` | `interviews/interview_form.html` |
| **Interview calendar** | `/interviews/calendar/` | `interview-calendar` | `interviews/interview_calendar.html` |
| **Interview export CSV** | `/interviews/export/` | `interview-export-csv` | (HTTP response) |

---

## 7. Consultants (Users app under /consultants/)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Consultant list** | `/consultants/` | `consultant-list` | `users/consultant_list.html` |
| **Consultant list (HTMX partial)** | Same, HTMX | — | `users/_consultant_list_partial.html` |
| **Consultant detail** | `/consultants/<pk>/` | `consultant-detail` | `users/consultant_detail.html` |
| **Add consultant** | `/consultants/add/` | `consultant-add` | `users/consultant_create.html` |
| **Edit consultant** | `/consultants/<pk>/edit/` | `consultant-edit` | `users/profile_form.html` |
| **Consultant export CSV** | `/consultants/export/` | `consultant-export-csv` | (HTTP response) |
| **Saved jobs list** | `/consultants/saved-jobs/` | `saved-jobs` | `users/saved_jobs.html` |
| **Save job (action)** | `/consultants/save-job/<pk>/` | `save-job` | (redirect) |

### Experience / Education / Certification (self or admin)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Add experience** | `/consultants/experience/add/` | `experience-add` | `users/profile_form.html` (or form in context) |
| **Edit experience** | `/consultants/experience/<pk>/edit/` | `experience-edit` | `users/profile_form.html` |
| **Delete experience** | `/consultants/experience/<pk>/delete/` | `experience-delete` | `users/profile_confirm_delete.html` |
| **Add education** | `/consultants/education/add/` | `education-add` | `users/profile_form.html` |
| **Edit education** | `/consultants/education/<pk>/edit/` | `education-edit` | `users/profile_form.html` |
| **Delete education** | `/consultants/education/<pk>/delete/` | `education-delete` | `users/profile_confirm_delete.html` |
| **Add certification** | `/consultants/certification/add/` | `certification-add` | `users/profile_form.html` |
| **Edit certification** | `/consultants/certification/<pk>/edit/` | `certification-edit` | `users/profile_form.html` |
| **Delete certification** | `/consultants/certification/<pk>/delete/` | `certification-delete` | `users/profile_confirm_delete.html` |
| **Admin: add experience for consultant** | `/consultants/<consultant_pk>/experience/add/` | `admin-experience-add` | same |
| **Admin: edit experience** | `/consultants/<consultant_pk>/experience/<pk>/edit/` | `admin-experience-edit` | same |
| **Admin: delete experience** | `/consultants/<consultant_pk>/experience/<pk>/delete/` | `admin-experience-delete` | same |
| **Admin: add education** | `/consultants/<consultant_pk>/education/add/` | `admin-education-add` | same |
| **Admin: edit education** | `/consultants/<consultant_pk>/education/<pk>/edit/` | `admin-education-edit` | same |
| **Admin: delete education** | `/consultants/<consultant_pk>/education/<pk>/delete/` | `admin-education-delete` | same |
| **Admin: add certification** | `/consultants/<consultant_pk>/certification/add/` | `admin-certification-add` | same |
| **Admin: edit certification** | `/consultants/<consultant_pk>/certification/<pk>/edit/` | `admin-certification-edit` | same |
| **Admin: delete certification** | `/consultants/<consultant_pk>/certification/<pk>/delete/` | `admin-certification-delete` | same |

### Marketing roles (admin)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Marketing role list** | `/consultants/marketing-roles/` | `marketing-role-list` | `users/marketing_role_list.html` |
| **Add marketing role** | `/consultants/marketing-roles/add/` | `marketing-role-add` | `users/marketing_role_form.html` |
| **Edit marketing role** | `/consultants/marketing-roles/<pk>/edit/` | `marketing-role-edit` | `users/marketing_role_form.html` |
| **Delete marketing role** | `/consultants/marketing-roles/<pk>/delete/` | `marketing-role-delete` | `users/marketing_role_confirm_delete.html` |

### Consultant drafts (admin/employee)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Generate draft** | `/consultants/<pk>/drafts/generate/` | `draft-generate` | (modal/form on consultant detail) |
| **Draft preview (LLM)** | `/consultants/<pk>/drafts/preview/` | `draft-preview-llm` | (view, may return JSON/HTML) |

---

## 8. Employees

*Note: Employee URLs exist in two mounts — under `/employees/` (urls_employees) and under `/consultants/employees/` (main users app).*

### Under /employees/ (urls_employees)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Employee list** | `/employees/` | `employee-list` | `users/employee_list.html` |
| **Employee list (HTMX partial)** | Same, HTMX | — | `users/_employee_list_partial.html` |
| **Employee detail** | `/employees/<pk>/` | `employee-detail` | `users/employee_detail_v2.html` |
| **Add employee** | `/employees/add/` | `employee-add` | `users/employee_create.html` |
| **Edit employee** | `/employees/<pk>/edit/` | `employee-edit` | `users/profile_form.html` |

### Under /consultants/ (users app)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Employee list** | `/consultants/employees/` | `employee-list` | `users/employee_list.html` |
| **Employee export CSV** | `/consultants/employees/export/` | `employee-export-csv` | (HTTP response) |
| **Add employee** | (use `/employees/add/` or create from settings) | `employee-add` | `users/employee_create.html` |

---

## 9. Settings hub (admin)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Settings dashboard** | `/consultants/settings/` | `settings-dashboard` | `settings/dashboard.html` |
| **Platform config** | `/core/setup/` | `platform-config` | `settings/platform_config.html` |
| **Audit log** | `/core/audit/` | `audit-log` | `settings/audit_log.html` |
| **System status** | `/core/status/` | `system-status` | `settings/system_status.html` |
| **LLM config** | `/core/llm/` | `llm-config` | `settings/llm_config.html` |
| **LLM logs list** | `/core/llm/logs/` | `llm-logs` | `settings/llm_logs.html` |
| **LLM log detail** | `/core/llm/logs/<pk>/` | `llm-log-detail` | `settings/llm_log_detail.html` |
| **Health JSON** | `/core/health/` | `health-json` | (JSON response) |

---

## 10. Prompts (admin)

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Prompt list** | `/prompts/` | `prompt-list` | `prompts/prompt_list.html` |
| **Prompt detail** | `/prompts/<pk>/` | `prompt-detail` | `prompts/prompt_detail.html` |
| **Add prompt** | `/prompts/add/` | `prompt-add` | `prompts/prompt_form.html` |
| **Edit prompt** | `/prompts/<pk>/edit/` | `prompt-edit` | `prompts/prompt_form.html` |
| **Delete prompt** | `/prompts/<pk>/delete/` | `prompt-delete` | `prompts/prompt_confirm_delete.html` |

---

## 11. Resumes & drafts

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Resume create (new)** | `/resumes/new/` | `resume-create` | `resumes/resume_form.html` |
| **Resume detail** | `/resumes/<pk>/` | `resume-detail` | `resumes/resume_detail.html` |
| **Resume download** | `/resumes/<pk>/download/` | `resume-download` | (HTTP response) |
| **Draft detail** | `/resumes/drafts/<pk>/` | `draft-detail` | `resumes/draft_detail.html` |
| **Draft set prompt** | `/resumes/drafts/<pk>/set-prompt/` | `draft-set-prompt` | (view) |
| **Draft regenerate** | `/resumes/drafts/<pk>/regenerate/` | `draft-regenerate` | (view) |
| **Draft regenerate section** | `/resumes/drafts/<pk>/regenerate-section/` | `draft-regenerate-section` | (view) |
| **Draft save LLM input defaults** | `/resumes/drafts/<pk>/save-input-defaults/` | `llm-input-defaults` | (view) |
| **Draft download** | `/resumes/drafts/<pk>/download/` | `draft-download` | (HTTP response) |
| **Draft promote** | `/resumes/drafts/<pk>/promote/` | `draft-promote` | (view) |
| **Draft delete** | `/resumes/drafts/<pk>/delete/` | `draft-delete` | (view) |

---

## 12. Messaging

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Inbox** | `/messages/` | `inbox` | `messaging/inbox.html` |
| **Thread detail** | `/messages/thread/<pk>/` | `thread-detail` | `messaging/thread_detail.html` |
| **Start thread** | `/messages/start/<user_id>/` | `start-thread` | (redirect to thread or form) |

---

## 13. Analytics

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Analytics dashboard** | `/analytics/` | `analytics-dashboard` | `analytics/dashboard.html` |
| **Analytics export CSV** | `/analytics/export/` | `analytics-export-csv` | (HTTP response) |

---

## 14. Other / system

| Screen | URL path | URL name | Template |
|--------|----------|----------|----------|
| **Django admin** | `/admin/` | — | Django admin |
| **Impersonate user** | `/impersonate/<user_id>/` | `start-impersonate` | (redirect) |
| **Stop impersonating** | `/impersonate/stop/` | `stop-impersonate` | (redirect) |
| **Browser reload** | `/__reload__/` | — | django-browser-reload |

---

## 15. Partials / “tabs” (HTMX or includes)

| Partial / tab | Template | Used in |
|---------------|----------|---------|
| **Job list (table + pagination)** | `jobs/_job_list_partial.html` | Job list (HTMX refresh) |
| **Consultant cards + pagination** | `users/_consultant_list_partial.html` | Consultant list (HTMX) |
| **Employee cards + pagination** | `users/_employee_list_partial.html` | Employee list (HTMX) |

---

## 16. Templates that are shared (multiple URL names)

- **`users/profile_form.html`** — consultant edit, employee edit, experience/education/certification add/edit (various URL names).
- **`users/profile_confirm_delete.html`** — confirm delete for experience, education, certification, etc.
- **`jobs/job_form.html`** — create and update job.
- **`interviews/interview_form.html`** — add and edit interview.
- **`prompts/prompt_form.html`** — add and edit prompt.
- **`users/marketing_role_form.html`** — add and edit marketing role.

---

## Summary counts

- **Full-page templates:** ~51 (including partials).
- **URL names (named routes):** ~70+.
- **Small UI surfaces:** maintenance banner, impersonation banner, nav, mobile menu, flash messages, bulk-action bar on submission list, period tabs on analytics, filter bars (submissions, consultants, jobs, interviews).

---

## 17. Flat checklist (QA)

Single table for quick scanning. Export/redirect-only screens have "—" for template.

| Screen name | URL name | Template |
|-------------|----------|----------|
| Home | home | home.html |
| Login | login | registration/login.html |
| Logout | logout | — |
| Change password | password_change | registration/password_change_form.html |
| Password change done | password_change_done | registration/password_change_done.html |
| Admin dashboard | admin-dashboard | core/admin_dashboard.html |
| Employee dashboard | employee-dashboard | core/employee_dashboard.html |
| Consultant dashboard | consultant-dashboard | users/consultant_dashboard.html |
| Job list | job-list | jobs/job_list.html |
| Job list partial | — | jobs/_job_list_partial.html |
| Job detail | job-detail | jobs/job_detail.html |
| Job create | job-create | jobs/job_form.html |
| Job edit | job-update | jobs/job_form.html |
| Job delete confirm | job-delete | jobs/job_confirm_delete.html |
| Job bulk upload | job-bulk-upload | jobs/job_bulk_upload.html |
| Job export CSV | job-export-csv | — |
| Submission list | submission-list | submissions/submission_list.html |
| Submission detail | submission-detail | submissions/submission_detail.html |
| Submission create | submission-create | submissions/submission_form.html |
| Submission update | submission-update | submissions/submission_form.html |
| Submission claim | submission-claim | — |
| Submission export CSV | submission-export-csv | — |
| Submission bulk status | submission-bulk-status | — |
| Interview list | interview-list | interviews/interview_list.html |
| Interview detail | interview-detail | interviews/interview_detail.html |
| Interview add | interview-add | interviews/interview_form.html |
| Interview edit | interview-edit | interviews/interview_form.html |
| Interview calendar | interview-calendar | interviews/interview_calendar.html |
| Interview export CSV | interview-export-csv | — |
| Consultant list | consultant-list | users/consultant_list.html |
| Consultant list partial | — | users/_consultant_list_partial.html |
| Consultant detail | consultant-detail | users/consultant_detail.html |
| Consultant add | consultant-add | users/consultant_create.html |
| Consultant edit | consultant-edit | users/profile_form.html |
| Consultant export CSV | consultant-export-csv | — |
| Saved jobs | saved-jobs | users/saved_jobs.html |
| Save job | save-job | — |
| Employee list | employee-list | users/employee_list.html |
| Employee list partial | — | users/_employee_list_partial.html |
| Employee detail | employee-detail | users/employee_detail_v2.html |
| Employee add | employee-add | users/employee_create.html |
| Employee edit | employee-edit | users/profile_form.html |
| Employee export CSV | employee-export-csv | — |
| Settings dashboard | settings-dashboard | settings/dashboard.html |
| Platform config | platform-config | settings/platform_config.html |
| Audit log | audit-log | settings/audit_log.html |
| System status | system-status | settings/system_status.html |
| LLM config | llm-config | settings/llm_config.html |
| LLM logs list | llm-logs | settings/llm_logs.html |
| LLM log detail | llm-log-detail | settings/llm_log_detail.html |
| Health JSON | health-json | — |
| Prompt list | prompt-list | prompts/prompt_list.html |
| Prompt detail | prompt-detail | prompts/prompt_detail.html |
| Prompt add | prompt-add | prompts/prompt_form.html |
| Prompt edit | prompt-edit | prompts/prompt_form.html |
| Prompt delete | prompt-delete | prompts/prompt_confirm_delete.html |
| Resume create | resume-create | resumes/resume_form.html |
| Resume detail | resume-detail | resumes/resume_detail.html |
| Resume download | resume-download | — |
| Draft detail | draft-detail | resumes/draft_detail.html |
| Draft set prompt | draft-set-prompt | — |
| Draft regenerate | draft-regenerate | — |
| Draft download | draft-download | — |
| Draft promote | draft-promote | — |
| Draft delete | draft-delete | — |
| Draft generate | draft-generate | — |
| Draft preview LLM | draft-preview-llm | — |
| Inbox | inbox | messaging/inbox.html |
| Thread detail | thread-detail | messaging/thread_detail.html |
| Start thread | start-thread | — |
| Analytics dashboard | analytics-dashboard | analytics/dashboard.html |
| Analytics export CSV | analytics-export-csv | — |
| Marketing role list | marketing-role-list | users/marketing_role_list.html |
| Marketing role add | marketing-role-add | users/marketing_role_form.html |
| Marketing role edit | marketing-role-edit | users/marketing_role_form.html |
| Marketing role delete | marketing-role-delete | users/marketing_role_confirm_delete.html |
| Start impersonate | start-impersonate | — |
| Stop impersonate | stop-impersonate | — |
| Base layout / nav / banners | — | base.html |

*(Experience/education/certification add/edit/delete use profile_form.html and profile_confirm_delete.html; see section 7 for URL names.)*

---

## 18. Minimalist UI/UX design (per screen)

Every screen should follow the same minimalist design system so the app feels consistent and uncluttered.

### Global principles

- **Cards:** `bg-white rounded-xl shadow-sm` (or `shadow-md` only where emphasis is needed). Optional `border-l-4 border-<color>-500` for category.
- **Spacing:** Generous whitespace; `gap-4`–`gap-6` in grids; `p-4`–`p-6` in cards; `mb-6`–`mb-8` between sections.
- **Typography:** One clear page title (`text-2xl` or `text-3xl font-bold text-gray-800`); body `text-gray-600`; labels `text-sm text-gray-500` or `uppercase tracking-wide`.
- **Buttons:** Primary = solid blue/green/indigo with `rounded-lg`; secondary = border only. No heavy shadows; hover = slight darken or `hover:bg-*-50`.
- **Lists/tables:** `divide-y divide-gray-100`; row hover `hover:bg-gray-50`; actions subtle (e.g. show on row hover).
- **Status pills:** `rounded-full text-xs font-medium px-2.5 py-0.5` with semantic colors (green/amber/red/gray).
- **Forms:** Inputs `border border-gray-300 rounded-lg`; `focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500`; labels above, no visual clutter.
- **Banners/alerts:** Flat or light border; no thick shadows (e.g. `shadow-sm` or none).

### Per-screen minimalist design notes

| Screen | Template | Minimalist UI/UX notes |
|--------|----------|------------------------|
| Home | home.html | Centered content; single headline + tagline; one primary CTA or one line when logged in. No cards. |
| Login | registration/login.html | Single centered card `max-w-md`, `shadow-md`, one heading; form fields stacked; one submit button; minimal error block. |
| Change password | registration/password_change_form.html | Same as login: one card, clear labels, one primary button. |
| Password change done | registration/password_change_done.html | One short success message; one link back. No extra decoration. |
| Admin dashboard | core/admin_dashboard.html | KPI cards: white, `shadow-md`, left border accent; two-column content; quick actions as simple buttons. Pending callout: light blue bar, one CTA. |
| Employee dashboard | core/employee_dashboard.html | Same pattern as admin: header, KPI cards (shadow-md, left border), lists in cards, quick actions. |
| Consultant dashboard | users/consultant_dashboard.html | Same: header, minimal cards, list of jobs/applications, clear CTAs. |
| Job list | jobs/job_list.html | Page title + filters in one bar; content in one white card or list; pagination minimal (prev/next + page). |
| Job list partial | jobs/_job_list_partial.html | Table or rows with `divide-y`; row hover; light shadow on container. |
| Job detail | jobs/job_detail.html | One main content card; metadata as simple list or pills; one prominent “Apply” CTA; related content in secondary cards. |
| Job create / edit | jobs/job_form.html | Single form card; sections with clear headings; one primary submit, one secondary cancel. |
| Job delete confirm | jobs/job_confirm_delete.html | One card: title, short warning, two buttons (danger + secondary). |
| Job bulk upload | jobs/job_bulk_upload.html | One card: instructions, file input, submit. Optional result list with minimal rows. |
| Submission list | submissions/submission_list.html | Title + filter bar; table in white card; bulk bar only when items selected; pagination minimal. |
| Submission detail | submissions/submission_detail.html | Header (applicant, job); status pill; content in cards (e.g. notes, timeline); actions as buttons. |
| Submission create / update | submissions/submission_form.html | Single form card; clear sections; one submit. |
| Interview list | interviews/interview_list.html | Title + filters (status, when); table/cards with status pills; “Export CSV” link; minimal pagination. |
| Interview detail | interviews/interview_detail.html | Header (candidate, role, date); timeline/notes in one card; Edit/Back buttons. |
| Interview add / edit | interviews/interview_form.html | One form card; fields grouped; one submit. |
| Interview calendar | interviews/interview_calendar.html | Calendar view in one card; minimal controls; event chips simple. |
| Consultant list | users/consultant_list.html | Title + filters; grid or list in white card; cards with `shadow-sm`; pagination. |
| Consultant list partial | users/_consultant_list_partial.html | Same: light card style, divide between items. |
| Consultant detail | users/consultant_detail.html | Profile header; sections in separate cards (experience, education, etc.); actions as text/icon links or small buttons. |
| Consultant add | users/consultant_create.html | Single form card; one submit. |
| Consultant edit / Employee edit | users/profile_form.html | Form in one card; sections for profile, experience, education; one primary submit. |
| Saved jobs | users/saved_jobs.html | Title; list of jobs in one card; minimal row design; remove action subtle. |
| Employee list | users/employee_list.html | Same as consultant list: title, filters, one card with rows; Export CSV link. |
| Employee list partial | users/_employee_list_partial.html | Rows with divide; light hover. |
| Employee detail | users/employee_detail_v2.html | Header; permissions block (role + “Can manage consultants” pill); sections in cards. |
| Employee add | users/employee_create.html | One form card; one submit. |
| Settings dashboard | settings/dashboard.html | Grid of setting cards; each card: title, short description, one link; `shadow-sm`, no heavy borders. |
| Platform config | settings/platform_config.html | Form in one card; grouped fields; one save. |
| Audit log | settings/audit_log.html | Title; table in white card; `divide-y`; minimal pagination. |
| System status | settings/system_status.html | Status items in one card or list; green/red/gray indicators; no decoration. |
| LLM config | settings/llm_config.html | One card; form fields; one save. |
| LLM logs list | settings/llm_logs.html | Title; table in card; row link to detail. |
| LLM log detail | settings/llm_log_detail.html | One card: timestamp, payload/result in monospace or simple blocks. |
| Prompt list | prompts/prompt_list.html | Title; list/table in one card; add button; row actions subtle. |
| Prompt detail | prompts/prompt_detail.html | One card: name, body (readable font); Edit/Delete as buttons. |
| Prompt add / edit | prompts/prompt_form.html | One form card; one submit. |
| Prompt delete | prompts/prompt_confirm_delete.html | One card: confirm message; two buttons. |
| Resume create | resumes/resume_form.html | Single form card; one submit. |
| Resume detail | resumes/resume_detail.html | One card: content; download button. |
| Draft detail | resumes/draft_detail.html | Header; content in card; actions (regenerate, promote, download) as small buttons or links. |
| Inbox | messaging/inbox.html | Title; thread list in one card; row hover; unread indicator subtle. |
| Thread detail | messaging/thread_detail.html | Thread title; messages in one card, `divide-y`; reply form at bottom. |
| Analytics dashboard | analytics/dashboard.html | Period tabs minimal; KPI cards `shadow-md` + left border; charts in white cards; one Export CSV link. |
| Marketing role list | users/marketing_role_list.html | Title; table in card; add link; row edit/delete subtle. |
| Marketing role add / edit | users/marketing_role_form.html | One form card; one submit. |
| Marketing role delete | users/marketing_role_confirm_delete.html | One card: confirm; two buttons. |
| Profile confirm delete | users/profile_confirm_delete.html | One card: “Delete [item]?”; danger + cancel. |
| Base / nav / banners | base.html | Nav: flat bar, no shadow; banners: flat or `shadow-sm`; flash messages: light border, no heavy shadow. |

Use this table as a checklist: for each screen, ensure the template matches the described minimalist treatment (cards, spacing, typography, buttons, no clutter).

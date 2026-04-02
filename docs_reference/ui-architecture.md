## EduTech App – UI & Screen Architecture

### 1. Global Theme & Layout

- **Design language**
  - **Framework**: Tailwind CSS via the `theme` app and `{% tailwind_css %}` in `base.html`.
  - **Typography**: Sans-serif (`font-sans`), normal line height, neutral tracking for a clean SaaS look.
  - **Overall feel**: Admin-style web app – white cards on a pale gray background with a strong blue primary brand color and semantic colored badges.

- **Base template (`templates/base.html`)**
  - **Background & spacing**
    - `body` uses `min-h-screen bg-gray-100`, so the entire viewport has a soft gray background.
    - Main page content is wrapped in `container mx-auto mt-8 p-4` for a centered layout with top margin and padding.
  - **Top navigation bar**
    - Full-width blue strip (`bg-blue-600 p-6`) with white text.
    - Left: platform logo (if configured via `PLATFORM_CONFIG.logo_url`) and `PLATFORM_CONFIG.site_name`.
    - Center/right: role-aware navigation links (Admin, Employee, Consultant each see different menus).
    - Far right: “Welcome, {{ user.username }}”, “Change Password”, and a white bordered `Logout` button.
  - **Role-based navigation**
    - Links use muted blue (`text-blue-200`) by default and turn white on hover.
    - For admins, the `⚙️ Settings` link is bolded to emphasize configuration access.
  - **System banners**
    - **Maintenance mode banner**: `bg-red-600 text-white` at the very top when `PLATFORM_CONFIG.maintenance_mode` is enabled.
    - **Impersonation banner**: sticky `bg-amber-500 text-white` bar shown when impersonating another user.
  - **Flash messages**
    - Displayed in a blue alert style: `bg-blue-100 border-blue-500 text-blue-700`, with a bold title and lighter message body.

### 2. Color System & Common UI Patterns

- **Primary colors**
  - **Blue**: primary brand color (`bg-blue-600/700`, `text-blue-600/800`). Used for main buttons, headers, and important navigation.
  - **Gray scale**: `bg-gray-50/100` for section backgrounds and `text-gray-600/800/900` for body text and headings.
  - **Semantic accents**
    - **Green**: success, active statuses (`bg-green-100 text-green-800`).
    - **Yellow/Amber**: in-progress, pending, or warning states (`bg-yellow-100 text-yellow-800`, `bg-amber-600`).
    - **Red**: errors or closed states (`bg-red-100 text-red-800`, `bg-red-600`).
    - **Indigo/Purple/Teal**: secondary feature areas like Analytics (purple), LLM/AI (indigo), and Prompt Service (teal).

- **Card pattern**
  - Reusable card style: `bg-white rounded-xl shadow-md` or `shadow-sm`, often with a left color border (`border-l-4 border-<color>-500`) to visually categorize the card (e.g., Jobs, Employees, AI).

- **Status badges & chips**
  - Rounded pills: `px-2 py-0.5 rounded-full text-xs font-semibold` with color based on status (e.g., OPEN/CLOSED, ACTIVE/BENCH/PLACED).

- **Lists and tables**
  - Lists are typically white containers with:
    - `bg-white rounded-xl shadow-sm border border-gray-200`.
    - `divide-y divide-gray-100` to separate items.
  - Rows use hover states (`hover:bg-blue-50/30`) and reveal action icons (view, edit) only on hover for a cleaner default view.

- **Forms & controls**
  - Inputs: `border border-gray-300 rounded-lg`, with blue-focused rings (`focus:ring-blue-500/20 focus:border-blue-500`).
  - Filled primary buttons: blue, green, indigo, or red backgrounds with white text, rounded corners, medium weight, and hover darkening.

- **Pagination**
  - Uses a bottom bar with `border-gray-300 bg-white`, “Previous/Next” links, and a bold current page indicator, often centered or right-aligned.

### 3. Screen Architecture

#### 3.1 Home / Landing Page (`templates/home.html`)

- **Purpose**
  - Acts as a marketing-style landing page and login entry point.

- **Layout**
  - Extends `base.html`.
  - Content is center-aligned:
    - Large headline (`text-4xl font-bold`) using `MSG_HOME_WELCOME`.
    - Secondary tagline from `SITE_TAGLINE`.

- **Behavior**
  - If the user is not authenticated:
    - Shows a solid blue call-to-action button that links to the login view.
  - If the user is authenticated:
    - Shows an informative line with username and human-readable role (`user.get_role_display`).

#### 3.2 Admin Dashboard (`templates/core/admin_dashboard.html`)

- **Purpose**
  - High-level operational overview for admins: jobs, consultants, employees, and applications.

- **Layout**
  - Container: `max-w-7xl mx-auto px-4 py-8`.
  - Vertical structure:
    1. Header.
    2. KPI cards grid.
    3. Two-column content (Recent Jobs, Recent Applications).
    4. Quick Actions row.

- **Header**
  - `Admin Dashboard` title (`text-3xl font-bold text-gray-800`).
  - Subheading greeting the admin (`text-gray-500`) with name or username.

- **KPI cards**
  - Grid: `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-6`.
  - Cards: white rounded with shadows and left color border:
    - Total Jobs (blue).
    - Active Jobs (green).
    - Consultants (indigo).
    - Employees (amber).
    - Applications (purple).

- **Main two-column content**
  - **Recent Job Postings**
    - Card container with title, “View All →” link.
    - Each job row:
      - Job title (bold).
      - Company and location (gray text).
      - Status pill (`OPEN` vs others) with color-coded backgrounds.
  - **Recent Applications**
    - Similar card style.
    - Each row:
      - Job title and consultant name.
      - Status badge using yellow, red, or green depending on application status.

- **Quick actions**
  - Single card spanning both columns (`lg:col-span-2`).
  - 2x2 grid of buttons:
    - Post Job (blue).
    - Add Consultant (indigo).
    - Analytics (purple).
    - Employees (amber).

#### 3.3 Employee Dashboard (`templates/core/employee_dashboard.html`)

- **Purpose**
  - Shows performance and activity for individual employees (internal recruiters/hiring managers).

- **Header & KPI row**
  - Title: `Employee Dashboard` plus greeting.
  - Four KPI cards:
    - My Posted Jobs (blue).
    - My Open Jobs (green).
    - Apps Received (purple).
    - Pending Review (yellow).
  - Each card uses the same white rounded style with a colored left border.

- **Two-column main section**
  - **My Recent Job Postings**
    - List of the employee’s own jobs.
    - Each row includes:
      - Job title with hover link to detail.
      - Company/location.
      - Status pill for job status.
      - Inline “Edit” link for quick navigation to the job edit form.
  - **Applications for My Jobs**
    - List of submissions to jobs posted by the employee.
    - Each row shows:
      - Job title and consultant name.
      - Status pill with yellow (APPLIED), red (REJECTED), or green (other positive states).

- **Quick Actions**
  - Card at bottom with 2x2 button grid:
    - Post New Job (blue).
    - Bulk Upload (green).
    - All Applications (purple).
    - Analytics (amber).

- **Platform overview**
  - Simple card summarizing global open jobs count, highlighting the number in blue for emphasis.

#### 3.4 Consultant Dashboard (`templates/users/consultant_dashboard.html`)

- **Purpose**
  - Personal “job hunting cockpit” for consultants: applications, interviews, and saved jobs.

- **Header & KPI cards**
  - Title: `My Dashboard`.
  - Three KPI cards:
    - Total Applications (blue left border).
    - Active (green left border).
    - Pending (yellow left border).

- **Pipeline snapshot**
  - Card titled **Pipeline Snapshot**.
  - Grid of seven small bordered tiles (Draft, In Progress, Submitted, Active, Interview, Rejected, Responses).
  - Each tile shows:
    - Uppercase label in gray.
    - Large numeric count, colored per state (e.g., yellow for In Progress, green for Active).

- **Application tracking**
  - Card titled **My Application Tracking**.
  - Inside: 3-column layout for Drafts, In Progress, Submitted.
  - For each list item:
    - Mini white sub-card with job title, company, and date/time metadata.
    - Quick action links: Job Detail (`JD`), Resume, or external proof file when applicable.
  - Column backgrounds:
    - Drafts: gray (neutral).
    - In Progress: yellow theme.
    - Submitted: green theme.

- **Lower grid**
  - Responsive two-column arrangement:
    - **Recent Applications**: job/company text with multi-color status badges.
    - **Interviews (Recent)**: job title, company, date/time with purple status chip.
    - **Latest Open Jobs**: list of click-through links to recent jobs.
    - **Saved Jobs**: saved job list, with relative “saved X ago” text.
    - **Quick Actions**: vertical stack of four full-width colored buttons (Interviews, My Profile, Browse Jobs, My Messages), each with an emoji and bright background.

#### 3.5 Consultants List (`templates/users/consultant_list.html` and `_consultant_list_partial.html`)

- **Purpose**
  - Discover, filter, and browse consultants.

- **Header bar**
  - Flex layout:
    - Left: `Find Consultants` heading and a count pill showing the number of results.
    - Right: actions and filters:
      - For admins: `+ Add Consultant` (blue) and `Manage Roles` (indigo).
      - Role filter dropdown using a standard white select with blue focus.
      - Search input:
        - Bordered, rounded.
        - Integrated with **HTMX** (`hx-get`, `hx-trigger="keyup changed delay:500ms"`) to perform live search and update the result list.

- **Results grid (`_consultant_list_partial.html`)**
  - Card container is a responsive grid: 1–3 columns depending on viewport width.
  - Each card includes:
    - **Header row**:
      - Avatar circle (uploaded image or placeholder initial).
      - Name as a bold link to consultant detail.
      - Rate and status with status pill color-coded (ACTIVE, BENCH, PLACED, etc.).
    - **Bio snippet**:
      - Gray text truncated to three lines using a line clamp class.
    - **Skills list**:
      - Blue pill tags for each skill.
      - Fallback text “No skills listed” if the list is empty.
    - **Footer actions**:
      - `Message` link (indigo) to start a message thread.
      - `View Profile` link (gray).

- **Pagination**
  - When paginated, a centered pagination component with Previous/Next links and “Page X of Y” label.

#### 3.6 Jobs List (`templates/jobs/job_list.html` and `_job_list_partial.html`)

- **Purpose**
  - Browse, filter, and manage job postings.

- **Header**
  - Left:
    - `Job Listings` title and a count chip indicating total jobs found.
    - Subtitle: “Manage and track your open positions.”
  - Right:
    - **Role filter** (`select` with custom SVG arrow overlay).
    - **Search input**:
      - Contains a search icon in the left, blue focus ring.
      - HTMX-enabled for live updates to the job list.
    - **Employee/Admin tools**:
      - Icon-only button for bulk upload (gray text with green hover).
      - Primary `Post Job` button (blue with plus icon).

- **List container (`_job_list_partial.html`)**
  - Outer wrapper:
    - `bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden`.
  - Each row is a `flex` layout with:
    - **Left section**:
      - Job title as a bold link.
      - Optional `New` badge (green) for top-listed job.
      - Company and location in muted text with small icons.
    - **Middle section**:
      - Job type badge (gray outlined pill) and status badge (blue, gray, or yellow based on job status).
    - **Right section**:
      - Posted date in small gray text.
      - On hover:
        - View and Edit icons appear (for authorized users) with hover backgrounds and accent colors.
      - Chevron arrow icon on the far right moves slightly on hover.
  - **Empty state**
    - Centered message with magnifier icon and “No jobs found” heading, plus suggestion text.

- **Pagination**
  - Responsive:
    - On mobile: simple Previous/Next buttons.
    - On larger screens: page index, textual “Showing X to Y of N results”, and chevron navigation.

#### 3.7 Job Detail (`templates/jobs/job_detail.html`)

- **Purpose**
  - Rich, two-column job detail view capturing raw JD, metadata, and parsed JD output.

- **Page background**
  - Outer wrapper: `bg-gray-50 min-h-screen pb-12` for a soft full-height gray canvas.

- **Hero header**
  - Blue banner (`bg-blue-600 text-white shadow-lg`) across the top.
  - **Left cluster**:
    - Job ID badge (`JOB-{id}`) in a small monospaced chip.
    - “Back to Jobs” link with left arrow icon.
    - Main title: job title in large, bold font.
    - Meta row (company, location, type) with icons and pale blue text.
  - **Right cluster** (for authorized users):
    - Button group:
      - Edit (white/transparent overlay button).
      - Delete (red).
      - Parse JD (indigo).
      - Copy JD (blue).
    - All with icons and subtle background blur (`backdrop-blur-sm`) for a modern look.

- **Main content layout**
  - Container overlapped against header (`-mt-8`) for card-like effect.
  - **Left column (details)**:
    - White card with:
      - Metadata strip:
        - Status pill (green, red, or yellow).
        - Posted date.
        - Optional source link (“View Original”) if `original_link` exists.
      - Description section:
        - Section title “Description” with folder-like icon.
        - Body inside `prose`-styled div with `whitespace-pre-wrap` to preserve JD formatting.
  - **Right column (sidebar)**:
    - **At a Glance** card:
      - Posted by / edited by with initial avatars.
      - Marketing roles as indigo pill chips.
      - Salary range chip (green).
      - Job ID in a mono chip.
      - JD parse status indicator (OK/ERROR/N/A) with time of last parse and optional error string.
    - **Parsed JD Summary** (conditional on parsed data):
      - Compact grid of gray cards showing:
        - Role domain, seniority level, parsed title, and company.
      - Several sections of tag chips:
        - Required Skills, Preferred Skills, Tools & Technologies, Platforms & Services, Domain Terms, Certifications Preferred, ATS Keywords.
      - Expandable `<details>` for raw JSON representation of parsed JD.
    - **Apply section**:
      - If job is open: wide blue `Apply Now` button with hover animation.
      - If closed: disabled gray “Position Closed” button.

- **Interactions**
  - “Copy JD” button:
    - Uses JavaScript and `navigator.clipboard` to copy the raw `object.description`.
    - Button text temporarily changes to `JD Copied` and resets after a timeout.

#### 3.8 Settings Dashboard (`templates/settings/dashboard.html`)

- **Purpose**
  - Centralized admin settings hub: employees, marketing roles, platform configuration, LLMs, prompts, system health.

- **Layout**
  - Container: `container mx-auto px-4 py-8`.
  - Header:
    - `System Settings` title.
    - Subtitle: “Manage users, roles, and platform configuration.”
  - Content grid:
    - `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6`.

- **Settings cards**
  - Each setting area is a white card with:
    - Colored left border to indicate category (blue, purple, green, indigo, teal, red).
    - Top row: section title and small pill label (e.g., Access Control, Configuration, Active, AI, Monitor).
    - Short explanatory text (two-line height, trimmed with fixed `h-12`).
    - Primary button leading to the relevant management screen, styled with its category color.
  - Sections:
    - Employees (blue).
    - Marketing Roles (purple).
    - Platform Config (green).
    - LLM Settings (indigo).
    - Prompt Service (teal).
    - System Health (red).
    - Audit Log (amber) – recent actions for compliance and tracking.

### 4. Arrangement Principles & UX Guidelines

- **Consistent page scaffolding**
  - Every significant page:
    - Inherits the main nav + top bars from `base.html`.
    - Starts with a clear page title and short supporting text.
    - Presents KPIs or filters near the top, followed by lists or detail content.

- **Card-first information hierarchy**
  - KPIs appear in top rows using compact cards.
  - Lists and dashboards are built from white `card + divider` patterns.
  - Details pages (like job and consultant detail) use:
    - A hero/top section with strong color.
    - A body split into primary (left) and supplementary (right) columns.

- **Color semantics**
  - Reserve blue for navigation, primary CTAs, and neutral highlights.
  - Use green for positive / success, yellow/amber for pending or caution, red for error/closed.
  - Use indigo/purple/teal to visually group specialized product areas (AI, analytics, prompts).

- **Responsiveness patterns**
  - Grids are defined mobile-first (`grid-cols-1`) and add more columns only on `md`/`lg` breakpoints.
  - Flex containers often use `flex-col md:flex-row` to stack on mobile and align horizontally on larger screens.

- **Interaction patterns**
  - Hover states:
    - Cards and list rows gain subtle background tints and stronger shadows.
    - Action icons appear on hover to keep the base view clean.
  - Filters and search:
    - Live search is powered by HTMX for both jobs and consultants, using debounced keyup triggers.
  - Semantic badges and tags:
    - Use small, rounded pills with low-saturation backgrounds to provide dense-but-readable metadata.

- **Navigation & back links**
  - **Top nav (role-aware):** Admin: Dashboard, Jobs, Applications, Interviews, Calendar, Analytics, Messages, Consultants, Employees, ⚙️ Settings. Employee: Dashboard, Jobs, Applications, Interviews, Calendar, Messages, Consultants, Analytics. Consultant: Dashboard, Jobs, Applications, Interviews, Messages, Consultants, Saved Jobs.
  - **Mobile:** Hamburger toggles the nav menu (Alpine.js); `.nav-open` reveals `.nav-menu` on small screens.
  - **Back links:** List and detail pages expose a role-aware “← Dashboard” (or “← Applications”, “Consultants”, etc.) so users can return to the correct hub without relying only on the top bar.
  - **Settings hub:** System Settings dashboard links to Employees, Marketing Roles, Platform Config, LLM Settings (+ View Logs), Prompt Service, System Health. Sub-pages (LLM Config, LLM Logs, Platform Config, Prompt list, System Health) include “← Back to Settings” or equivalent.
  - **Admin dashboard quick actions:** Second row includes Settings, System Health, LLM Logs, LLM Config in addition to Post Job, Add Consultant, Analytics, Employees.

### 5. Summary

The EduTech app UI is a Tailwind-driven, card-based admin interface with a strong blue brand, semantic color coding, and role-specific dashboards. Screens consistently layer navigation, page headers, KPIs, and card-based content, with jobs, consultants, and settings all presented through reusable list and detail patterns. This document should be the reference for maintaining visual consistency and understanding how each major screen is structured and themed.

### 6. All UI screens (reference)

A full, exhaustive list of every screen, tab, and small UI surface (including partials, banners, and export endpoints) is maintained in **`docs_reference/ui-screens-list.md`**. It includes:

- **Global:** Base layout, maintenance banner, impersonation banner, nav, mobile menu, flash messages
- **Auth:** Home, login, logout, change password, password change done
- **Dashboards:** Admin, Employee, Consultant
- **Jobs:** List (and HTMX partial), detail, create, edit, delete confirm, bulk upload, export CSV
- **Applications:** List, detail, log/create, update, claim, export CSV, bulk status
- **Interviews:** List, detail, add, edit, calendar, export CSV
- **Consultants:** List (and partial), detail, add, edit, export CSV, saved jobs; experience/education/certification CRUD (self + admin); marketing roles CRUD; draft generate/preview
- **Employees:** List (and partial), detail, add, edit, export CSV
- **Settings:** Settings dashboard, platform config, audit log, system status, LLM config, LLM logs (list + detail), health JSON
- **Prompts:** List, detail, add, edit, delete confirm
- **Resumes & drafts:** Resume create/detail/download; draft detail and actions (set-prompt, regenerate, download, promote, delete, etc.)
- **Messaging:** Inbox, thread detail, start thread
- **Analytics:** Dashboard, export CSV

Use `ui-screens-list.md` for QA checklists, onboarding, or when you need the exact URL name or template path for any screen.


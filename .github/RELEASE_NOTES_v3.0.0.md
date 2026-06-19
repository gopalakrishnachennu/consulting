## Consulting v3.0.0 (major)

**Commit:** `fa0eff6` on `main`

### Highlights

- **Consultant workflow pipeline** — dashboard, HTMX panel, locks, stars, stale indicators
- **Job vetting pool** — filters, validation pipeline hooks, URL uniqueness checks, empty states
- **Resume tooling** — generation engine, master prompt admin, templates, draft review, export utilities
- **Companies** — enrichment helpers, company–job sync command, company type
- **Messaging** — thread org scoping, HTMX partials, search, typing indicators, notifications
- **Core** — broadcasts, enterprise notifications, platform config (job pool staging), audit utilities
- **Users** — consultant journey, preferred location, notification preferences
- **Ops** — CI workflow (`.github/workflows/ci.yml`), custom 403/404/500 pages

### Deploy notes

- **Run Django migrations** after pulling (multiple apps).
- Review **environment variables** and `PlatformConfig` (e.g. pool staging, enrichment keys).
- See repository `docs_reference/workflow-dashboard-urls.md` for workflow-related routes.

### Previous v2.x

- Prior tags: `v2.1.0`, `v2.0.0`, `v1.0.0`.

# Consulting — Claude Code Context

## Environment map (read this before suggesting any command)

There are three distinct environments. Always confirm which one is active before writing or running anything.

| Env | DB | Who triggers it | Env file |
|---|---|---|---|
| **local-dev** | Local Postgres (Docker) | Dev loop — safe to experiment | `.env` |
| **local-ops** | **Production Postgres on Hetzner** | Running ops/backfill commands locally for speed | `.env.harvester` |
| **production** | **Production Postgres on Hetzner** | GitHub Actions → SSH → VPS | `.env.production` (on VPS) |

`local-ops` and `production` hit the **same database**. A command run locally with `.env.harvester` is a prod write. Treat it identically to a prod command.

## How to tell which env is active

```bash
# Check what DATABASE_URL is set to
grep DATABASE_URL .env.harvester

# If it contains the Hetzner IP or chennu.co → you're on prod DB
# If it contains localhost/127.0.0.1 → you're on local DB
```

## Running ops commands locally (the fast path)

Use the local-harvester compose file. It mounts the local code but connects to prod DB via `.env.harvester`.

```bash
# Any management command
docker compose -f docker-compose.local-harvester.yml run --rm harvester \
  python manage.py <command> [args]

# Examples matching the GitHub Actions workflows:
docker compose -f docker-compose.local-harvester.yml run --rm harvester \
  python manage.py evaluate_rawjob_scope --only-unscoped --batch-size 1000 --dry-run

docker compose -f docker-compose.local-harvester.yml run --rm harvester \
  python manage.py classify_job_domains --batch-size 1000 --dry-run

docker compose -f docker-compose.local-harvester.yml run --rm harvester \
  python manage.py backfill_job_marketing_roles --dry-run

docker compose -f docker-compose.local-harvester.yml run --rm harvester \
  python manage.py refetch_ambiguous_locations --limit 1000 --dry-run
```

**Always run with `--dry-run` first.** Every ops management command supports it.

## Settings files and what they're for

| File | Use case |
|---|---|
| `config/settings.py` | Web server + Celery workers (prod and local-dev) |
| `config/settings_local_harvester.py` | Ops/backfill commands run locally — no web stack, Celery runs eagerly in-process |

## Docker compose files

| File | What it runs |
|---|---|
| `docker-compose.yml` | Full local dev stack: web + celery_worker + celery_beat + redis + db |
| `docker-compose.prod.yml` | Production stack on Hetzner |
| `docker-compose.local-harvester.yml` | Local machine → prod DB. CLI ops only, no web server |

## GitHub Actions workflows and their local equivalents

All `run-*.yml` workflows SSH into Hetzner and run a management command on the VPS. You can run the same command locally (faster) by using the local-harvester pattern above.

| Workflow | Management command |
|---|---|
| `run-evaluate-rawjob-scope.yml` | `evaluate_rawjob_scope` |
| `run-classify-job-domains.yml` | `classify_job_domains` |
| `run-backfill-marketing-roles.yml` | `backfill_job_marketing_roles` |
| `run-backfill-enrichment.yml` | `enrich_existing_jobs_task` (via shell) |
| `run-validate-links.yml` | `validate_raw_job_urls_task` (via shell) |
| `run-refetch-ambiguous-locations.yml` | `refetch_ambiguous_locations` |
| `run-mapbox-full-setup.yml` | `configure_geocoding_provider` + `evaluate_rawjob_scope` |

## Key safety rules for ops commands

1. **`--dry-run` first, always.** No exceptions for commands that modify more than 100 rows.
2. **Check for concurrent Celery workers.** If prod Celery is running a related task (e.g. scope evaluation), don't run the same command locally in parallel — you'll get lock contention.
3. **`--limit` when testing.** Use `--limit 50` or `--limit 100` to validate behavior before going full-scale.
4. **`--id-gt` / `--id-lte`** are available on `evaluate_rawjob_scope` to shard large runs by ID range.

## Apps layout

```
apps/
  harvest/     — job harvesting engine (Jarvis HTTP, platform scrapers)
  jobs/        — Job model, filtering, management commands
  users/       — Auth
  companies/   — Company model
  submissions/ — Applications
  resumes/     — Resume processing
  analytics/   — Reporting
  messaging/   — Notifications
  interviews_app/
```

## Production server

- Hetzner VPS at `chennu.co` / `62.238.6.14`
- Stack managed via `docker-compose.prod.yml`
- Deployed via `deploy.sh` (sources `.env.deploy`) or `deploy-vps.yml` GitHub Action

## Deploy workflow

**Always follow this exact sequence:**

```
git push origin main
→ Wait for "Build & publish Docker image" workflow (GH Actions)
→ gh workflow run deploy-vps.yml -f confirm=DEPLOY
→ Wait for deploy workflow
→ Verify with curl https://chennu.co/
```

**NEVER deploy before the Docker build completes** — it will pull the old image.

## Subagent patterns

Use parallel subagents when:
- Editing view + template simultaneously (independent files)
- Running tests while checking deployment status
- Code review on one file while implementing changes in another

Use sequential (foreground) agents when:
- Need to read a file's content before deciding what to edit
- Deploy depends on build completing first

## Template conventions

**BEFORE building ANY UI, read `docs/ui-style-contract.md` and reuse the documented
component classes.** Do NOT invent new one-off card/button/badge/table styles — this is
the #1 cause of UI drift. Add a new pattern only if nothing in the contract fits, and
document it in that file in the same session.

Hard rules from the contract:
- **Brand color ≠ status color.** Brand (`--brand-*`, `.btn-primary`, `.brand-link`) =
  navigation/primary actions. Status (fixed `emerald/blue/violet/amber/red/gray`) =
  success/warning/error/info. Never use brand vars for a status badge.
- Every list view needs empty + loading + error + no-permission states.
- Destructive actions (delete/purge/bulk) = red button + confirm modal + impact text + POST.
- Long text (titles, companies, URLs) = `truncate`/`line-clamp-2`/`overflow-x-auto`.
- Admin-only UI gated in BOTH view (`test_func`) and template (`{% if is_admin %}`).

Stack:
- All templates use Tailwind CSS utility classes
- Alpine.js for interactivity (x-data, x-show, @click, x-model)
- HTMX for partial page updates (hx-get, hx-trigger, hx-swap)
- Django template tags: `{% load humanize widget_tweaks %}`
- Partials go in `templates/<app>/_partial_name.html`
- Dashboard partials: `templates/core/dashboard_partials/_dashboard_*.html`

## Model gotchas

- Job model: use `company_obj` not `company` for FK (has `company` property)
- ConsultantProfile: `submissions` is the related name for ApplicationSubmission
- EmployeeDesignation: `allowed_features` M2M to FeatureFlag (changes affect ALL employees with that designation)
- Always `select_related('consultant__user')` when template accesses `sub.consultant.user`

## AI handoff / current work pointer

For new AI sessions, read this file first, then read:

```bash
docs_reference/ai-handoff-current-state.md
```

Current important product direction:

- Raw Jobs filtering must use standardized fields, not loose text filters.
- Country filter source of truth: `RawJob.country_code`.
- Marketing role/domain filter source of truth: `RawJob.job_domain`, matched to `MarketingRole.slug`.
- If filters look empty, standardize/backfill first with `evaluate_rawjob_scope` and `classify_job_domains`, using `--dry-run` before writes.
- Do not deploy or commit unrelated dirty files. Commit only the files involved in the requested change.

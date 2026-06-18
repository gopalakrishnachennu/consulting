# Classification Workspace Restructure Plan

This document is the rollout contract for restructuring the harvest -> classification -> vetting flow without breaking current production behavior.

## Why this exists

The current system works, but the operator workflow is spread across:

- `apps/harvest/views.py`
- `apps/jobs/views.py`
- `templates/harvest/rawjob_detail.html`
- `templates/jobs/pipeline.html`
- `templates/settings/platform_config.html`

That shape is difficult to scale safely for future AI features.

The restructuring goal is to move toward a clearer operating model while preserving:

- current database schema compatibility
- current URL compatibility
- current Celery task behavior
- current audit trail behavior
- current effective approved-classification override behavior

## Product intent

This project is an operations-heavy recruiting pipeline system, not a generic public jobs site.

The product shape is:

1. Harvest source jobs
2. Normalize and classify raw intake
3. Review and approve classification
4. Push into vetting
5. Approve/reject in pool/live workflow
6. Feed resume, matching, and downstream employee workflows

The UI should therefore optimize for:

- dense but readable operator workflows
- audit visibility
- queue throughput
- explainability
- progressive disclosure

## Non-goals

These changes must not become:

- a big-bang rewrite
- a schema-reset exercise
- a frontend/backend split rewrite
- a provider-specific Codex/Claude UI

## Architecture boundaries

### `apps/harvest`

Owns:

- sources
- raw fetches
- raw payload archives
- source health
- link health at the raw layer
- JD fetch state

Should not own long-term classification review workflow.

### `apps/jobs`

Owns:

- classification snapshots
- provider runs
- merged outputs
- review and approval
- push-to-vetting decisions
- pool/live downstream workflows

### `apps/core`

Owns:

- runtime/provider configuration
- platform-wide settings
- ops visibility
- feature flags

Should not be the dumping ground for workflow-specific settings.

## Target operator workspaces

### 1. Raw Intake

Purpose:

- source jobs
- source payloads
- JD fetch/debug
- source health

Primary pages:

- Raw Jobs list
- Raw Job detail
- Sources
- Schedule
- Run Monitor

### 2. Classification

Purpose:

- backend classification
- secondary classification
- merged result
- verifier warnings
- approval
- push eligibility

Primary pages:

- Classification Queue
- Classification Detail
- Classification Settings
- Classification Metrics

This is the future home for additional AI providers and comparison workflows.

### 3. Vet Queue

Purpose:

- approve/reject
- blocked reasons
- lane decisions
- downstream queue actions

Primary pages:

- Vet Queue
- Approved queue
- Blocked queue
- Archived queue

### 4. Routing & Rules

Purpose:

- intake rules
- role routing
- sync thresholds
- warning-push policy
- country/domain policy

### 5. Ops

Purpose:

- worker health
- queue health
- backlog
- historical backfills
- provider/runtime health

## Migration strategy

Use a strangler pattern:

1. Keep old flows alive
2. Build new classification surfaces beside them
3. Reuse current services underneath
4. Move traffic gradually
5. Reduce old surfaces only after parity is proven

## Safety rules

1. Old URLs must continue to work until replacement parity is proven
2. Current DB schema must remain compatible during the migration
3. Approval/push audit data must never be dropped
4. Vetting must continue to consume effective approved classification
5. No page loses visibility before its replacement exists
6. Dangerous actions stay POST + confirmed
7. Every rollout slice requires parity tests before deployment

## Feature-flag rollout

The restructure is gated behind disabled-by-default flags:

- `employee_classification_workspace_v2`
- `employee_classification_settings_v2`
- `employee_classification_metrics_v2`
- `employee_legacy_rawjob_review_bridge`

Rules:

- new workspace pages stay off by default
- legacy review remains available until the new workspace is proven
- enabling a new flag must not change database semantics

## Phase plan

### Milestone 1: Foundation

Deliverables:

- this document
- rollout feature flags
- helper utilities for route gating

No workflow behavior changes.

### Milestone 2: Classification Queue V2

Deliverables:

- dedicated queue page
- compatibility links from existing review surfaces
- no removal of old review controls yet

### Milestone 3: Classification Detail V2

Deliverables:

- dedicated detail page
- compare panel
- approve/push workflow
- raw JSON demoted to debug-only

### Milestone 4: Settings split

Deliverables:

- classification settings page
- old platform-config classification controls marked legacy or moved

### Milestone 5: RawJob detail cleanup

Deliverables:

- source/JD/history/debug-only rawjob detail
- links to classification workspace

### Milestone 6: Module split

Deliverables:

- split oversized views/templates into focused modules
- keep compatibility imports/routes

### Milestone 7: Metrics

Deliverables:

- classification throughput
- review queue health
- conflict rates
- provider/runtime failure visibility

## Acceptance criteria for each milestone

A milestone is only valid when all of these are true:

1. `python3 manage.py check` passes
2. migration dry-run is clean
3. targeted tests pass
4. new UI renders without regressing old entry points
5. old URL behavior remains valid
6. no audit fields are lost

## Edge cases to keep in scope

- JD changed after a classification run was queued
- approved result exists and a new run arrives
- manual override should survive reruns unless explicitly reset
- already-synced job receives a new approved result
- push-to-vetting must stay idempotent
- provider returns malformed or partial JSON
- dead jobs must not consume expensive classification
- warning-pushed jobs must remain visible downstream
- historical backfill must not starve live intake
- reviewer must see provenance and why a job entered review

## Decision rule for future changes

Before implementing a restructuring slice, verify it does at least one of these:

1. reduces operator confusion
2. reduces ownership ambiguity
3. reduces raw/approved/job drift risk
4. improves future AI extensibility
5. shrinks a giant page/view instead of enlarging it
6. can be verified with tests instead of assumption

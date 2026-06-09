# New Feature Flow

A structured intake for any new feature so nothing slips — **even when the ask is vague or half-described.**
Given a feature idea, run these phases IN ORDER. Don't skip ahead to coding. The whole point is to
surface fit, suggestions, the plan, and the edge cases the user *didn't* mention — before building.

## Phase 0 — Capture & clarify
- Restate the feature in ONE sentence so we agree on what it is.
- Read enough of the codebase to infer the obvious answers yourself (don't ask what the code already says).
- Then ask **2–4 sharp clarifying questions** (use AskUserQuestion) ONLY for decisions that change the
  design and can't be inferred — scope, who it's for, must-have vs nice-to-have, data source.
- Explicitly call out anything the user likely *forgot* to specify and propose a sensible default.

## Phase 1 — Fit check (does it belong here?)
- Locate where it would live: which app (`apps/…`), models, views, templates, pipeline.
- Does it fit the current architecture or fight it? Note conflicts/overlap with existing features.
- Reuse first (per CLAUDE.md): what existing code/patterns/components should it build on instead of new code?
- **Verdict:** ✅ Good fit · ⚠️ Fits with caveats · ❌ Doesn't fit — and if not, propose a better approach.

## Phase 2 — Suggestions & alternatives
- Offer 1–3 approaches with honest tradeoffs; **recommend one** and say why.
- Flag scope creep. Define the smallest slice that delivers real value (ship that first).

## Phase 3 — Plan
- Step-by-step implementation plan: files to touch, model/migration changes, endpoints, UI, settings/flags.
- Backward compatibility + data/migration impact. Use a feature flag if it's risky or big.
- Note which parts are reversible vs not.

## Phase 4 — Edge cases (the part that gets forgotten)
- Enumerate edge cases explicitly and say how each is handled. Always consider:
  - Empty / missing / malformed data; partial records
  - Permissions & roles (superuser / ADMIN / EMPLOYEE / CONSULTANT)
  - Concurrency / double-submit / race conditions
  - Large inputs & prod-scale data (137k+ RawJobs) — pagination, query cost, N+1
  - Failure modes (LLM down, external API error, timeout) and what the user sees
  - Truthfulness/safety for resume/AI features (no fabrication); destructive/prod-data actions
  - Mobile / responsive; preview-vs-export parity for resume/editor features
  - Security: authz on every new endpoint, input validation, no secrets leaked
- List these **before** implementing — get a quick nod if any change scope.

## Phase 5 — Implement (advanced, robust)
- Build per the plan and the project's CLAUDE.md rules (reuse-first UI, env safety, `--dry-run` for ops).
- Handle the Phase-4 edge cases in code, not just in prose.
- Add/extend tests for the new behavior + the key edge cases.

## Phase 6 — Verify & ship
- `python manage.py check` and run the touched app's tests (built-in Postgres CI mirrors this).
- Ship via the **/release** flow (or /deploy) — never ship a red build.
- Verify live (health + the feature actually works) and report what changed.

## Rules
- ALWAYS produce the fit verdict + edge-case list **before** writing implementation code.
- If the feature is large, phase it and ship the first slice; don't big-bang it.
- If it touches prod data, money, or safety, flag it and get explicit confirmation before any destructive step.
- Proactively raise what the user forgot — that's the job of this flow.

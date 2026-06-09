# Modify Existing Behavior

Structured flow for **changing something that already exists** — a fix, tweak, refactor, or improvement.
Use this (not `/feature`) when the code/feature is already there. The risk here isn't "build it right",
it's **"don't break what already works, or the data already created under the old behavior."**

Run these phases IN ORDER. The map-the-blast-radius and backward-compatibility steps are the whole point.

## Phase 0 — Pin down what exists today
- Find the current implementation: the exact files, functions, templates, models, settings.
- Read it. State precisely how it behaves **now**, and what it should behave like **after**.
- Confirm the change in ONE sentence.

## Phase 1 — Blast radius (who/what depends on this)
- Grep for **every** reference: call sites, imports, template `{% url %}` / includes, JS handlers, tests.
  Don't edit until you've listed them all.
- What **data already exists** in the shape this code produces or reads?
  (ResumeDraft content, cached `sections_json`, `template_overrides`, saved rows, prior migrations.)
- What other features rely on the current behavior?

## Phase 2 — Regression & compatibility risks
- List concrete things that could break.
- **Backward compatibility:** rows/drafts/cached state created under the OLD behavior — do they need a
  migration, a re-derive on read, a version bump, or a fallback? (This codebase's pattern: `PARSER_VERSION`
  re-parses stale `sections_json`; `template_overrides` merged for preview==export.)
- **Reversibility:** can this roll back cleanly? Is it destructive (drops a column, deletes/rewrites data)?

## Phase 3 — Plan the change (minimal, safe)
- The smallest change that achieves the goal. Prefer additive/reversible over destructive.
- Migration plan if the data shape changes. Feature-flag if risky or broad.
- Name exactly which files change and why. Keep the old path working unless explicitly removing it.

## Phase 4 — Edge cases (existing-state focused)
- Stale/old data created before the change — does it still render/parse/behave correctly after?
- Partial records, in-flight users, half-saved editor state. Don't silently clobber user edits.
- Plus the usual: roles/permissions, empty/large data, concurrency, failure modes, security on changed
  endpoints, and (for resume/editor changes) **preview == DOCX == PDF** parity.

## Phase 5 — Implement
- Make the change; keep old data working via migrate / re-derive / fallback as needed.
- Update and EXTEND tests — include a test that OLD-shape data still works, not just the new path.

## Phase 6 — Verify & ship
- `python manage.py check` + run the affected apps' tests.
- Verify live that the change works AND that pre-existing data still works.
- Ship via the **/release** flow — never ship a red build.

## Rules
- ALWAYS map the blast radius (grep every reference) BEFORE editing.
- ALWAYS account for data already created under the old behavior (migrate / re-derive / version-bump / fallback).
- Prefer reversible. For destructive changes (drop column, delete/rewrite data) get explicit confirmation first.
- If a Django model field or parser/render shape changes, ask: "do existing rows/drafts need re-processing?"

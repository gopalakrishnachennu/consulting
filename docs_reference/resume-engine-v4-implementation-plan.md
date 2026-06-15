# Resume Engine V4 — Implementation Plan

Status: **PLAN ONLY — not implemented.** Adds layers to the existing Django pipeline;
does **not** rebuild. Everything ships behind a feature flag (`resume_engine_v4`, default OFF).

## Goal & non-goals

**Goal:** fix the felt symptom — API resumes are *generic, over-keyworded, not natural,
sometimes claim skills the consultant can't defend*. Do it by adding a **Strategy stage**,
**skill-safety from existing matching**, a **DB-guide layer** (role/domain/edge-case), a
**facts-frozen humanizer**, and a **validate→rewrite loop** — onto the current pipeline.

**Non-goals (deferred, deliberately):**
- No Node/TS rewrite. Stay Django + Celery + Postgres.
- No verified-skills taxonomy DB (use matching-derived safety instead).
- No vector RAG yet (DB/JSON guides now; vectorize only if the library grows large).
- No multi-consultant batch scoring, output/generation modes, LinkedIn consistency — Tier 3.

## Current state (verified in code)

`apps/resumes/pipeline/__init__.py :: generate_resume_pipeline(job, consultant, actor, input_sections)`:
- Phase 1 `jd_intelligence.get_jd_intelligence(job)` — JD parse (0 tokens). ✅
- Phase 2 `matching.build_compatibility_matrix(consultant, jd_intel)` → `match_pct,
  matched_required, missing_required, coaching_keywords, warnings`. ✅
- Deterministic sections: `generators/header,education,certifications` (0 tokens). ✅
- Phase 3 **ONE** LLM call via `prompts.build_prompt(jd_intel, matching, consultant,
  header, education, certifications, admin_rules)` → whole body (summary+skills+experience). ⚠️ monolithic
- Phase 4 `utils.validate_resume` + `utils.score_ats` + `guardrails.run_guardrails`
  (status pass/review/block) → `metadata["review_status"]`. ✅ (no auto-rewrite)
- Stored on `ResumeDraft` (versions, `review_status`) + `PipelineRun`
  (jd_intelligence, matching_matrix, section_results, quality_gate). ✅

So ~70% exists. V4 inserts 4 stages + a guide layer + a rewrite loop around Phase 3/4.

## Target pipeline (bracketed = new)

```
jd_intelligence  →  matching  →  [resume_scope]  →  [guide_retrieval]  →  [strategy]
   →  build_prompt (now strategy/scope/guide-aware)  →  LLM body
   →  [humanizer]  →  validate_resume + guardrails + quality_gate
   →  [rewriter loop ×≤2 on fail]  →  ResumeDraft + PipelineRun
```

## New data models (small, Django-native)

New file `apps/resumes/guide_models.py` (or extend `models.py`), migration only:

- **`RoleWritingGuide`**: `role_family` (slug), `sub_role`, `seniority`, `strong_focus`
  (JSON list), `bullet_style` (text), `keywords` (JSON), `avoid` (JSON), `version`,
  `status` (active/draft), `updated_at`. Seed ~15 role families from blueprint §22.
- **`DomainWritingGuide`**: `domain` (healthcare/finance/retail/…), `safe_terms` (JSON),
  `avoid` (JSON), `version`, `status`. Seed from blueprint §23.
- **`EdgeCaseGuide`**: `edge_case` (graduate_role/career_switch/short_tenure/…),
  `trigger` (JSON signals), `rules` (JSON), `priority`, `status`.
- **`BulletPatternGuide`**: `role_family`, `pattern` (text, *style only, never copied*),
  `skill_tags` (JSON), `approval_status`. Optional for P1; can defer to P4.
- **Company→domain map**: reuse existing `job_domain`/company data first; only add a
  `CompanyDomainOverride` table if needed.

Strategy + scope are **not** new tables — store as JSON on `PipelineRun`
(`resume_scope` JSONField, `strategy` JSONField) so they're audited per run.

## New pipeline stages (files + contracts)

1. **`pipeline/resume_scope.py`** — `build_resume_scope(matching, consultant, jd_intel) -> dict`
   *Skill-safety lite, 0 tokens, no new data entry.* Uses matching's `matched_required`
   vs `missing_required`:
   - `write_strong`: matched skills the consultant demonstrably has → bullets/summary OK.
   - `soften`: JD-preferred but only partially matched → adjacent/light wording.
   - `exclude`: `missing_required` not present anywhere in profile → never claim.
   - Also derives tenure-per-company (for #8) and seniority (resume vs JD, for #2).
   Output feeds strategy + build_prompt + guardrails.

2. **`pipeline/guide_retrieval.py`** — `retrieve_guides(role_family, sub_role, seniority,
   domain, jd_keywords) -> dict` Queries the new DB guides (active only), caps at
   **1 role + 1 domain + ≤2 edge-case + ≤5 bullet patterns** (blueprint §38). Fallback
   when no guide: closest role family + `guide_missing=true` flag (§40).

3. **`pipeline/strategy.py`** — `build_strategy(jd_intel, matching, scope, guides,
   consultant) -> dict` **One structured-JSON LLM call**, temp 0.1–0.2. Produces
   `target_title, summary_angle, per_company_focus, terms_to_include/soften/exclude,
   tone, resume_length, project_decision, required_items`. This is the core "not generic"
   fix. Stored on `PipelineRun.strategy`.

4. **`pipeline/humanizer.py`** — `humanize(content, strategy) -> str` Optional post-pass,
   temp 0.25–0.35. **Facts frozen**: may only vary verbs/phrasing/reduce repetition;
   prompt forbids changing names/dates/companies/numbers/skills. Runs **before**
   guardrails so any drift is re-caught.

5. **`pipeline/rewriter.py`** — `rewrite(content, feedback, strategy, scope) -> str`
   Drives the loop: on guardrail/quality fail, rewrite once with the failure list, re-validate,
   then settle to `review_required` if still failing (max 2 passes, blueprint §15).

## Integration into `generate_resume_pipeline` (exact insert points)

- After Phase 2 matching → call `build_resume_scope(...)` then `retrieve_guides(...)`
  then `build_strategy(...)`; add all three to `metadata` + `PipelineRun`.
- Phase 3 `build_prompt(...)` gains kwargs `strategy=`, `scope=`, `guides=` (extend
  `prompts.build_prompt` signature; thread into the prompt body). Generators stay 0-token.
- Phase 3.5 → `humanize(content, strategy)` (flag-gated).
- Phase 4 → after `run_guardrails`, if `status == "block"` (or quality_gate fail) and
  rewrite budget remains → `rewrite(...)` → re-validate. Persist final `review_status`.
- Everything new is wrapped in `if feature_flag('resume_engine_v4')` so V3 stays the
  default until validated.

## Tier-1 edge cases → owner stage

| Edge case | Owner |
|---|---|
| #1 role ambiguity / blend | `jd_intelligence` (emit primary/secondary + blend) → `strategy` positioning |
| #2 seniority mismatch | `resume_scope` (derive) → `strategy` (tone) |
| #3 JD special instructions (grad date, availability) | `jd_intelligence` extract → `strategy.required_items` → `guardrails` block-if-missing |
| #4 company-domain mismatch | `guide_retrieval` (domain guide) → `strategy.per_company_focus` |
| #6 keyword density | `quality_gate` (measure repeats) → `rewriter` instruction |
| #7 metric density | `guardrails`/`quality_gate` (cap unprovided metrics) |
| #8 short-tenure bullet count | `resume_scope` (tenure) → `strategy` (bullet_count per company) |
| #35 strategy adherence | `quality_gate` (did output honor strategy terms?) |
| #45 repeated-company responsibilities | `strategy` (distinct focus per company) → `quality_gate` similarity check |

## Tier-2 validator checks (fold into `guardrails.py` / `quality_gate.py`, deterministic)

#13 skills overload · #15 education/grad-date present · #21 tense consistency ·
#22 verb variety · #27 length vs experience · #28 contact completeness ·
#30 ATS format (tables/columns/icons) · #34 section coherence (summary↔experience↔skills).

## Phasing (each shippable, flag-gated, with its own release + tests)

- **P1 — Scope + Strategy (highest impact).** `resume_scope.py`, `strategy.py`, extend
  `build_prompt`, store on `PipelineRun`. Edge cases #2,#8,#35,#45. *Deliverable: resumes
  stop over-claiming missing skills and follow a per-company angle.*
- **P2 — Rewrite loop + Tier-2 checks.** `rewriter.py`, keyword/metric density (#6,#7),
  Tier-2 validator checks into `guardrails`/`quality_gate`. *Deliverable: auto-lift quality,
  fewer manual reviews.*
- **P3 — Guide layer.** Guide models + seeds (§22/§23), `guide_retrieval.py`, wire into
  strategy/prompt. Edge cases #1,#3,#4. *Deliverable: role/domain-specific, defensible wording.*
- **P4 — Humanizer + bullet patterns.** `humanizer.py` (facts-frozen), `BulletPatternGuide`.
  *Deliverable: natural tone, varied verbs, less AI-looking.*
- **P5 (deferred) — controlled RAG + evals dashboard.** Only if guide library outgrows
  manual selection; vectorize guides (pgvector), add eval test-cases (blueprint §31).

## Testing

- Unit: `resume_scope` (matched→strong, missing→exclude), `strategy` JSON schema valid,
  guide retrieval caps + fallback, rewrite-loop terminates ≤2, humanizer preserves facts
  (diff name/dates/numbers == none).
- Golden cases (blueprint §31 subset): graduate JD, senior JD, role-switch JD, missing
  grad-date JD → assert `review_status`/required_items behavior.
- Regression: V3 path unchanged when flag OFF.

## Rollout
Feature flag `resume_engine_v4` (default OFF). Enable per-run for A/B against V3, compare
ATS + guardrail pass-rate + manual "feels natural" review on a handful of real JD×consultant
pairs before defaulting ON.

## Explicitly NOT doing now
Node rewrite · 18 UUID tables · vector DB · verified-skills DB · batch 10k throttling ·
output/generation modes · LinkedIn consistency · confidential-client/work-auth formatting.
All are Tier-3 — revisit only when a real case demands them.
```

# RawJob ↔ Job consistency audit

Why the same posting shows different numbers on `/harvest/raw-jobs/<id>/` vs `/jobs/<id>/`.

## Root cause (one sentence)
`RawJob → sync_harvested_to_pool → Job` is a **one-time snapshot + recompute**, not a live link.
After sync, the **RawJob keeps evolving** (JD backfill, country resolution, re-classification,
re-gating) while the **Job is frozen at sync-time** except for its own *independent* recomputations.
So similar-named fields drift, and "quality" means two different formulas.

Reference example: Raw Job `33060` (quality **0.60**, "Ready for resume") vs Job `11948`
(quality **0.875**, validation 72/100 "2 weeks ago", JD "Not parsed").

## Field-by-field map (from `tasks.py` sync, ~line 3086–3122)

| Concept | RawJob field | Job field | At sync | Drift risk | Why it looks inconsistent |
|---|---|---|---|---|---|
| **Quality** | `quality_score` (harvest formula) | `quality_score` = `jobs.quality.compute_quality_score(job)` | **recomputed, different formula** | High (by design) | same word "quality", two formulas → 0.60 vs 0.875 |
| **Validation** | gate `vet_priority_score` | `validation_score` (= gate×100 at sync) | snapshot, never re-run | High (staleness) | shows "checked 2 weeks ago" |
| **JD content gate** | `jd_gate_decision` ("Ready for resume") | — | not carried to Job | — | RawJob-only concept |
| **JD structured parse** | — | `parsed_jd` / `parsed_jd_status` ("Not parsed") | **not set at sync** (separate LLM step) | High (often never runs) | Job-only; reads as contradicting the RawJob gate |
| **Description (JD text)** | `description` (can be backfilled *after* sync) | `description` (snapshot at sync) | copied snapshot | Medium | Job JD lags RawJob JD backfill |
| **Country / scope** | `country`, `country_codes`, `scope_status` (Unknown-Country resolver upgrades these later) | `country` (snapshot) | copied snapshot | **High** | resolver updates RawJob, not the Job → the "Unknown Country 1043" backlog |
| **Classification / domain** | `category_data`, `domain` | `marketing_roles`, `department` (+ independent `needs_reclassification` reclassify) | copied at sync, then recomputed separately | Medium | two classifiers can disagree |
| **Title / company / location** | `title`/`company_name`/`location_*` | `title`/`company`/`location` (truncated snapshot) | copied snapshot | Low | rarely changes; truncation (512→200) can differ |
| **Remote / job_type** | `is_remote`/`employment_type` | `job_type` | copied (UNKNOWN→FULL_TIME) | Low | default coercion |

## What's actually wrong (vs. just different-by-design)
1. **Confusing labels** — two "quality" numbers from different formulas, "Ready for resume" next to "Not parsed". (Presentation problem.)
2. **Staleness** — `validation_score`, `parsed_jd`, and `country` on the Job freeze at sync while the RawJob moves on. (Real correctness problem.)
3. **No propagation** — when the RawJob's description/country is enriched after sync, nothing pushes it to the Job.

## Recommended fix order
1. **Labels (low risk, do first):** disambiguate in the UI — "Harvest quality" vs "Pool quality (recomputed)", "JD content gate" vs "JD structured parse", and add "as of <date>" to validation/parse. Kills the confusion without changing any scoring.
2. **Freshness (medium):** on the Job page show last-updated for validation/parse/country + a "Refresh from latest RawJob" action that re-pulls description/country and re-runs parse + validation.
3. **Propagation (medium):** when a RawJob's description/country/scope updates post-sync, propagate to the linked Job (signal or a periodic "refresh synced jobs"). Highest-value target: **country** (clears the Unknown-Country drift) and **description→parse**.
4. **Canonical quality (optional, bigger):** pick ONE quality definition the Job displays, label the other explicitly.

This is the same pattern anywhere a value is *copied* from RawJob to Job at sync and then independently changes on either side — country and description/parse are the ones that actually bite.

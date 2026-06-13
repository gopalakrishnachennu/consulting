# Releases

Production release log for GoCareers. Newest first. Created/updated by the `/release` skill
(verify CI green → version + changelog → deploy → health-verify → auto-rollback → record).

## v4.4.5 — Location Resolution Ladder (E+A+B+C+D) (2026-06-13)
Attacks the root causes of unknown-country jobs across ALL platforms. Audit found the
local gazetteer was only 151 cities / 15 countries (Spain & Mexico weren't even in it),
and Mapbox only ran on the manual button. Built a layered resolution ladder:
- PHASE A (gazetteer): added geonamescache (offline) — gazetteer 151 → 36,641 cities /
  237 countries, accent-folded (São Paulo), curated aliases still win. Madrid→ES,
  Tijuana→MX, Pune Research Campus→IN now resolve with zero API cost. Disambiguation
  verified intact (London→GB, "San Francisco, CA"→US, Berlin→DE).
- PHASE B (remote): resolver now pulls a country clue from the remote string, title,
  or JD ("Remote - US", "Engineer (Remote, UK)"). New HarvestEngineConfig.remote_unknown_policy
  (review/target/cold) for bare "Remote" with no country; surfaced on the review page.
- PHASE C (provider): decoupled on-demand Mapbox from auto-during-harvest. The review
  "Re-evaluate with Mapbox" button + sweep now work via force_provider even when the
  global auto-flag is off — caps (80k/mo, 1k/hr) + LocationCache dedup still enforced.
  (Fixes a latent bug: the Mapbox button did nothing unless auto-Mapbox was globally on.)
- PHASE D (backlog): new `sweep_unknown_country_locations` command re-resolves the
  REVIEW_UNKNOWN_COUNTRY backlog with the current offline resolver (no re-fetch — fast),
  --dry-run/--limit/--provider/--include-inactive, reports cleared count + outcomes.
- PHASE E (telemetry): "Why unresolved" breakdown on the review page (multi-placeholder /
  remote / office-label / named-place / blank) so each phase's impact is visible.
- +7 tests (gazetteer, remote, policy, force-provider, sweep, classifier, page render).
Status: deployed (754597e · CI+image green · health overall=ok · 2026-06-13)
Follow-up: run `sweep_unknown_country_locations --dry-run` to project the backlog drain
now that the gazetteer covers 36k cities.

## v4.4.4 — Location Review: hide delisted jobs (2026-06-13)
Follow-up to v4.4.3, driven by evidence: a Workday dry-run refetch resolved only
28 of 889 rows (861 returned no location — the detail pages are gone). Conclusion:
the stuck "N Locations" backlog is mostly expired postings, not a fetch bug. The
v4.4.3 code fix still prevents NEW multi-location jobs from going unknown.
- Unknown-Country review queue now hides is_active=False (delisted/expired) jobs by
  default — you only review live postings. ?include_inactive=1 shows everything, and
  a banner reports how many are hidden (full transparency, no silent data loss).
- The revived daily harvest will mark the remaining stale reqs inactive as it
  re-crawls Workday, so they drop off the queue over the next runs.
- +1 test (default hides delisted, include_inactive shows all).
Status: deployed (531ffdf · CI+image green · health overall=ok · 2026-06-13)

## v4.4.3 — Location Review overhaul: Workday root-cause + bulk classify (2026-06-13)
Attacks the Unknown-Country queue (1,004 pending; 776 Workday) on two fronts —
auto-resolve the bulk at the source, and make the manual tail a fast bulk sweep.
- ROOT CAUSE (#1, ~60% of queue): Workday's list API returns a count placeholder
  ("2 Locations") instead of the real list. Both the live harvester
  (workday.py `_workday_location_candidates`) and the re-fetch path (jarvis.py
  `_workday_location`) now read the detail endpoint's `location` + `additionalLocations`
  fields. Critically, no longer early-returns on the primary location — a Madrid
  (non-target) primary no longer hides a London (target) additional location.
  The existing `refetch_ambiguous_locations` command (filter already matches
  "N Locations") will now actually clear these rows.
- MANUAL CLASSIFY (#2): new "Assign country" action on the review page — pick from a
  curated list; target countries → Priority Target, others → Cold. (Previously the
  page could only re-run the resolver that had already failed.)
- BULK BY STRING (#3): the Top Location Strings panel is now clickable — assign a
  country / mark target / cold / Mapbox for EVERY job sharing that exact string in
  one click (turns "104 × Remote" into one decision).
- GAZETTEER (#4): label-stripper now catches HQ / Global HQ / Headquarters / Campus /
  Research Campus (e.g. "San Francisco-HQ" → San Francisco, "NYC Global HQ" → NYC).
- Fixed a latent bug: the review POST handler never returned a redirect (actions
  fell through to None).
- +7 tests (Workday additionalLocations, dict-shaped entries, label strip, assign
  target/non-target, bulk-by-string); green.
Status: deployed (c6a712d · CI+image green · health overall=ok · 2026-06-13)
Follow-up: run `refetch_ambiguous_locations` once to clear the ~600 already-stuck
"N Locations" rows (the code fix only auto-resolves new harvests + re-fetches).

## v4.4.2 — Role Routing: inline ops + end-to-end propagation (2026-06-13)
Made the Job Domains / Role Routing Rules registry easy to operate and fixed three
propagation edge cases so a rule truly flows the whole chain (domain → marketing role →
consultant targeting → vetting queue).
- Inline list controls: one-click Pause/Activate toggle and inline priority edit (AJAX,
  no edit-page round trip); collapsible Quick-Add panel with regex "Test pattern";
  slug now auto-generates from the name.
- EDGE A (bug): routing role-map cache was never busted on domain save — a new/edited
  domain wasn't used for job→role auto-assignment for up to 5 min. Now live immediately.
- EDGE B (bug): pausing a domain left its MarketingRole active (still offered/auto-assigned).
  The domain is now the single on/off switch; pause deactivates the role everywhere for
  NEW activity while preserving existing consultant assignments.
- EDGE C: consultant submission form offered paused roles. Now active-only, but keeps any
  role already assigned to the profile so editing never silently drops a selection.
- Downstream flow surfaced on the list (banner + per-rule role state + consultants-targeting).
- +3 regression tests (pause propagation, quick-update endpoint, slug autogen); 8/8 green.
Status: deployed (7a6a593 · CI+image green · health overall=ok · 2026-06-13)

## v4.4.1 — Harvest fully revived: 274 jobs/3min + Role Targeting Studio (2026-06-12)
- FINAL root cause: Celery's global 300s soft time limit killed the daily harvest 5 min in,
  while still on robots-blocked scrape boards — API platforms were never reached. harvest_jobs
  now has a 3h budget and processes API platforms FIRST, scrapers last.
- VERIFIED LIVE: manual greenhouse run → 274 new RawJobs, 23 dup, 0 fail in ~3 minutes
  (vs ~0/day for the prior 4 weeks).
- Role Targeting Studio: phrase impact preview, missed-titles review with one-click add-phrase,
  'Apply phrases to existing jobs' (tracked ops run).
- New sanctioned ops workflow: Run — Harvest Jobs Now (HARVEST confirm gate).
Status: deployed (071eb7a+ · health 200 · verified with live fetch)

## v4.4.0 — Harvest engine revival + freshness alarm (2026-06-12)
- ROOT CAUSE FIX: daily harvest silently stalled since ~May 15 (empty board counted as
  failure → circuit breaker killed each platform after 3 quiet companies). Empties no
  longer trip the breaker; verified live via selective-harvest smoke (batch path fetched
  a new RawJob).
- Harvest writes routed through the advisory-lock dedupe service (cross-URL duplicates blocked).
- 30s default timeout on all harvester HTTP calls; content-gate failures log tracebacks;
  LLM classifier API errors recorded to the incident log.
- System Health: Harvest Freshness alarm (new 24h/7d, newest-fetch age, red NOT FETCHING
  banner) + per-platform scoreboard.
Status: deployed (d54f8a0 · health 200 · 2026-06-12, ahead of the 02:00 UTC harvest)

## v4.3.2 — Ops monitor in-depth detail + zombie guard (2026-06-11)
- Live Ops Monitor rows expand to show trigger source, exact params, full result payload,
  error, and stale reason ("details — what this run did").
- Retry Failed now creates a tracked ops run (was fire-and-forget; failures were invisible).
- Duplicate guard gains a 6h max-runtime ceiling — a hung-but-heartbeating worker can no
  longer block an operation forever (root cause of "backfill jd ×60" skips).
Status: deployed (22e5a07 · health 200 · 2026-06-11)

## v4.3.1 — Country drift propagation (2026-06-10)
- Daily sync now propagates post-sync RawJob country corrections to linked pool Jobs
  (capped 500/run, ops-run logged, 'job_location_sync' flag = UI kill-switch).
Status: deployed (45f25b7 · health 200 · 2026-06-10)

## v4.3.0 — System Health dashboard + VPS hygiene (2026-06-10)
- Ops Center System Health card: disk, DB size, errors, LLM spend, queues, pipeline counts — zero SSH.
- Deploys auto-prune old Docker images/build cache (disk was 81%, now 39%).
- RawJob vs Job metric labels disambiguated (harvest quality vs pool quality, validation at-sync, JD gate vs JD parse).
Status: deployed (2097f37 · health 200 · 2026-06-10)

## v4.2.0 — Multi-provider LLM + truth guardrails (2026-06-09)
- LLMConfig: switch provider from settings — OpenAI / DeepSeek / OpenRouter / Together / custom
  (provider, base_url, validation_model fields; per-call model override; DeepSeek/OpenRouter pricing).
- Deterministic truth guardrails: post-generation, model-agnostic checks vs the candidate's real
  data (employers / JD-company / certs = block; fake metrics / missing headings = review).
  ResumeDraft.review_status + banner; pipeline now persists validation to the draft.
- /explain skill (trace any feature → steps + Mermaid diagrams, read-only).
Status: deployed (354081f · health 200 · 2026-06-09)

## v4.1.0 — Resume editor customization + draft compare (2026-06-09)
Resume engine + editor overhaul.
- Truthful generation: no fabricated skills/metrics; fixed pipeline prompt assembly + Master Prompt v2.
- Editor: WYSIWYG preview that exactly matches DOCX/PDF, US-Letter sheet, zoom, page-count + page-break guides, distraction-free fullscreen.
- Parser: fixed multi-role collapse; auto re-parse of stale drafts (PARSER_VERSION) + manual Re-sync.
- Design customization (Phases 1–3): bold keywords (preview/PDF/DOCX), more fonts, color themes, header alignment, section reorder/show-hide/rename, skills layout, spacing presets, Fit-to-2-pages.
- Templates: save/reuse now captures the full design (layout + alignment).
- NEW: resume draft history + side-by-side compare (versions per job & across jobs).
- CI: use the runner's built-in PostgreSQL (kills Docker pull rate-limit flakes).
- Tooling: /release, /feature, /modify skills + this release log.
Status: deployed (ac1762c · health 200 · 2026-06-09)

## v4.0.0 — baseline (2026-06-09)
Baseline tag prior to the consistent-release flow. Everything before this was deployed ad-hoc via
`/deploy`. From the next release onward, each entry is tagged, changelogged, health-verified, and recorded here.
Status: deployed

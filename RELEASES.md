# Releases

Production release log for GoCareers. Newest first. Created/updated by the `/release` skill
(verify CI green → version + changelog → deploy → health-verify → auto-rollback → record).

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

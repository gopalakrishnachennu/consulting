# Releases

## v4.3.0 — System Health dashboard + VPS hygiene (2026-06-10)
- Ops Center System Health card: disk, DB size, errors, LLM spend, queues, pipeline counts — zero SSH.
- Deploys auto-prune old Docker images/build cache (disk was 81%, now 39%).
- RawJob vs Job metric labels disambiguated (harvest quality vs pool quality, validation at-sync, JD gate vs JD parse).
Status: pending

Production release log for GoCareers. Newest first. Created/updated by the `/release` skill
(verify CI green → version + changelog → deploy → health-verify → auto-rollback → record).

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

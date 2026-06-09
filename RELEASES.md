# Releases

Production release log for GoCareers. Newest first. Created/updated by the `/release` skill
(verify CI green → version + changelog → deploy → health-verify → auto-rollback → record).

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
Status: pending

## v4.0.0 — baseline (2026-06-09)
Baseline tag prior to the consistent-release flow. Everything before this was deployed ad-hoc via
`/deploy`. From the next release onward, each entry is tagged, changelogged, health-verified, and recorded here.
Status: deployed

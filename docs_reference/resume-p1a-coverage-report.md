# Resume Engine V4 — P1a Coverage Report (JD Extraction Engine)

Status: **engine core built + tested (9/9 green, mocked LLM).** Flag-gated, default OFF.
Not deployed. The async-at-promotion trigger, lazy-fallback wiring, and admin debug view
are the next increment (P1a-wiring) — listed as `deferred` below with reasons.

## Files added
- `apps/resumes/pipeline/jd_extractor_schemas.py` — SCHEMA/PROMPT versions, system prompt, `validate_parsed_jd()` (VAL_001–010)
- `apps/resumes/pipeline/jd_extractor.py` — `extract_jd(job)`: normalize → hash → cache → LLM → validate → repair-retry → legacy fallback → store
- `apps/resumes/pipeline/parser_diff.py` — `diff_parsers(legacy, llm)`
- `apps/jobs/migrations/0022_jd_extractor_metadata.py` — Job cache/version fields

## Files changed
- `apps/jobs/models.py` — `parsed_jd_hash / parsed_jd_model / parsed_jd_prompt_version / parsed_jd_schema_version`
- `apps/resumes/tests.py` — 9 tests (validation, engine, cache, retry, fallback, diff)

## Checklist coverage

| ID | Item | Status | Notes |
|----|------|--------|-------|
| P1_001 | Keep rule_parse_jd as fallback/comparison | implemented | `_fallback()` calls `rule_parse_jd`; `parser_diff.py` compares |
| P1_002 | JD text-hash cache | implemented | sha256(normalized) + `_cache_valid()` |
| P1_003 | Source evidence on high-importance items | implemented | prompt requires it; VAL_005 enforces |
| P1_004 | Store raw + normalized term | implemented | schema fields + prompt |
| P1_005 | Richer importance levels | implemented | screen_out…context_only |
| P1_006 | JD noise filter | implemented | `ignored_sections` + VAL_006 |
| P1_007 | Special resume requirement detector | implemented | `special_resume_requirements` |
| P1_008 | Role ambiguity support | implemented | primary + secondary_role_families + blend |
| P1_009 | Seniority + mismatch-warning support | implemented | seniority enum + VAL_003 warning |
| P1_010 | Extraction warnings | implemented | `extraction_quality.extraction_warnings` |
| P1_011 | Alternative requirement groups | implemented | `one_of` groups + VAL_007 |
| P1_012 | Equivalent-experience degree | implemented | `degree_requirement.equivalent_experience_allowed` |
| P1_013 | Stack clustering | implemented | `skill_categories` |
| P1_014 | Responsibility theme depth | implemented | `responsibility_themes[].depth` |
| P1_015 | Resume positioning hint | implemented | `role_classification.resume_positioning_hint` |
| P1_016 | Parser confidence score | implemented | per-item + overall confidence + VAL_009 |
| P1_017 | Structured output validation | implemented | `validate_parsed_jd` VAL_001–010 |
| P1_018 | Retry logic | implemented | one repair-retry with feedback |
| P1_019 | Parser diff | implemented | `parser_diff.diff_parsers` |
| P1_020 | Source-backed resume scope map | deferred | **P1b** (next phase, per agreed sequencing) |
| P1_021 | Runtime evidence levels in scope map | deferred | P1b |
| P1_022 | Prevent strategy forcing all JD terms | deferred | P1b (strategy stage) |
| P1_023 | Exact phrase risk controls | implemented | `exact_phrase_controls` extracted (consumed in P1b) |
| P1_024 | Extractor test cases | implemented | 9 mocked tests, structural assertions |
| P1_025 | Golden expected outputs | implemented | fixture `_valid_parsed_jd()` asserted structurally |
| P1_026 | No RAG in P1 | implemented | none added |
| P1_027 | Clean API-call count + cache | implemented | 1 call/JD, cached; reused across consultants |
| P1_028 | model/prompt/schema version fields | implemented | on Job + parser_metadata |
| P1_029 | Admin debug view | deferred | **P1a-wiring** — small, next increment |
| P1_030 | Wire parsed_jd/scope/strategy into generation | deferred | scope/strategy are P1b; extractor entry point ready |

## Deferred (with reasons) — next increments
- **Async-at-promotion trigger + lazy fallback wiring** (P1a-wiring): a Celery `extract_jd_task` fired when a job goes Live + a periodic sweep + lazy call in the resume pipeline. Deferred so the *engine* can be reviewed in isolation first; the entry point `extract_jd(job)` is ready to call.
- **Admin debug view** (P1_029): read-only page showing parsed JSON, hash, status, evidence, warnings, parser diff. Small; next.
- **Scope map + Strategy** (P1_020–022, P1_030): **P1b**, per the agreed "validate the extractor before building on it" sequencing.

## Tests
9/9 passing (mocked LLM, in-memory sqlite): validation pass/fail (VAL_002/005/006), extract+store, cache-hit-skips-LLM, repair-retry, fallback-when-unavailable, parser-diff.

## Manual verification before enabling on prod
1. Set `LLMConfig` → OpenRouter + `deepseek/deepseek-chat`, enable `harvest_use_central_llm`.
2. In a shell, run `extract_jd(job)` on 10–15 real Live JDs; eyeball role_family, evidence, alt-groups, ignored_sections.
3. Compare via `diff_parsers` against the rule parser to confirm the LLM finds more.
4. Only then wire the async trigger + flip `resume_engine_v4`.

## Remaining risks
- Prompt tuning: the schema is large; very long JDs may need the noise filter to work well — validate on real long JDs.
- Provider JSON fidelity: relying on JSON-mode + our validator + repair-retry (not provider strict schema) — correct, but watch the retry rate on the chosen model.

# LLM Services Map — who calls which API

A reference for *every* place the app talks to an LLM: which service, which model, which
provider, and whether it's logged. Live per-service numbers are on `/core/llm/`
("Usage by Service & Model").

## 1. Resume generation & pipeline ✅ (central, configurable, logged)
- **Client:** `apps/resumes/pipeline/llm_client.py` (`PipelineLLMClient`) + legacy `apps/resumes/engine.py`.
- **Config:** the `/core/llm/` settings (`LLMConfig`) — provider, base_url, **active_model** (generation),
  **validation_model** (cheap, reserved for the LLM audit pass), temperature, max tokens, monthly cap.
- **Provider:** whatever `LLMConfig.provider` / `base_url` points at — OpenAI, **DeepSeek**, OpenRouter, Together, custom.
- **Logged:** yes → `LLMUsageLog` (request_type + model + tokens + cost).
- **request_types you'll see:** `pipeline_v3_generate` (main single-call generation),
  `pipeline_summary` / `pipeline_skills` / `pipeline_experience*` (older multi-call paths),
  `master_resume_generation`, `pipeline_generic`.
- **Guardrails** (`pipeline/guardrails.py`): pure **code**, no LLM call — runs after generation.

## 2. Harvest job classification ⚠️ (separate key, NOT in central config, NOT logged)
- **Code:** `apps/harvest/llm_classifier.py`.
- **Client:** its own `openai.OpenAI(api_key=…)` where the key = `OPENAI_API_KEY` **env var**
  (not the encrypted key in `LLMConfig`).
- **Model:** hardcoded default `gpt-4o-mini`. **Provider:** OpenAI default (ignores `base_url`).
- **Logged:** no — these calls do **not** appear in `LLMUsageLog` or the usage breakdown.
- **Implication:** switching `/core/llm/` to DeepSeek does **not** change harvest classification —
  it still calls OpenAI with the env key. And you can't see its cost on the dashboard.

## Flow
```mermaid
flowchart TD
  subgraph Resume["Resume features"]
    Gen[Resume generation / pipeline] --> Client[PipelineLLMClient]
    Client --> Cfg[(LLMConfig: provider+base_url+model)]
    Client --> Log[(LLMUsageLog ✓)]
    Cfg --> Prov{{OpenAI / DeepSeek / OpenRouter / …}}
    Gen --> Guard[Guardrails — CODE, no LLM]
  end
  subgraph Harvest["Harvest"]
    Cls[Job classification] --> OwnClient[own openai client]
    OwnClient --> Env[(OPENAI_API_KEY env)]
    OwnClient --> OAI{{OpenAI gpt-4o-mini}}
    Cls -. not logged .-> NoLog[(no LLMUsageLog)]
  end
```

## Unify harvest onto the central config — IMPLEMENTED (opt-in)
`llm_classifier.py` can now route through `LLMConfig` (central key/base_url/model) and log to
`LLMUsageLog` — but it's **opt-in via the `harvest_use_central_llm` feature flag** so the
scheduled harvest stays byte-identical until you deliberately enable it.

- **Flag OFF (default):** exactly the current behavior — `OPENAI_API_KEY` env, the caller's model
  (e.g. `HarvestEngineConfig.jd_gate_model`), OpenAI endpoint, no usage logging.
- **Flag ON:** key from `LLMConfig` (env fallback), `base_url` via `effective_base_url()`, model =
  `validation_model` or `active_model`, and each batch logs to `LLMUsageLog`
  (`harvest_classification` / `harvest_jd_gate`) → shows up in the usage breakdown.

**To enable (after setting up the provider on `/core/llm/`):** create/enable FeatureFlag
`harvest_use_central_llm`. Test on a small run first — the JD gate is fault-tolerant (any error →
jobs stay UNCERTAIN, never crashes), but verify classification quality on the new model before
relying on it for the nightly harvest.

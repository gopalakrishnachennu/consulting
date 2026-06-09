# Explain a Feature

Point this at anything in the project — a button, a page, a URL, a model, a workflow — and it
traces the whole thing end-to-end through the real code and explains **how it actually works**:
the trigger, every step, the upstream services it depends on, the downstream services it calls,
and the data it touches — in plain language **plus diagrams**.

This is read-only. It NEVER changes code. Every claim must be grounded in the actual files
(grep + read), not guessed.

## Phase 0 — Identify the target
- Restate what's being explained in one line (e.g. "the Generate Resume button on the workspace").
- If it's ambiguous (which button? which page?), ask ONE quick question or state the assumption.
- Find the concrete entry point in code: the template element (`<button>`, `<form>`, `hx-*`,
  `@click`, `<a href>`) or the URL the user is looking at.

## Phase 1 — Trace the path (follow the request all the way)
Walk the chain and read each hop. Don't skip layers:
1. **Trigger** — the template/JS that starts it (Alpine `@click`, HTMX `hx-post`, form `action`, link).
2. **Route** — the `urls.py` path → the view it maps to.
3. **View / handler** — what the view does: permissions (mixins/test_func), inputs, branching.
4. **Business logic** — services/engine/pipeline functions it calls (e.g. `engine.py`, `pipeline/`,
   `export_utils.py`, parsers).
5. **Data** — models read/written, queries, migrations, cached state (e.g. `sections_json`,
   `template_overrides`).
6. **External / async** — LLM calls (`llm_client`), Celery tasks, Redis, email, geocoding, GHCR,
   anything off-box.
7. **Response** — what comes back: rendered template, JSON, redirect, file download, HTMX swap —
   and how the UI updates.

## Phase 2 — Upstream & downstream
- **Upstream (what it depends on BEFORE it can work):** required data/state, feature flags,
  config (LLMConfig, MasterPrompt), prior steps, auth/role, env (which DB/settings).
- **Downstream (what it triggers AFTER):** records created/updated, Celery jobs queued, other
  features that consume its output, side effects, notifications.

## Phase 3 — Explain in simple steps
Write a numbered, plain-English walkthrough a non-expert could follow. No jargon without a gloss.
Example shape: "1. You click **Generate**. 2. The browser POSTs to `/resumes/generate/run/`.
3. Django checks you're allowed… 4. It builds the prompt from the candidate + job… 5. Calls the
LLM once… 6. Saves a ResumeDraft… 7. Redirects you to the draft."

## Phase 4 — Diagrams (always include these)
Produce BOTH, using Mermaid (renders on GitHub / most viewers); add a short ASCII sketch if helpful.

1. **Workflow flowchart** — the decision/step flow:
   ```mermaid
   flowchart TD
     A[User clicks Button] --> B{Allowed?}
     B -- no --> X[403 / redirect]
     B -- yes --> C[View builds inputs]
     C --> D[(DB read)]
     C --> E[[External service]]
     E --> F[Save result]
     F --> G[Response / UI update]
   ```
2. **Sequence diagram** — who talks to whom across the request lifecycle:
   ```mermaid
   sequenceDiagram
     participant Br as Browser
     participant Dj as Django view
     participant Svc as Service/Pipeline
     participant DB as Postgres
     participant Ext as LLM/Celery
     Br->>Dj: POST /…
     Dj->>Svc: call()
     Svc->>Ext: request
     Ext-->>Svc: result
     Svc->>DB: save
     Dj-->>Br: response
   ```

## Phase 5 — File & service map
A table so they know exactly where each part lives:

| Layer | File / location | Role |
|------|------------------|------|
| Trigger (UI) | `templates/…html` | the button/form |
| Route | `apps/<app>/urls.py` | URL → view |
| View | `apps/<app>/views.py::ViewName` | handles the request |
| Logic | `apps/…` | the actual work |
| Data | `apps/<app>/models.py::Model` | what's stored |
| External | … | LLM / Celery / etc. |

Also list **failure modes** (what happens if the LLM is down, input missing, permission denied)
and **auth** (which roles can do it).

## Rules
- READ the real code for every hop — quote the file/function. No hand-waving or invented behavior.
- Cover all sides: frontend, backend, data, external/async, auth, errors.
- Keep the prose simple; let the diagrams carry the structure.
- Read-only: never edit anything during an explanation.
- If a hop is genuinely unclear from the code, say so rather than guessing.

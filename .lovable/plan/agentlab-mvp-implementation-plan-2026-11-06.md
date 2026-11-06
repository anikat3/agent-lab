# AgentLab MVP — Implementation Plan

## Goal
A dark-mode developer tool to manage test suites for conversational AI agents, launch evaluations on an external Python (LangGraph + FastAPI) engine, and review results. Lovable Cloud stores suites, scenarios, runs and results. The frontend never fakes execution. LangGraph is not implemented here. Priority: working CRUD and engine integration over visual polish.

## Screens
1. **Dashboard** (`/`): KPI cards (total runs, avg pass rate, avg latency, open failures), pass-rate trend chart, failure-category bar chart, recent runs table. Real data only by default; a "Show demo metrics" toggle switches to demo data only (the two are never mixed).
2. **Test Suites** (`/suites`, `/suites/$id`): list and create suites; inside a suite, add, edit, delete and tag scenarios (input conversation, expected response, evaluation type and config, category).
3. **Agents** (`/agents`): create, edit and delete agent configurations (name, system prompt, model).
4. **Run Evaluation** (`/runs/new`): pick a suite, an agent configuration, a timeout and max retries, then launch. If the engine isn't configured or is unreachable, a clear error state is shown and the run is marked failed with the reason.
5. **Results** (`/runs`, `/runs/$id`): run list with status; run detail with per-test outcome, quality score, latency, failure explanation, expected vs. actual, plus "Refresh status" and "Retry run" actions. Retrying creates a new run linked to the original, and the detail page links between attempts.
6. **Sign in** (`/auth`): email + password.

Synthetic demo data is seeded and every demo row carries a visible **"Demo data"** badge.

## Authentication (single workspace)
- Email/password sign-in via Lovable Cloud. Every signed-in user shares one workspace.
- Signed-out visitors see demo data only, read-only, with a "Sign in to see your workspace" prompt.
- Real suites, scenarios, agents, runs and results require sign-in to read or write. This is enforced in two places:
  - Database: RLS lets `anon` select only rows where `is_demo = true`. `authenticated` can read all rows and write only non-demo rows.
  - Server functions: real-data functions use auth middleware. Public demo functions read through the publishable client and are always filtered to `is_demo = true`.
- **Launch rate limit**: the backend has no built-in rate limiting, so this is a simple custom check. The launch function counts the workspace's runs created in the last 60 seconds and rejects a new launch with `RATE_LIMITED` once 5 or more exist (also capped at 10 queued or running at once). It is approximate under heavy concurrency, which is acceptable for the MVP.

## Evaluation types (implemented by the Python engine)
Each scenario has `eval_type` and `eval_config`:
- `keyword`: `{ required: string[], forbidden?: string[], case_sensitive?: bool }`
- `exact`: `{ expected: string, normalize_whitespace?: bool, case_sensitive?: bool }`
- `json_schema`: `{ schema: JSONSchema }` (the agent response must parse as JSON and validate against it)
- `llm_judge`: `{ rubric: string, pass_threshold: number (0-1), judge_model?: string }`

One set of zod schemas (`src/lib/api/eval-config.ts`) is used by the editor form (client) and by the create/update server functions (server), so invalid configs are rejected on both sides. `json_schema` additionally checks that the schema is valid JSON Schema and that `pass_threshold` is between 0 and 1.

## Data model (Lovable Cloud)
```text
test_suites      id, name, description, created_by, created_at, updated_at, is_demo
test_scenarios   id, suite_id -> test_suites (cascade), name, category,
                 input_messages jsonb [{role, content}], expected_response text,
                 eval_type enum(keyword|exact|json_schema|llm_judge),
                 eval_config jsonb, tags text[], created_at, updated_at, is_demo
agent_configs    id, name, system_prompt text, model text, created_by,
                 created_at, updated_at, is_demo
eval_runs        id, suite_id -> test_suites, agent_config_id -> agent_configs,
                 agent_snapshot jsonb {name, system_prompt, model},
                 retry_of_run_id -> eval_runs (nullable),
                 status enum(pending|queued|running|completed|failed|cancelled),
                 external_run_id, timeout_seconds, max_retries, attempt_count,
                 error_code, error_message, created_by,
                 created_at, started_at, completed_at,
                 total, passed, pass_rate, avg_latency_ms, is_demo
eval_results     id, run_id -> eval_runs (cascade), scenario_id -> test_scenarios,
                 passed bool, quality_score numeric, latency_ms int,
                 actual_response text, failure_category text,
                 failure_explanation text, evaluator_details jsonb, is_demo,
                 UNIQUE (run_id, scenario_id)
```
Results are saved with `upsert ... on conflict (run_id, scenario_id)`, so repeated polls never duplicate rows. Each run stores a snapshot of the agent config so history stays accurate if the agent is later edited.
Dashboard aggregates filter `is_demo = false` by default and `is_demo = true` when the demo toggle is on.

## Engine integration
Server functions proxy every call to the engine. `EVAL_ENGINE_URL` and `EVAL_ENGINE_API_KEY` are kept as server-side secrets and sent as `Authorization: Bearer <key>`.
- **Timeouts**: each HTTP call from the app aborts after 15s. Execution timeouts are enforced by the Python engine using `timeout_seconds` (default 300) and reported back as a failed status with code `RUN_TIMEOUT`.
- **Retries**: on network errors, 5xx or 429, a launch is retried up to `max_retries` (default 2, max 5) with exponential backoff (1s, 2s, 4s). 4xx errors are not retried. Users can manually retry failed runs.
- **Error states**: the UI maps each typed error code to a message: `ENGINE_NOT_CONFIGURED`, `ENGINE_UNREACHABLE`, `ENGINE_TIMEOUT`, `ENGINE_AUTH_FAILED`, `ENGINE_BAD_REQUEST`, `ENGINE_ERROR`, `RUN_TIMEOUT`.
- A sidebar health indicator calls `/health`.
- The run detail page polls every 3s while the run is queued or running. Each poll saves the results.

## API contract (docs/api-contract.md and src/lib/api/types.ts)
All bodies are JSON. Auth: `Authorization: Bearer <EVAL_ENGINE_API_KEY>`.

`GET /health` → 200 `{ status: "ok", version: string }`

`POST /runs` (Idempotency-Key: <run_id>)
```text
request:  { run_id: uuid, retry_of_run_id: uuid|null, timeout_seconds: int,
            agent: { id, name, system_prompt, model },
            suite: { id, name },
            scenarios: [{ id, name, input_messages: [{role, content}],
                          expected_response: string|null,
                          eval_type, eval_config }] }
response 202: { external_run_id: string, status: "queued" }
```

`GET /runs/{external_run_id}`
```text
response 200: { external_run_id, status, started_at?, completed_at?,
                progress: { completed: int, total: int },
                results: [{ scenario_id, passed, quality_score (0-1),
                            latency_ms, actual_response,
                            failure_category?, failure_explanation?,
                            evaluator_details? }],
                error?: { code, message } }
```

`POST /runs/{external_run_id}/cancel` → 202 `{ status: "cancelled" }`

Error response (any non-2xx): `{ error: { code: string, message: string, details?: object } }`. Codes: 400 `INVALID_REQUEST`, 401 `UNAUTHORIZED`, 404 `RUN_NOT_FOUND`, 409 `DUPLICATE_RUN`, 422 `INVALID_EVAL_CONFIG`, 429 `RATE_LIMITED`, 500 `INTERNAL_ERROR`, 503 `UNAVAILABLE`.

Run status transitions:
```text
pending (row created in app)
  -> queued     (engine accepted, 202)
  -> failed     (launch failed after retries)
queued  -> running | failed | cancelled
running -> completed | failed | cancelled
completed, failed, cancelled: terminal
(manual retry of a failed run creates a NEW run in pending with retry_of_run_id set)
```

## Technical details
- `src/lib/api/`: `types.ts` (zod schemas and DTOs for the contract), `engine.server.ts` (fetch with AbortController timeout, retry/backoff, typed `EngineError`), `eval-config.ts` (shared validation), `suites.functions.ts`, `scenarios.functions.ts`, `agents.functions.ts`, `runs.functions.ts`, `dashboard.functions.ts`.
- TanStack Query for every read and mutation, with skeleton loaders, error cards with retry, and success toasts (sonner).
- Unit tests cover the retry/backoff decisions, status transitions, eval-config validation, the launch rate-limit rule and the demo-exclusion filter.
- Charts use Recharts. Sidebar layout lives in `__root.tsx`. Dark theme with semantic tokens and a monospace font for IDs and latency.
- Migration includes the schema, enums, grants, RLS and literal INSERTs for the demo rows (1 suite, ~8 scenarios covering all 4 eval types, 1 demo agent config, 3 completed demo runs with results).

## Out of scope (MVP)
Multi-tenant workspaces, roles, run comparison view, streaming logs, cancel button UI (contract only).

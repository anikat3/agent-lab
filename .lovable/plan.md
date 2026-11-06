# AgentLab MVP — Implementation Plan

## Goal
A dark-mode developer tool to manage test suites for conversational AI agents, launch evaluations on an external Python (LangGraph + FastAPI) engine, and review results. Lovable Cloud stores suites, scenarios, runs and results. The frontend never fakes execution.

## Screens
1. **Dashboard** (`/`): KPI cards (total runs, avg pass rate, avg latency, open failures), pass-rate trend chart, failure-category bar chart, recent runs table.
2. **Test Suites** (`/suites`, `/suites/$id`): list and create suites; inside a suite, add, edit, delete and tag scenarios (input conversation, expected response, evaluation criteria, category).
3. **Run Evaluation** (`/runs/new`): pick a suite and agent config (name/endpoint label), then launch. If the engine isn't configured, a clear "Evaluation engine not connected" state is shown and no run is created.
4. **Results** (`/runs`, `/runs/$id`): run list with status; run detail with per-test outcome (pass/fail), quality score, latency, failure explanation, and expected vs. actual response.

Synthetic demo data is seeded and every demo row carries a visible **"Demo data"** badge, with a banner on the Dashboard and Results pages.

## Data model (Lovable Cloud)
```text
test_suites      id, name, description, created_at, is_demo
test_scenarios   id, suite_id -> test_suites, name, category,
                 input_messages (jsonb), expected_response, criteria (jsonb),
                 tags text[], created_at, is_demo
eval_runs        id, suite_id -> test_suites, agent_name, status
                 (queued|running|completed|failed), external_run_id,
                 started_at, completed_at, pass_rate, avg_latency_ms,
                 error_message, is_demo
eval_results     id, run_id -> eval_runs, scenario_id -> test_scenarios,
                 passed bool, quality_score numeric, latency_ms int,
                 actual_response, failure_category, failure_explanation, is_demo
```
Single-workspace MVP with no login. Tables are readable and writable through server functions only, and grants and RLS restrict direct access.

## Backend integration (REST)
Server functions act as a thin proxy, so the engine URL and API key stay server-side as secrets (`EVAL_ENGINE_URL`, `EVAL_ENGINE_API_KEY`).
- `POST {engine}/runs` with `{ run_id, suite, scenarios }` returns `{ external_run_id }`
- `GET {engine}/runs/{id}` returns status plus results; the app polls this and saves results to Cloud
- `GET {engine}/health` drives the connection indicator in the sidebar

If the secrets are missing, launching returns a typed "not configured" error, which the UI displays as is. Nothing is simulated.

## Technical details
- `src/lib/api/`: `types.ts` (shared DTOs mirroring the FastAPI contract), `engine.server.ts` (fetch wrapper with timeout and typed errors), `*.functions.ts` (createServerFn for suites, scenarios, runs, results).
- TanStack Query for every read and mutation, with skeleton loaders, error cards with retry, and success toasts (sonner).
- Run detail polls every 3s while the status is queued or running.
- Charts use Recharts. Sidebar layout lives in `__root.tsx`.
- Design: near-black graphite background, a single electric-lime accent, Geist + JetBrains Mono for IDs and latency, dense tables, and status pills that use semantic tokens.
- Migration includes the schema, grants, RLS and literal INSERTs for the demo rows (1 suite, ~8 scenarios, 3 completed demo runs with results).
- `docs/api-contract.md` documents the endpoints for the Python team.

## Out of scope (MVP)
Auth/multi-tenant, run comparison view (planned as a follow-up), streaming logs.

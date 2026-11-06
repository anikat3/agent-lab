# AgentLab ⇄ Evaluation Engine REST contract (v1)

The AgentLab app calls your Python (FastAPI + LangGraph) engine **server-side only**.
Configure two project secrets in AgentLab:

- `EVAL_ENGINE_URL` – base URL, e.g. `https://engine.example.com`
- `EVAL_ENGINE_API_KEY` – sent as `Authorization: Bearer <key>` on every request

All bodies are JSON (`content-type: application/json`). Each HTTP call from AgentLab aborts after **15 s** (health: 5 s).

## GET /health

`200 { "status": "ok", "version": "1.2.0" }`

## POST /runs — launch a run

Headers: `Idempotency-Key: <run_id>`. The engine must treat a repeated key as the same run (return the same `external_run_id`), because AgentLab retries launches.

```json
{
  "run_id": "uuid",
  "retry_of_run_id": "uuid | null",
  "timeout_seconds": 300,
  "agent": { "id": "uuid", "name": "Support bot", "system_prompt": "...", "model": "gpt-4o-mini" },
  "suite": { "id": "uuid", "name": "Customer support" },
  "scenarios": [
    {
      "id": "uuid",
      "name": "Refund policy",
      "input_messages": [{ "role": "user", "content": "What is your refund policy?" }],
      "expected_response": "Refunds within 30 days.",
      "eval_type": "keyword",
      "eval_config": { "required": ["30 days"], "forbidden": [], "case_sensitive": false }
    }
  ]
}
```

`202 { "external_run_id": "string", "status": "queued" }`

The engine enforces `timeout_seconds` for the whole run and reports a timeout as `status: "failed"` with `error.code = "RUN_TIMEOUT"`.

### eval_type / eval_config

| eval_type | eval_config |
|---|---|
| `keyword` | `{ required: string[] (≥1), forbidden?: string[], case_sensitive?: bool }` |
| `exact` | `{ expected: string, normalize_whitespace?: bool, case_sensitive?: bool }` |
| `json_schema` | `{ schema: JSONSchema }` — response must parse as JSON and validate |
| `llm_judge` | `{ rubric: string, pass_threshold: number 0–1, judge_model?: string }` |

AgentLab validates these on both client and server before sending; the engine should still return `422 INVALID_EVAL_CONFIG` for anything it cannot run.

## GET /runs/{external_run_id} — status + results

AgentLab polls every 3 s while a run is `queued` or `running`. Return **all results so far** each time; AgentLab upserts them on `(run_id, scenario_id)`, so repeats are safe.

```json
{
  "external_run_id": "string",
  "status": "queued | running | completed | failed | cancelled",
  "started_at": "ISO-8601 | null",
  "completed_at": "ISO-8601 | null",
  "progress": { "completed": 3, "total": 8 },
  "results": [
    {
      "scenario_id": "uuid",
      "passed": false,
      "quality_score": 0.42,
      "latency_ms": 1234,
      "actual_response": "string",
      "failure_category": "schema_violation",
      "failure_explanation": "Missing required property 'status'",
      "evaluator_details": { "any": "json" }
    }
  ],
  "error": { "code": "RUN_TIMEOUT", "message": "..." }
}
```

`quality_score` is 0–1. `error` is null unless `status = failed`.

## POST /runs/{external_run_id}/cancel

`202 { "status": "cancelled" }` (in the contract; AgentLab has no cancel button yet)

## Errors (any non-2xx)

```json
{ "error": { "code": "INVALID_REQUEST", "message": "human readable", "details": {} } }
```

| HTTP | code | AgentLab retries launch? |
|---|---|---|
| 400 | `INVALID_REQUEST` | no |
| 401 | `UNAUTHORIZED` | no (shown as "Engine rejected the API key") |
| 404 | `RUN_NOT_FOUND` | no |
| 409 | `DUPLICATE_RUN` | no |
| 422 | `INVALID_EVAL_CONFIG` | no |
| 429 | `RATE_LIMITED` | yes |
| 500 | `INTERNAL_ERROR` | yes |
| 503 | `UNAVAILABLE` | yes |

Network failures and timeouts are also retried, up to the run's `max_retries` (0–5) with 1 s, 2 s, 4 s… backoff.

## Run status transitions

```text
pending  (row created in AgentLab)
  -> queued     (engine returned 202)
  -> failed     (launch failed after retries)
queued   -> running | completed | failed | cancelled
running  -> completed | failed | cancelled
completed, failed, cancelled: terminal
```

Retrying a failed run in AgentLab creates a **new** run (new `run_id`) with `retry_of_run_id` set to the original.

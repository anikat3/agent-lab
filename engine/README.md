# AgentLab Evaluation Engine

The Python service the AgentLab app calls to run evaluation suites. It implements the REST
contract in [`../docs/api-contract.md`](../docs/api-contract.md).

**Status: Milestone 2C.** Runs are executed for real. A background worker claims each
run, sends every scenario through a LangGraph workflow that calls the agent's OpenAI
model via LangChain, evaluates the reply with a deterministic evaluator, and stores the
result. Supported eval types are `keyword`, `exact` and `json_schema`, and one run may
mix them. `llm_judge` (Milestone 2D) is rejected at launch.

## Run locally

Requires Python 3.11+.

```sh
cd engine
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env        # set EVAL_ENGINE_API_KEY, EVAL_EXECUTOR=langgraph, OPENAI_API_KEY
uvicorn app.main:create_app --factory --reload --port 8000
```

Interactive API docs: http://localhost:8000/docs. `GET /health` reports whether execution
is available (`execution.available`, and `execution.reason` if not).

## Testing (offline, no API charges)

`pytest` runs the whole suite offline. Models are either mocked or served by a local
OpenAI-compatible server (`tests/fake_openai_server.py`), which exercises the real
ChatOpenAI → OpenAI SDK → HTTP path on 127.0.0.1. Three guards make sure no test can
spend money by accident:

1. **Credentials are scrubbed.** `OPENAI_*`, `LANGSMITH_*` and every engine setting are
   removed from the environment for each test, so a key exported in your shell is never
   used.
2. **`engine/.env` is ignored by test settings.**
3. **Outbound network is blocked.** Any connection to a non-loopback address raises
   `NetworkAccessBlocked`.

Live tests (`tests/test_live_openai.py`) are deselected by default. They only run with
**both** `-m live` and `RUN_LIVE_TESTS=1`, and they're the only tests the guards skip.

**Manual end-to-end runs without charges:**

```sh
python -m tests.fake_openai_server 8799        # terminal 1: replies are prefixed [fake-openai]
OPENAI_BASE_URL=http://127.0.0.1:8799/v1 OPENAI_API_KEY=sk-local-test-key-not-a-real-key \
  EVAL_EXECUTOR=langgraph uvicorn app.main:create_app --factory --port 8000   # terminal 2
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EVAL_ENGINE_API_KEY` | required | Bearer token the app must send. The engine will not start without it. |
| `DATABASE_URL` | `sqlite:///./agentlab_engine.db` | SQLAlchemy URL for run storage. |
| `SQLITE_BUSY_TIMEOUT_MS` | `5000` | How long a write waits for SQLite's write lock before failing (API returns 503). |
| `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | How often an idle worker looks for queued runs. |
| `WORKER_LEASE_SECONDS` | `30` | A running run whose owner hasn't renewed its lease for this long is treated as orphaned. |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `10` | On shutdown, how long the in-flight run may continue before it is interrupted. |
| `EVAL_EXECUTOR` | `none` | `langgraph` enables execution. With `none` (or a missing key), no worker starts and `POST /runs` returns 503. |
| `OPENAI_API_KEY` | — | OpenAI key for the agents' models. Server-side only; never logged or returned. |
| `OPENAI_BASE_URL` | real OpenAI API | Point at an OpenAI-compatible endpoint instead (the local fake server, a proxy). |
| `MODEL_TIMEOUT_SECONDS` | `60` | Budget for one model call, retries included (`MODEL_TIMEOUT` if exceeded). |
| `MODEL_MAX_RETRIES` | `2` | Client retries for rate limits, 5xx and connection errors. |

Variables are read from the environment, then from `engine/.env` (git-ignored).

## The evaluation workflow (LangGraph)

```text
START -> call_agent -> evaluate -> END          (one graph invocation per scenario)

call_agent  [system_prompt] + scenario messages (order and roles kept) -> ChatOpenAI.ainvoke
            -> state.response, state.latency_ms
evaluate    EVALUATORS[eval_type](response, eval_config) -> state.evaluation
```

- **Evaluators** (`app/evaluators.py`): pure functions, no LLM, binary score (1.0 / 0.0).
  `EVALUATORS[eval_type]` picks one per scenario.

  | eval_type | Passes when | Failure categories |
  |---|---|---|
  | `keyword` | Every `required` keyword appears and no `forbidden` keyword does (substring match, case-insensitive unless `case_sensitive`) | `keyword_missing`, `forbidden_content` (wins when both apply) |
  | `exact` | The response equals `expected` (Unicode NFC always; case-insensitive unless `case_sensitive`; whitespace significant unless `normalize_whitespace`, which collapses runs and trims; punctuation always counts) | `exact_mismatch` |
  | `json_schema` | The response is strict JSON (no code fences, NaN or duplicate keys) that validates against `schema`, using the draft named by `$schema` (latest by default) | `invalid_json`, `schema_violation` |

  Failure details say *where* a response failed (first differing position, JSON paths
  and constraint names) without copying the response text, which is stored once as
  `actual_response`.
- **Agent mistakes vs infrastructure failures.** A wrong answer is a stored result
  (`passed: false`) and the run continues. A model or provider failure fails the run
  with a structured code, keeping earlier results; nothing is ever recorded as a fake
  evaluation. The codes:

| `error.code` | Cause |
|---|---|
| `MODEL_AUTH_FAILED` | Key rejected (401) or not allowed to use the model (403) |
| `MODEL_NOT_FOUND` | Unknown model, or the key can't access it (404) |
| `MODEL_RATE_LIMITED` | Rate limit or exhausted quota, after retries |
| `MODEL_TIMEOUT` | One model call exceeded `MODEL_TIMEOUT_SECONDS` |
| `MODEL_BAD_REQUEST` | Request rejected, e.g. conversation too long |
| `MODEL_PROVIDER_ERROR` | Provider unreachable or 5xx, after retries |

Error messages are written by the engine, not copied from the provider, so neither keys
nor prompt content leak into responses.

- **Launch-time checks** (nothing is stored, no LLM call is spent):
  - an unsupported eval type → `422 INVALID_EVAL_CONFIG`;
  - a non-OpenAI model → `400 INVALID_REQUEST`. Models are accepted as bare OpenAI names
    (`gpt-4o-mini`, `o3-mini`, …) or as `openai:<model>`.
- **Adding a provider** takes a resolve rule and a branch in `ChatModels._create`
  (`app/llm.py`). Error mapping uses langchain-core's provider-neutral exceptions, so
  it needs no change.

## How execution works

```text
POST /runs ──► runs + run_scenarios rows (status=queued)          HTTP layer: store and read only
                         │
Worker (asyncio task in the same process, one run at a time)
  claim:    UPDATE runs SET status='running', worker_id, started_at, lease
            WHERE id=? AND status='queued'          ← exactly one claimer can match
  execute:  for each scenario, in order:
              outcome = await executor(run, scenario)        ← injected; LangGraph in 2B
              record_result: UPDATE runs SET lease=… WHERE id=? AND status='running' AND worker_id=me
                             then insert or replace the scenario's result (same transaction)
  heartbeat: renew the lease every lease/3 seconds
  finish:   UPDATE runs SET status=completed|failed, completed_at, error
            WHERE id=? AND status='running' AND worker_id=me
GET /runs/{id} ◄── reads runs + scenario_results
```

Every state change is a **conditional UPDATE**: it only applies if the run is still in the
expected state and owned by the expected worker. This gives three guarantees:

- Claims are exclusive.
- Terminal states (`completed`, `failed`, `cancelled`) can never be overwritten.
- A worker that lost its run can't write anything. It might have lost the run through a
  timeout, recovery, or in future a cancellation.

### Lifecycle and error codes

`queued → running → completed | failed` (and `cancelled`, reserved for the future cancel
endpoint). `started_at` is set on claim; `completed_at` on any terminal transition.

| `error.code` | Meaning |
|---|---|
| `RUN_TIMEOUT` | The run exceeded `timeout_seconds`, measured from `started_at`. |
| `WORKER_INTERRUPTED` | The worker stopped mid-run (crash, restart, deploy or shutdown). |
| `EXECUTION_ERROR` | The executor raised an unexpected exception. |
| `INVALID_EXECUTOR_OUTPUT` | The executor returned something other than a `ScenarioOutcome`. |
| *(executor-defined)* | The executor raised `ExecutionError(code, message)`. |

Results stored before any failure are always kept. A scenario the agent got wrong is a
result with `passed: false`, not a run failure.

### Crash recovery

The worker that owns a running run keeps renewing its lease. If the process dies, the
lease stops being renewed. Any engine process will then mark the run `failed` with
`WORKER_INTERRUPTED`: at startup, or in the idle worker's periodic sweep.

- **Nothing is re-executed automatically.** Partially executed LLM calls are never
  repeated behind your back, which avoids double cost and side effects. Retrying is
  the app's existing manual retry, which creates a new linked run.
- **Partial results are kept**, so no evaluation history is lost.
- **A live run is never touched.** Recovery only applies to *expired* leases, so a second
  process starting up does not interrupt a run another process is executing.

On graceful shutdown, the in-flight run gets `WORKER_SHUTDOWN_GRACE_SECONDS` to finish.
After that it is marked `WORKER_INTERRUPTED` straight away rather than waiting for its
lease to expire.

### Timeouts and blocking code

The deadline is `started_at + timeout_seconds`, enforced with `asyncio.timeout` around the
scenario loop. When it expires, the run is failed with `RUN_TIMEOUT` and anything that
finishes later is discarded by the fenced writes.

`asyncio` can only cancel code that `await`s. A blocking call running in a thread keeps
going until it returns, even though its result is ignored. It also holds a thread-pool
slot, and keeps spending tokens if it is an LLM call. Executors must therefore:

- be `async`;
- never block the event loop (the HTTP API shares it);
- give any thread-offloaded or network call its own timeout.

See [`app/execution.py`](app/execution.py).

### Concurrency assumptions (SQLite)

- **WAL mode.** Readers never block the writer and vice versa, so API polls keep working
  while the worker writes.
- **One writer at a time.** Writes wait up to `SQLITE_BUSY_TIMEOUT_MS`. Each worker
  transaction is short and starts with its UPDATE, so it takes the lock first. If the
  lock still can't be had, the API returns `503 UNAVAILABLE`, which the contract tells
  the app to retry.
- **Single host only.** Several engine processes on one machine (e.g. `uvicorn
  --workers N`, each with its own worker) are safe: claims are atomic and leases tell
  live runs from orphaned ones. SQLite must not live on a network filesystem, and leases
  compare timestamps, so all processes must share one clock.
- **Multiple machines or replicas** need Postgres (change `DATABASE_URL`). The
  conditional-UPDATE design carries over unchanged.

### Schema migrations

`app/migrations.py` applies numbered migrations in order. Each one runs in a single
transaction together with its row in `schema_migrations`, so a failure leaves the schema
untouched. Milestone 1 databases are upgraded in place. Any change to `app/models.py`
needs a new migration (a test checks they match). Move to Alembic for Postgres.

## Layout

```text
app/
  main.py         app factory: settings, migrations, startup recovery, worker, routes
  config.py       environment configuration
  auth.py         bearer-token check
  errors.py       contract error envelope + exception handlers
  schemas.py      Pydantic request/response models (mirror src/lib/api/types.ts)
  eval_config.py  per-eval_type config validation (mirrors src/lib/api/eval-config.ts)
  routes.py       HTTP endpoints
  repository.py   API-side database operations, idempotent run creation
  models.py       SQLAlchemy tables
  db.py           engine/session setup, SQLite pragmas
  migrations.py   versioned schema migrations
  lifecycle.py    statuses, allowed transitions, error codes, deadlines
  execution.py    executor interface: RunSpec, ScenarioOutcome, ExecutionError
  graph.py        LangGraph workflow + LangGraphExecutor + build_executor(settings)
  llm.py          model resolution, message construction, ChatOpenAI registry, error mapping
  evaluators.py   deterministic evaluators (keyword, exact, json_schema) + registry
  run_store.py    worker-side database operations (claim, lease, results, finish, recover)
  worker.py       background worker
tests/            pytest suite (mocked models; test_live_openai.py is opt-in)
```

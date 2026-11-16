# AgentLab Evaluation Engine

The Python service the AgentLab app calls to run evaluation suites. It implements the REST
contract in [`../docs/api-contract.md`](../docs/api-contract.md).

**Status: Milestone 1 (foundation).** Runs are validated, authenticated, stored in SQLite
and reported back, but nothing executes them yet, so every run stays `queued`. Execution
with LangChain/LangGraph is the next milestone.

## Run locally

Requires Python 3.11+.

```sh
cd engine
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env        # then set EVAL_ENGINE_API_KEY to a long random string
uvicorn app.main:create_app --factory --reload --port 8000
```

Interactive API docs: http://localhost:8000/docs

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `EVAL_ENGINE_API_KEY` | yes | — | Bearer token the app must send. The engine will not start without it. |
| `DATABASE_URL` | no | `sqlite:///./agentlab_engine.db` | SQLAlchemy URL for run storage. |

Variables are read from the environment, then from `engine/.env` (git-ignored).

## Tests

```sh
pytest
```

## Layout

```text
app/
  main.py         app factory: settings, DB, error handlers, routes
  config.py       environment configuration
  auth.py         bearer-token check
  errors.py       contract error envelope + exception handlers
  schemas.py      Pydantic request/response models (mirror src/lib/api/types.ts)
  eval_config.py  per-eval_type config validation (mirrors src/lib/api/eval-config.ts)
  routes.py       HTTP endpoints
  repository.py   database operations, idempotent run creation
  models.py       SQLAlchemy tables
  db.py           engine/session setup
tests/            pytest suite
```

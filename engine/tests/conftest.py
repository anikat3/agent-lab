import copy
import inspect
import os
import socket
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from app.run_store import RunStore

from .fake_openai_server import TEST_KEY, FakeOpenAIServer

API_KEY = "test-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


# --- offline guard: no test may reach a paid API by accident ---------------------------------
#
# Every test except those marked `live` (which also need RUN_LIVE_TESTS=1) runs with:
#   1. provider credentials and engine settings removed from the environment, so an
#      exported OPENAI_API_KEY / EVAL_EXECUTOR in your shell can't switch on real execution;
#   2. engine/.env ignored (see make_settings), so a real key saved there is never read;
#   3. outbound network blocked: any connection to a non-loopback address raises.

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_SETTINGS_ENV_VARS = {name.upper() for name in Settings.model_fields}


class NetworkAccessBlocked(RuntimeError):
    pass


def _guard_address(sock: socket.socket, address: Any) -> None:
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        host = address[0]
        if host not in _LOOPBACK_HOSTS:
            sock.close()  # the caller's connect fails; don't leak the descriptor
            raise NetworkAccessBlocked(
                f"Test tried to connect to {host}:{address[1]}. Tests must stay offline; use the "
                "local fake OpenAI server, or mark the test @pytest.mark.live (opt-in)."
            )


@pytest.fixture(autouse=True)
def offline_guard(request, monkeypatch):  # type: ignore[no-untyped-def]
    if request.node.get_closest_marker("live"):
        yield
        return
    for name in list(os.environ):
        if name.startswith(("OPENAI_", "LANGSMITH_", "LANGCHAIN_")) or name in _SETTINGS_ENV_VARS:
            monkeypatch.delenv(name)

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self, address, *args):  # type: ignore[no-untyped-def]
        _guard_address(self, address)
        return real_connect(self, address, *args)

    def connect_ex(self, address, *args):  # type: ignore[no-untyped-def]
        _guard_address(self, address)
        return real_connect_ex(self, address, *args)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield


@pytest.fixture
def fake_openai(monkeypatch):  # type: ignore[no-untyped-def]
    """A running local OpenAI-compatible server; OPENAI_BASE_URL points the engine at it.

    Set `fake_openai.respond = lambda req: ...` to script replies or Reply(status, code) errors.
    """
    server = FakeOpenAIServer().start()
    monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
    yield server
    server.stop()


def langgraph_settings(tmp_path, **overrides: Any) -> Settings:  # type: ignore[no-untyped-def]
    """Settings for the real LangGraph executor talking to the fake_openai server."""
    values: dict[str, Any] = {"eval_executor": "langgraph", "openai_api_key": TEST_KEY, "model_max_retries": 0}
    values.update(overrides)
    return make_settings(tmp_path, **values)


async def not_executed(run, scenario):  # type: ignore[no-untyped-def]
    """Executor for API-only tests: launches are accepted, but nothing may execute them."""
    raise AssertionError("no worker runs in API-only tests")


def make_api_app(tmp_path, **settings_overrides: Any) -> FastAPI:  # type: ignore[no-untyped-def]
    """An app that accepts launches (an executor is configured) but runs no worker, so
    runs stay queued unless a test drives a Worker itself."""
    return create_app(make_settings(tmp_path, **settings_overrides), executor=not_executed, start_worker=False)


@pytest.fixture
def app(tmp_path) -> FastAPI:  # type: ignore[no-untyped-def]
    return make_api_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:  # the context manager runs the lifespan (creates tables)
        yield c


@pytest.fixture
def db(app: FastAPI, client: TestClient) -> Iterator[Session]:
    session = app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


_BASE_PAYLOAD: dict[str, Any] = {
    "retry_of_run_id": None,
    "timeout_seconds": 300,
    "agent": {
        "id": "8f0f1b8e-6a43-4c8e-9a39-1d2b1a7e0c11",
        "name": "Support bot",
        "system_prompt": "You are a helpful support agent.",
        "model": "gpt-4o-mini",
    },
    "suite": {"id": "0b9d7c55-3f1e-4b8a-8f0e-2b6f3f1c9a22", "name": "Customer support"},
    "scenarios": [
        {
            "id": "a1a1a1a1-0000-4000-8000-000000000001",
            "name": "Refund policy",
            "input_messages": [{"role": "user", "content": "What is your refund policy?"}],
            "expected_response": "Refunds within 30 days.",
            "eval_type": "keyword",
            "eval_config": {"required": ["30 days"], "forbidden": [], "case_sensitive": False},
        },
        {
            "id": "a1a1a1a1-0000-4000-8000-000000000002",
            "name": "Order status as JSON",
            "input_messages": [
                {"role": "system", "content": "Reply in JSON."},
                {"role": "user", "content": "Where is order 42?"},
            ],
            "expected_response": None,
            "eval_type": "json_schema",
            "eval_config": {
                "schema": {
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                }
            },
        },
    ],
}


@pytest.fixture
def payload() -> dict[str, Any]:
    """A valid launch request matching docs/api-contract.md, with a fresh run_id."""
    p = copy.deepcopy(_BASE_PAYLOAD)
    p["run_id"] = str(uuid.uuid4())
    return p


def launch(client: TestClient, payload: dict[str, Any], **headers: str):  # type: ignore[no-untyped-def]
    h = {**AUTH, "Idempotency-Key": payload["run_id"], **headers}
    return client.post("/runs", json=payload, headers=h)


def queue_run(client: TestClient, **overrides: Any) -> str:
    """Launch a fresh run (2 scenarios) through the real API and return its external_run_id."""
    p = copy.deepcopy(_BASE_PAYLOAD)
    p["run_id"] = str(uuid.uuid4())
    p.update(overrides)
    resp = launch(client, p)
    assert resp.status_code == 202, resp.text
    return resp.json()["external_run_id"]


class FakeChatModel:
    """Test-only stand-in for a LangChain chat model: `respond(messages)` returns the reply
    text (sync or async) or raises. Records every call so tests can count LLM executions."""

    def __init__(self, respond) -> None:  # type: ignore[no-untyped-def]
        self.respond = respond
        self.calls: list[list[Any]] = []

    async def ainvoke(self, messages):  # type: ignore[no-untyped-def]
        from langchain_core.messages import AIMessage

        self.calls.append(messages)
        reply = self.respond(messages)
        if inspect.isawaitable(reply):
            reply = await reply
        return AIMessage(content=reply)


class FakeModels:
    """Test-only ModelSource handing out one model for every supported model name."""

    def __init__(self, model) -> None:  # type: ignore[no-untyped-def]
        from app.llm import resolve_model

        self.model = model
        self._resolve = resolve_model

    def supports(self, name: str) -> bool:
        return self._resolve(name) is not None

    def get(self, name: str):  # type: ignore[no-untyped-def]
        return self.model


def keyword_payload(*scenarios: dict[str, Any], model: str = "gpt-4o-mini") -> dict[str, Any]:
    """A launch request whose scenarios are all keyword-evaluated (what 2B can run).

    Each scenario dict needs `id`, `user` and `required`; `forbidden`/`case_sensitive` optional.
    """
    p = copy.deepcopy(_BASE_PAYLOAD)
    p["run_id"] = str(uuid.uuid4())
    p["agent"]["model"] = model
    p["scenarios"] = [
        {
            "id": s["id"],
            "name": s.get("name", s["id"]),
            "input_messages": [{"role": "user", "content": s["user"]}],
            "expected_response": None,
            "eval_type": "keyword",
            "eval_config": {k: s[k] for k in ("required", "forbidden", "case_sensitive") if k in s},
        }
        for s in scenarios
    ]
    return p


def run_status(client: TestClient, external_run_id: str) -> dict[str, Any]:
    resp = client.get(f"/runs/{external_run_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02):  # type: ignore[no-untyped-def]
    """Poll `predicate` until it returns something truthy; fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)


@pytest.fixture
def store(app: FastAPI, client: TestClient) -> RunStore:
    """The app's RunStore, after the lifespan has migrated the database."""
    return app.state.run_store


def make_settings(tmp_path, **overrides: Any) -> Settings:  # type: ignore[no-untyped-def]
    values: dict[str, Any] = {
        "eval_engine_api_key": API_KEY,
        "database_url": f"sqlite:///{tmp_path / 'test.db'}",
        "worker_poll_interval_seconds": 0.02,
    }
    values.update(overrides)
    # _env_file=None: never read engine/.env in tests (it may hold a real key).
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def assert_error(resp, status: int, code: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    return body["error"]

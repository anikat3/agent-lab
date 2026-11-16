import copy
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

API_KEY = "test-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def app(tmp_path) -> FastAPI:  # type: ignore[no-untyped-def]
    settings = Settings(eval_engine_api_key=API_KEY, database_url=f"sqlite:///{tmp_path / 'test.db'}")
    return create_app(settings)


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
    return Settings(**values)


def assert_error(resp, status: int, code: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    return body["error"]

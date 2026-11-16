"""Opt-in tests against the real OpenAI API. They cost (a fraction of a cent) and need network.

    RUN_LIVE_TESTS=1 OPENAI_API_KEY=sk-... pytest -m live -v

Model: OPENAI_TEST_MODEL (default gpt-4o-mini). Skipped in the normal test run.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import keyword_payload, launch, make_settings, run_status, wait_until

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("OPENAI_TEST_MODEL", "gpt-4o-mini")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="live API test; set RUN_LIVE_TESTS=1 to run"),
]

SUPPORT_PROMPT = (
    "You are the customer-support assistant for Acme Store. Policy: refunds are accepted "
    "within 30 days of purchase with a receipt. Answer in one or two sentences."
)


def run_live(tmp_path, api_key: str, *scenarios, timeout: float = 60):  # type: ignore[no-untyped-def]
    payload = keyword_payload(*scenarios, model=MODEL)
    payload["agent"]["system_prompt"] = SUPPORT_PROMPT
    settings = make_settings(tmp_path, eval_executor="langgraph", openai_api_key=api_key, model_timeout_seconds=30)
    with TestClient(create_app(settings)) as client:
        resp = launch(client, payload)
        assert resp.status_code == 202, resp.text
        ext_id = resp.json()["external_run_id"]
        return wait_until(
            lambda: (b := run_status(client, ext_id))["status"] in ("completed", "failed") and b, timeout=timeout
        )


@pytest.mark.skipif(not KEY, reason="OPENAI_API_KEY not set")
def test_real_model_run_is_executed_and_evaluated(tmp_path):
    body = run_live(
        tmp_path,
        KEY,
        {"id": "refund", "user": "How long do I have to return an item?", "required": ["30"]},
    )
    print(json.dumps(body, indent=2))  # visible with -s
    assert body["status"] == "completed", body["error"]
    [result] = body["results"]
    assert result["actual_response"]  # a real generated answer
    assert result["latency_ms"] > 0
    assert result["quality_score"] in (0.0, 1.0)
    # The policy is in the system prompt, so a working model virtually always says "30".
    assert result["passed"] is True, result["actual_response"]


def test_invalid_key_gives_structured_auth_error(tmp_path):
    bad_key = "sk-invalid-agentlab-live-test-0000000000"
    body = run_live(tmp_path, bad_key, {"id": "refund", "user": "Hi", "required": ["hello"]})
    assert body["status"] == "failed"
    assert body["error"]["code"] == "MODEL_AUTH_FAILED"
    assert bad_key not in json.dumps(body)
    assert body["results"] == []

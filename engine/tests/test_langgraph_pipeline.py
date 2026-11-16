"""End to end: POST /runs -> worker -> LangGraph executor -> SQLite -> GET /runs/{id}.

Real app, real worker, real graph and evaluator; only the model is mocked.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_openai import ChatOpenAI
from sqlalchemy import func, select, update

from app import lifecycle as lc
from app.graph import LangGraphExecutor
from app.main import create_app
from app.models import Run
from app.worker import Worker

from .conftest import (
    AUTH,
    FakeChatModel,
    FakeModels,
    assert_error,
    keyword_payload,
    launch,
    make_api_app,
    make_settings,
    run_status,
    wait_until,
)

FAKE_KEY = "sk-test-0123456789abcdefSECRET"
REFUND = {"id": "refund", "user": "What is your refund window?", "required": ["30 days"]}
SHIPPING = {"id": "shipping", "user": "Do you ship abroad?", "required": ["international"], "forbidden": ["guarantee"]}
TONE = {"id": "tone", "user": "Say hello", "required": ["Acme"], "case_sensitive": True}

ANSWERS = {
    "What is your refund window?": "You can request a refund within 30 days of purchase.",
    "Do you ship abroad?": "Yes, we ship internationally and guarantee delivery in 3 days.",
    "Say hello": "hello from acme support!",
}


def answer_by_question(messages):  # type: ignore[no-untyped-def]
    return ANSWERS[messages[-1].content]


def executor_with(respond, model_timeout=5.0) -> tuple[LangGraphExecutor, FakeChatModel]:  # type: ignore[no-untyped-def]
    model = FakeChatModel(respond)
    return LangGraphExecutor(FakeModels(model), model_timeout_seconds=model_timeout), model


def finished(client, ext_id):  # type: ignore[no-untyped-def]
    body = wait_until(lambda: (b := run_status(client, ext_id))["status"] in ("completed", "failed") and b)
    return body


def launch_ok(client, payload) -> str:  # type: ignore[no-untyped-def]
    resp = launch(client, payload)
    assert resp.status_code == 202, resp.text
    return resp.json()["external_run_id"]


# --- the success criteria -----------------------------------------------------------------


def test_single_scenario_run_completes_with_real_evaluation(tmp_path):
    executor, model = executor_with(answer_by_question)
    with TestClient(create_app(make_settings(tmp_path), executor=executor)) as client:
        ext_id = launch_ok(client, keyword_payload(REFUND))
        body = finished(client, ext_id)

    assert body["status"] == "completed"
    assert body["progress"] == {"completed": 1, "total": 1}
    assert body["error"] is None
    [result] = body["results"]
    assert result["scenario_id"] == "refund"
    assert result["passed"] is True
    assert result["quality_score"] == 1.0
    assert result["actual_response"] == ANSWERS["What is your refund window?"]
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
    assert result["failure_category"] is None and result["failure_explanation"] is None
    assert result["evaluator_details"]["missing_required"] == []
    assert len(model.calls) == 1


def test_multiple_scenarios_pass_and_fail_in_one_run(tmp_path):
    executor, model = executor_with(answer_by_question)
    app = create_app(make_settings(tmp_path), executor=executor)
    with TestClient(app) as client:
        ext_id = launch_ok(client, keyword_payload(REFUND, SHIPPING, TONE))
        body = finished(client, ext_id)

    assert body["status"] == "completed"  # failed evaluations do not fail the run
    assert body["progress"] == {"completed": 3, "total": 3}
    by_id = {r["scenario_id"]: r for r in body["results"]}
    assert [r["scenario_id"] for r in body["results"]] == ["refund", "shipping", "tone"]

    assert by_id["refund"]["passed"] is True

    assert by_id["shipping"]["passed"] is False
    assert by_id["shipping"]["quality_score"] == 0.0
    assert by_id["shipping"]["failure_category"] == "forbidden_content"
    assert by_id["shipping"]["failure_explanation"] == "Response contains forbidden keyword 'guarantee'."

    assert by_id["tone"]["passed"] is False
    assert by_id["tone"]["failure_category"] == "keyword_missing"
    assert by_id["tone"]["failure_explanation"] == "Response missing required keyword 'Acme' (case-sensitive)."
    assert len(model.calls) == 3


def test_results_are_persisted_in_sqlite(tmp_path):
    executor, _ = executor_with(answer_by_question)
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, executor=executor)) as client:
        ext_id = launch_ok(client, keyword_payload(REFUND, SHIPPING))
        finished(client, ext_id)
    # Read back with a fresh app and no executor: everything came from the database.
    with TestClient(create_app(settings)) as client:
        body = run_status(client, ext_id)
    assert body["status"] == "completed"
    assert [r["passed"] for r in body["results"]] == [True, False]
    assert body["results"][1]["actual_response"] == ANSWERS["Do you ship abroad?"]


# --- infrastructure failures vs evaluation failures ----------------------------------------


def chat_openai(handler) -> ChatOpenAI:  # type: ignore[no-untyped-def]
    return ChatOpenAI(
        model="gpt-4o-mini",
        api_key=FAKE_KEY,
        max_retries=0,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_auth_failure_fails_run_and_keeps_earlier_results(tmp_path):
    """First scenario succeeds; the key is then revoked. The run fails with a structured
    error and no fake keyword result is recorded for the second scenario."""

    def handler(request: httpx.Request) -> httpx.Response:
        question = json.loads(request.content)["messages"][-1]["content"]
        if question == "Do you ship abroad?":
            return httpx.Response(
                401, json={"error": {"message": f"Incorrect API key provided: {FAKE_KEY}", "code": "invalid_api_key"}}
            )
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWERS[question]}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    executor = LangGraphExecutor(FakeModels(chat_openai(handler)), model_timeout_seconds=5)
    with TestClient(create_app(make_settings(tmp_path), executor=executor)) as client:
        ext_id = launch_ok(client, keyword_payload(REFUND, SHIPPING, TONE))
        body = finished(client, ext_id)

    assert body["status"] == "failed"
    assert body["error"]["code"] == lc.MODEL_AUTH_FAILED
    assert FAKE_KEY not in json.dumps(body) and "sk-" not in json.dumps(body)
    assert body["progress"] == {"completed": 1, "total": 3}
    assert [r["scenario_id"] for r in body["results"]] == ["refund"]


def test_model_timeout_fails_run_with_model_timeout(tmp_path):
    async def hang(messages):  # type: ignore[no-untyped-def]
        await asyncio.sleep(10)

    executor, _ = executor_with(hang, model_timeout=0.1)
    with TestClient(create_app(make_settings(tmp_path), executor=executor)) as client:
        ext_id = launch_ok(client, keyword_payload(REFUND))
        body = finished(client, ext_id)
    assert body["status"] == "failed"
    assert body["error"]["code"] == lc.MODEL_TIMEOUT
    assert body["results"] == []


def test_run_wide_timeout_still_applies_to_model_calls(tmp_path):
    """Model budget 30s, run budget 1s: the run deadline wins and reports RUN_TIMEOUT."""

    async def slow_second(messages):  # type: ignore[no-untyped-def]
        if messages[-1].content == "Do you ship abroad?":
            await asyncio.sleep(10)
        return answer_by_question(messages)

    executor, _ = executor_with(slow_second, model_timeout=30)
    app = make_api_app(tmp_path)  # launch without a worker so the timeout can be lowered first
    with TestClient(app) as client:
        ext_id = launch_ok(client, keyword_payload(REFUND, SHIPPING))
        with app.state.session_factory() as s:
            s.execute(update(Run).where(Run.external_run_id == ext_id).values(timeout_seconds=1))
            s.commit()
        asyncio.run(Worker(app.state.run_store, executor).run_once())
        body = run_status(client, ext_id)
    assert body["error"]["code"] == lc.RUN_TIMEOUT
    assert [r["scenario_id"] for r in body["results"]] == ["refund"]


# --- idempotency: no duplicate LLM execution ------------------------------------------------


def test_repeated_launches_execute_each_scenario_once(tmp_path):
    executor, model = executor_with(answer_by_question)
    payload = keyword_payload(REFUND, SHIPPING)
    with TestClient(create_app(make_settings(tmp_path), executor=executor)) as client:
        ids = {launch_ok(client, payload) for _ in range(3)}  # app retries the launch
        [ext_id] = ids
        finished(client, ext_id)
        assert launch_ok(client, payload) == ext_id  # retry after completion
        assert run_status(client, ext_id)["status"] == "completed"
        with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
            assert s.scalar(select(func.count()).select_from(Run)) == 1
    assert len(model.calls) == 2  # one model call per scenario, ever


# --- configuration and readiness -------------------------------------------------------------


def health(client) -> dict:  # type: ignore[no-untyped-def,type-arg]
    resp = client.get("/health", headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({}, "No executor is configured (set EVAL_EXECUTOR=langgraph)"),
        ({"eval_executor": "langgraph"}, "EVAL_EXECUTOR=langgraph but OPENAI_API_KEY is not set"),
        ({"eval_executor": "langgraph", "openai_api_key": "  "}, "EVAL_EXECUTOR=langgraph but OPENAI_API_KEY is not set"),
    ],
    ids=["default", "no-key", "blank-key"],
)
def test_unconfigured_execution_refuses_launches(tmp_path, monkeypatch, overrides, reason):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EVAL_EXECUTOR", raising=False)
    app = create_app(make_settings(tmp_path, **overrides))
    with TestClient(app) as client:
        assert app.state.worker is None
        assert health(client)["execution"] == {"available": False, "executor": None, "reason": reason}
        err = assert_error(launch(client, keyword_payload(REFUND)), 503, "UNAVAILABLE")
        assert err["message"] == f"Run execution is unavailable: {reason}"
        with app.state.session_factory() as s:
            assert s.scalar(select(func.count()).select_from(Run)) == 0  # nothing stored


def test_langgraph_executor_enabled_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_EXECUTOR", "langgraph")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    settings = make_settings(tmp_path)  # values come from the environment
    app = create_app(settings)
    with TestClient(app) as client:
        assert isinstance(app.state.executor, LangGraphExecutor)
        assert app.state.worker is not None
        body = health(client)
        assert body["execution"] == {"available": True, "executor": "langgraph", "reason": None}
        assert FAKE_KEY not in json.dumps(body)
    assert FAKE_KEY not in repr(settings)


def test_llm_judge_without_any_judge_model_is_rejected_before_storage(tmp_path, payload):
    """No judge_model on the scenario and no DEFAULT_JUDGE_MODEL: refuse rather than guess."""
    executor, model = executor_with(answer_by_question)  # no default judge configured
    app = create_app(make_settings(tmp_path), executor=executor)
    payload["scenarios"][1]["eval_type"] = "llm_judge"
    payload["scenarios"][1]["eval_config"] = {"rubric": "Is the answer polite and correct?", "pass_threshold": 0.7}
    with TestClient(app) as client:
        err = assert_error(launch(client, payload), 422, "INVALID_EVAL_CONFIG")
        [bad] = err["details"]["scenarios"]
        assert bad["scenario_id"] == payload["scenarios"][1]["id"]
        assert bad["problems"] == ["judge_model is not set and the engine has no DEFAULT_JUDGE_MODEL"]
        with app.state.session_factory() as s:
            assert s.scalar(select(func.count()).select_from(Run)) == 0
    assert model.calls == []


def test_unsupported_model_is_rejected_before_storage(tmp_path):
    executor, model = executor_with(answer_by_question)
    app = create_app(make_settings(tmp_path), executor=executor)
    with TestClient(app) as client:
        err = assert_error(launch(client, keyword_payload(REFUND, model="claude-sonnet-4")), 400, "INVALID_REQUEST")
        assert err["details"] == {"model": "claude-sonnet-4"}
        with app.state.session_factory() as s:
            assert s.scalar(select(func.count()).select_from(Run)) == 0
    assert model.calls == []

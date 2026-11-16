"""One run mixing keyword, exact and json_schema scenarios, end to end and offline.

Settings -> LangGraph executor -> real ChatOpenAI -> HTTP to the local fake OpenAI server
-> evaluator registry -> worker -> SQLite -> GET /runs/{id}.
"""

import asyncio
import copy
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app import lifecycle as lc
from app.graph import LangGraphExecutor
from app.main import create_app
from app.models import Run, ScenarioResult
from app.worker import Worker

from .conftest import (
    _BASE_PAYLOAD,
    FakeChatModel,
    FakeModels,
    langgraph_settings,
    launch,
    make_api_app,
    make_settings,
    run_status,
    wait_until,
)
from .fake_openai_server import Reply

REFUND_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["approved", "denied"]}, "refund_amount": {"type": "number"}},
    "required": ["status", "refund_amount"],
    "additionalProperties": False,
}


def scenario(sid: str, question: str, eval_type: str, eval_config: dict) -> dict:  # type: ignore[type-arg]
    return {
        "id": sid,
        "name": sid,
        "input_messages": [{"role": "user", "content": question}],
        "expected_response": None,
        "eval_type": eval_type,
        "eval_config": eval_config,
    }


SCENARIOS = [
    scenario("kw-pass", "Refund window?", "keyword", {"required": ["30 days"]}),
    scenario("exact-pass", "Confirm the refund.", "exact", {"expected": "Your refund is approved.", "normalize_whitespace": True}),
    scenario("exact-fail", "Confirm again.", "exact", {"expected": "Your refund is approved.", "case_sensitive": True}),
    scenario("json-pass", "Refund status as JSON", "json_schema", {"schema": REFUND_SCHEMA}),
    scenario("json-invalid", "Status as JSON please", "json_schema", {"schema": REFUND_SCHEMA}),
    scenario("json-violation", "JSON with amount", "json_schema", {"schema": REFUND_SCHEMA}),
]

REPLIES = {
    "Refund window?": "Refunds are accepted within 30 days.",
    "Confirm the refund.": "  Your refund\n is approved.\n",
    "Confirm again.": "your refund is approved.",
    "Refund status as JSON": '{"status": "approved", "refund_amount": 42.5}',
    "Status as JSON please": '```json\n{"status": "approved", "refund_amount": 1}\n```',
    "JSON with amount": '{"status": "approved", "refund_amount": "forty", "note": "x"}',
}

EXPECTED = {  # scenario -> (passed, score, failure_category)
    "kw-pass": (True, 1.0, None),
    "exact-pass": (True, 1.0, None),
    "exact-fail": (False, 0.0, "exact_mismatch"),
    "json-pass": (True, 1.0, None),
    "json-invalid": (False, 0.0, "invalid_json"),
    "json-violation": (False, 0.0, "schema_violation"),
}


def mixed_payload() -> dict:  # type: ignore[type-arg]
    p = copy.deepcopy(_BASE_PAYLOAD)
    p["run_id"] = str(uuid.uuid4())
    p["scenarios"] = copy.deepcopy(SCENARIOS)
    return p


def finished(client, ext_id):  # type: ignore[no-untyped-def]
    return wait_until(lambda: (b := run_status(client, ext_id))["status"] in ("completed", "failed") and b)


def test_mixed_run_through_real_openai_client_and_local_server(tmp_path, fake_openai):
    fake_openai.respond = lambda req: REPLIES[req.last_user_message]
    with TestClient(create_app(langgraph_settings(tmp_path))) as client:
        resp = launch(client, mixed_payload())
        assert resp.status_code == 202, resp.text  # all three types accepted at launch
        body = finished(client, resp.json()["external_run_id"])

    assert body["status"] == "completed"
    assert body["error"] is None
    assert body["progress"] == {"completed": 6, "total": 6}
    got = {r["scenario_id"]: (r["passed"], r["quality_score"], r["failure_category"]) for r in body["results"]}
    assert got == EXPECTED
    assert [r["scenario_id"] for r in body["results"]] == [s["id"] for s in SCENARIOS]  # launch order

    by_id = {r["scenario_id"]: r for r in body["results"]}
    assert by_id["exact-fail"]["failure_explanation"].endswith("The texts differ only in letter case.")
    assert "Markdown code fence" in by_id["json-invalid"]["failure_explanation"]
    assert by_id["json-violation"]["evaluator_details"]["error_count"] == 2
    # Each scenario was routed to its own evaluator.
    assert {r["scenario_id"]: r["evaluator_details"]["evaluator"] for r in body["results"]} == {
        "kw-pass": "keyword",
        "exact-pass": "exact",
        "exact-fail": "exact",
        "json-pass": "json_schema",
        "json-invalid": "json_schema",
        "json-violation": "json_schema",
    }
    assert len(fake_openai.requests) == 6  # one model call per scenario, no extra (judge) calls


def test_mixed_results_persist_in_sqlite(tmp_path):
    executor = LangGraphExecutor(FakeModels(FakeChatModel(lambda m: REPLIES[m[-1].content])), model_timeout_seconds=5)
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, executor=executor)) as client:
        ext_id = launch(client, mixed_payload()).json()["external_run_id"]
        finished(client, ext_id)
        with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
            rows = s.scalars(
                select(ScenarioResult).where(ScenarioResult.external_run_id == ext_id).order_by(ScenarioResult.id)
            ).all()
            assert [(r.scenario_id, r.passed, r.failure_category) for r in rows] == [
                (sid, *EXPECTED[sid][::2]) for sid in EXPECTED
            ]
            assert rows[3].actual_response == REPLIES["Refund status as JSON"]
    with TestClient(create_app(settings)) as client:  # fresh process, no executor: read back from disk
        assert run_status(client, ext_id)["progress"] == {"completed": 6, "total": 6}


def test_model_auth_failure_mid_mixed_run_keeps_earlier_results(tmp_path, fake_openai):
    def respond(req):  # type: ignore[no-untyped-def]
        if req.last_user_message == "Refund status as JSON":
            return Reply(401, "invalid_api_key")
        return REPLIES[req.last_user_message]

    fake_openai.respond = respond
    with TestClient(create_app(langgraph_settings(tmp_path))) as client:
        body = finished(client, launch(client, mixed_payload()).json()["external_run_id"])
    assert body["status"] == "failed"
    assert body["error"]["code"] == lc.MODEL_AUTH_FAILED
    assert [r["scenario_id"] for r in body["results"]] == ["kw-pass", "exact-pass", "exact-fail"]


def test_run_timeout_still_applies_to_mixed_runs(tmp_path):
    async def respond(messages):  # type: ignore[no-untyped-def]
        if messages[-1].content == "Refund status as JSON":
            await asyncio.sleep(10)
        return REPLIES[messages[-1].content]

    executor = LangGraphExecutor(FakeModels(FakeChatModel(respond)), model_timeout_seconds=30)
    app = make_api_app(tmp_path)
    with TestClient(app) as client:
        ext_id = launch(client, mixed_payload()).json()["external_run_id"]
        with app.state.session_factory() as s:
            s.execute(update(Run).where(Run.external_run_id == ext_id).values(timeout_seconds=1))
            s.commit()
        asyncio.run(Worker(app.state.run_store, executor).run_once())
        body = run_status(client, ext_id)
    assert body["error"]["code"] == lc.RUN_TIMEOUT
    assert len(body["results"]) == 3


def test_repeated_mixed_launch_executes_each_scenario_once(tmp_path):
    model = FakeChatModel(lambda m: REPLIES[m[-1].content])
    executor = LangGraphExecutor(FakeModels(model), model_timeout_seconds=5)
    payload = mixed_payload()
    with TestClient(create_app(make_settings(tmp_path), executor=executor)) as client:
        ids = {launch(client, payload).json()["external_run_id"] for _ in range(3)}
        [ext_id] = ids
        finished(client, ext_id)
        assert launch(client, payload).json()["external_run_id"] == ext_id
        with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
            assert s.scalar(select(func.count()).select_from(Run)) == 1
    assert len(model.calls) == 6


def test_llm_judge_needs_a_configured_judge_at_launch(tmp_path):
    executor = LangGraphExecutor(FakeModels(FakeChatModel(lambda m: "x")), model_timeout_seconds=5)  # no default judge
    payload = mixed_payload()
    payload["scenarios"].append(
        scenario("judge", "Be polite", "llm_judge", {"rubric": "Is the reply polite and accurate?", "pass_threshold": 0.7})
    )
    with TestClient(create_app(make_settings(tmp_path), executor=executor)) as client:
        resp = launch(client, payload)
    assert resp.status_code == 422
    [bad] = json.loads(resp.text)["error"]["details"]["scenarios"]
    assert bad["scenario_id"] == "judge"
    assert bad["problems"] == ["judge_model is not set and the engine has no DEFAULT_JUDGE_MODEL"]

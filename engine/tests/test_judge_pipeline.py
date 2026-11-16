"""LLM-as-judge end to end, offline: real app, worker, LangGraph and ChatOpenAI, talking
over HTTP to the local fake OpenAI server, which plays both the agent and the judge."""

import asyncio
import copy
from contextlib import contextmanager
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select, update

from app import lifecycle as lc
from app.graph import LangGraphExecutor
from app.llm import ChatModels
from app.main import create_app
from app.models import Run, ScenarioResult
from app.worker import Worker

from .conftest import _BASE_PAYLOAD, assert_error, langgraph_settings, launch, make_api_app, run_status, wait_until
from .fake_openai_server import TEST_KEY, FakeRequest, Reply

RUBRIC = "Evaluate whether the agent provides a helpful, polite and accurate response."
SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "refund_amount": {"type": "number"}},
    "required": ["status", "refund_amount"],
}


def scenario(sid, question, eval_type, eval_config):  # type: ignore[no-untyped-def]
    return {
        "id": sid,
        "name": sid,
        "input_messages": [{"role": "user", "content": question}],
        "expected_response": None,
        "eval_type": eval_type,
        "eval_config": eval_config,
    }


ALL_FOUR = [
    scenario("keyword", "Refund window?", "keyword", {"required": ["30 days"]}),
    scenario("exact", "Confirm.", "exact", {"expected": "Your refund is approved."}),
    scenario("json", "Status as JSON", "json_schema", {"schema": SCHEMA}),
    scenario("judge-pass", "Can I get a refund?", "llm_judge", {"rubric": RUBRIC, "pass_threshold": 0.8}),
    scenario("judge-fail", "Why so slow?", "llm_judge", {"rubric": RUBRIC, "pass_threshold": 0.8, "judge_model": "gpt-4.1-mini"}),
]

AGENT_REPLIES = {
    "Refund window?": "Refunds are accepted within 30 days.",
    "Confirm.": "Your refund is approved.",
    "Status as JSON": '{"status": "approved", "refund_amount": 42.5}',
    "Can I get a refund?": "Of course! You can request a refund within 30 days of purchase.",
    "Why so slow?": "Not my problem.",
}

# The fake judge grades by what it is shown, like a real one would.
JUDGE_SCORES = {
    "Of course! You can request a refund within 30 days of purchase.": (0.93, "Polite, helpful and states the policy."),
    "Not my problem.": (0.1, "Dismissive and unhelpful."),
}


def agent_and_judge(req: FakeRequest):  # type: ignore[no-untyped-def]
    if req.is_structured:  # judge call
        graded = json.loads(req.messages[-1]["content"])["agent_response"]
        score, reasoning = JUDGE_SCORES[graded]
        return json.dumps({"score": score, "reasoning": reasoning})
    return AGENT_REPLIES[req.last_user_message]


def payload(scenarios=ALL_FOUR) -> dict:  # type: ignore[no-untyped-def,type-arg]
    p = copy.deepcopy(_BASE_PAYLOAD)
    p["run_id"] = str(uuid.uuid4())
    p["scenarios"] = copy.deepcopy(scenarios)
    return p


def judged_settings(tmp_path, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("default_judge_model", "gpt-4o-mini")
    return langgraph_settings(tmp_path, **kw)


def finished(client, ext_id):  # type: ignore[no-untyped-def]
    return wait_until(lambda: (b := run_status(client, ext_id))["status"] in ("completed", "failed") and b)


def run(tmp_path, fake_openai, scenarios=ALL_FOUR, **settings):  # type: ignore[no-untyped-def]
    with TestClient(create_app(judged_settings(tmp_path, **settings))) as client:
        resp = launch(client, payload(scenarios))
        assert resp.status_code == 202, resp.text
        return finished(client, resp.json()["external_run_id"])


# --- the success criteria --------------------------------------------------------------------


def test_run_with_all_four_evaluation_types(tmp_path, fake_openai):
    fake_openai.respond = agent_and_judge
    body = run(tmp_path, fake_openai)

    assert body["status"] == "completed"
    assert body["error"] is None
    assert body["progress"] == {"completed": 5, "total": 5}
    by_id = {r["scenario_id"]: r for r in body["results"]}
    assert [r["scenario_id"] for r in body["results"]] == [s["id"] for s in ALL_FOUR]
    assert {k: (r["passed"], r["quality_score"]) for k, r in by_id.items()} == {
        "keyword": (True, 1.0),
        "exact": (True, 1.0),
        "json": (True, 1.0),
        "judge-pass": (True, 0.93),
        "judge-fail": (False, 0.1),
    }
    fail = by_id["judge-fail"]
    assert fail["failure_category"] == "judge_below_threshold"
    assert fail["failure_explanation"] == "Judge score 0.1 is below the pass threshold 0.8. Dismissive and unhelpful."
    assert fail["actual_response"] == "Not my problem."
    assert fail["evaluator_details"]["judge_model"] == "gpt-4.1-mini"  # scenario override
    assert by_id["judge-pass"]["evaluator_details"]["judge_model"] == "gpt-4o-mini"  # engine default
    assert by_id["judge-pass"]["failure_explanation"] is None


def test_deterministic_evaluators_never_call_the_judge(tmp_path, fake_openai):
    fake_openai.respond = agent_and_judge
    run(tmp_path, fake_openai)
    agent_calls = [r for r in fake_openai.requests if not r.is_structured]
    judge_calls = [r for r in fake_openai.requests if r.is_structured]
    assert len(agent_calls) == 5  # one per scenario
    assert len(judge_calls) == 2  # only the two llm_judge scenarios
    assert sorted(r.model for r in judge_calls) == ["gpt-4.1-mini", "gpt-4o-mini"]
    graded = sorted(json.loads(r.messages[-1]["content"])["agent_response"] for r in judge_calls)
    assert graded == sorted(JUDGE_SCORES)


def test_judge_results_persist_in_sqlite(tmp_path, fake_openai):
    fake_openai.respond = agent_and_judge
    settings = judged_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        ext_id = launch(client, payload()).json()["external_run_id"]
        finished(client, ext_id)
        with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
            row = s.scalars(
                select(ScenarioResult).where(
                    ScenarioResult.external_run_id == ext_id, ScenarioResult.scenario_id == "judge-pass"
                )
            ).one()
            assert (row.passed, row.quality_score, row.failure_category) == (True, 0.93, None)
            assert row.evaluator_details["reasoning"] == "Polite, helpful and states the policy."
    with TestClient(create_app(settings)) as client:  # read back by a fresh process
        assert run_status(client, ext_id)["results"][3]["quality_score"] == 0.93


# --- judge infrastructure failures fail the run, keep earlier results ------------------------


@pytest.mark.parametrize(
    "judge_reply,code",
    [
        (Reply(401, "invalid_api_key"), lc.JUDGE_AUTH_FAILED),
        (Reply(503), lc.JUDGE_PROVIDER_ERROR),
        ('{"score": 1.7, "reasoning": "x"}', lc.JUDGE_INVALID_OUTPUT),
        ("definitely a pass", lc.JUDGE_INVALID_OUTPUT),
    ],
    ids=["auth", "provider", "out-of-range", "unparseable"],
)
def test_judge_failure_after_earlier_scenarios(tmp_path, fake_openai, judge_reply, code):
    fake_openai.respond = lambda req: judge_reply if req.is_structured else AGENT_REPLIES[req.last_user_message]
    body = run(tmp_path, fake_openai)
    assert body["status"] == "failed"
    assert body["error"]["code"] == code
    # keyword, exact and json completed before the first judge scenario; no fake judge result.
    assert [r["scenario_id"] for r in body["results"]] == ["keyword", "exact", "json"]
    assert body["progress"] == {"completed": 3, "total": 5}


def test_low_score_does_not_stop_the_run(tmp_path, fake_openai):
    fake_openai.respond = agent_and_judge
    order = [ALL_FOUR[4], ALL_FOUR[0]]  # failing judge scenario first
    body = run(tmp_path, fake_openai, order)
    assert body["status"] == "completed"
    assert [(r["scenario_id"], r["passed"]) for r in body["results"]] == [("judge-fail", False), ("keyword", True)]


def test_judge_timeout_fails_run_with_judge_timeout(tmp_path, fake_openai):
    # MODEL_TIMEOUT_SECONDS applies to the agent call too, so leave it a wide margin: a slow
    # first HTTP request on a busy machine must not turn this into MODEL_TIMEOUT.
    def respond(req):  # type: ignore[no-untyped-def]
        if req.is_structured:
            time.sleep(3.0)
        return agent_and_judge(req)

    fake_openai.respond = respond
    body = run(tmp_path, fake_openai, [ALL_FOUR[0], ALL_FOUR[3]], model_timeout_seconds=1.0)
    assert body["error"]["code"] == lc.JUDGE_TIMEOUT
    assert [r["scenario_id"] for r in body["results"]] == ["keyword"]


def test_run_timeout_covers_the_judge_call(tmp_path, fake_openai):
    def respond(req):  # type: ignore[no-untyped-def]
        if req.is_structured:
            time.sleep(3)
        return agent_and_judge(req)

    fake_openai.respond = respond
    app = make_api_app(tmp_path)
    with TestClient(app) as client:
        ext_id = launch(client, payload([ALL_FOUR[0], ALL_FOUR[3]])).json()["external_run_id"]
        with app.state.session_factory() as s:
            s.execute(update(Run).where(Run.external_run_id == ext_id).values(timeout_seconds=1))
            s.commit()

        async def work() -> None:
            models = ChatModels(
                openai_api_key=SecretStr(TEST_KEY), timeout_seconds=30, max_retries=0, openai_base_url=fake_openai.base_url
            )
            executor = LangGraphExecutor(models, model_timeout_seconds=30, default_judge_model="gpt-4o-mini")
            try:
                await Worker(app.state.run_store, executor).run_once()
            finally:
                await executor.aclose()

        asyncio.run(work())
        body = run_status(client, ext_id)
    assert body["error"]["code"] == lc.RUN_TIMEOUT
    assert [r["scenario_id"] for r in body["results"]] == ["keyword"]


# --- idempotency -----------------------------------------------------------------------------


def test_repeated_launch_makes_no_duplicate_agent_or_judge_calls(tmp_path, fake_openai):
    fake_openai.respond = agent_and_judge
    p = payload()
    with TestClient(create_app(judged_settings(tmp_path))) as client:
        ids = {launch(client, p).json()["external_run_id"] for _ in range(3)}
        [ext_id] = ids
        finished(client, ext_id)
        assert launch(client, p).json()["external_run_id"] == ext_id
        with client.app.state.session_factory() as s:  # type: ignore[attr-defined]
            assert s.scalar(select(func.count()).select_from(Run)) == 1
    assert len(fake_openai.requests) == 7  # 5 agent calls + 2 judge calls, once each


# --- launch-time configuration checks --------------------------------------------------------


def test_unsupported_judge_model_is_rejected_at_launch(tmp_path, fake_openai):
    bad = scenario("j", "hi", "llm_judge", {"rubric": RUBRIC, "pass_threshold": 0.5, "judge_model": "claude-sonnet-4"})
    with TestClient(create_app(judged_settings(tmp_path))) as client:
        err = assert_error(launch(client, payload([ALL_FOUR[0], bad])), 422, "INVALID_EVAL_CONFIG")
    assert err["details"]["scenarios"] == [
        {"scenario_id": "j", "name": "j", "problems": ["judge_model 'claude-sonnet-4' is not supported by this engine (supported: OpenAI models)"]}
    ]
    assert fake_openai.requests == []


def test_missing_judge_configuration_is_rejected_at_launch(tmp_path, fake_openai):
    with TestClient(create_app(langgraph_settings(tmp_path))) as client:  # no DEFAULT_JUDGE_MODEL
        err = assert_error(launch(client, payload([ALL_FOUR[3]])), 422, "INVALID_EVAL_CONFIG")
    assert err["details"]["scenarios"][0]["problems"] == ["judge_model is not set and the engine has no DEFAULT_JUDGE_MODEL"]
    assert fake_openai.requests == []


def test_scenario_judge_model_works_without_a_default(tmp_path, fake_openai):
    fake_openai.respond = agent_and_judge
    with TestClient(create_app(langgraph_settings(tmp_path))) as client:  # no default needed here
        resp = launch(client, payload([ALL_FOUR[4]]))
        assert resp.status_code == 202, resp.text
        body = finished(client, resp.json()["external_run_id"])
    assert body["results"][0]["evaluator_details"]["judge_model_source"] == "scenario"


def test_default_judge_model_from_environment(tmp_path, fake_openai, monkeypatch):
    monkeypatch.setenv("DEFAULT_JUDGE_MODEL", "gpt-4o-mini")
    fake_openai.respond = agent_and_judge
    settings = langgraph_settings(tmp_path)  # picks DEFAULT_JUDGE_MODEL up from the environment
    assert settings.default_judge_model == "gpt-4o-mini"
    with TestClient(create_app(settings)) as client:
        body = finished(client, launch(client, payload([ALL_FOUR[3]])).json()["external_run_id"])
    assert body["status"] == "completed"
    assert body["results"][0]["evaluator_details"]["judge_model_source"] == "default"


# --- the judge model is frozen at launch ---------------------------------------------------


def stored_judge_models(app, ext_id) -> dict:  # type: ignore[no-untyped-def,type-arg]
    from app.models import RunScenario

    with app.state.session_factory() as s:
        rows = s.scalars(select(RunScenario).where(RunScenario.external_run_id == ext_id)).all()
        return {r.scenario_id: r.judge_model for r in rows}


@contextmanager
def launched_without_worker(tmp_path, default_judge_model, p):  # type: ignore[no-untyped-def]
    """Launch with the real executor built from settings but no worker, so the run waits.
    Yields (app, ext_id) while the app is still running."""
    app = create_app(judged_settings(tmp_path, default_judge_model=default_judge_model), start_worker=False)
    with TestClient(app) as client:
        resp = launch(client, p)
        assert resp.status_code == 202, resp.text
        yield app, resp.json()["external_run_id"]


def test_resolved_judge_model_is_stored_with_the_run(tmp_path, fake_openai):
    with launched_without_worker(tmp_path, "gpt-4o-mini", payload()) as (app, ext_id):
        assert stored_judge_models(app, ext_id) == {
            "keyword": None,
            "exact": None,
            "json": None,
            "judge-pass": "gpt-4o-mini",  # from DEFAULT_JUDGE_MODEL at launch
            "judge-fail": "gpt-4.1-mini",  # the scenario's own judge_model
        }


def test_changing_the_default_after_launch_does_not_change_the_judge(tmp_path, fake_openai):
    fake_openai.respond = agent_and_judge
    with launched_without_worker(tmp_path, "gpt-4o-mini", payload([ALL_FOUR[3]])) as (_, ext_id):
        pass

    # Operator changes DEFAULT_JUDGE_MODEL and restarts before the run is executed.
    with TestClient(create_app(judged_settings(tmp_path, default_judge_model="gpt-4.1"))) as client:
        body = finished(client, ext_id)

    [judge_call] = [r for r in fake_openai.requests if r.is_structured]
    assert judge_call.model == "gpt-4o-mini"  # the model frozen at launch, not gpt-4.1
    details = body["results"][0]["evaluator_details"]
    assert (details["judge_model"], details["judge_model_source"], details["judge_model_frozen"]) == (
        "gpt-4o-mini",
        "default",
        True,
    )


def test_replayed_launch_keeps_the_originally_frozen_model(tmp_path, fake_openai):
    p = payload([ALL_FOUR[3]])
    with launched_without_worker(tmp_path, "gpt-4o-mini", p) as (_, ext_id):
        pass
    with launched_without_worker(tmp_path, "gpt-4.1", p) as (app, replay_id):  # same run_id, new default
        assert replay_id == ext_id
        assert stored_judge_models(app, ext_id) == {"judge-pass": "gpt-4o-mini"}


def test_runs_stored_before_freezing_resolve_at_execution(tmp_path, fake_openai):
    """Runs queued before migration 3 have no frozen model: they use the current default,
    and the result says so."""
    from app.models import RunScenario

    fake_openai.respond = agent_and_judge
    with launched_without_worker(tmp_path, "gpt-4o-mini", payload([ALL_FOUR[3]])) as (app, ext_id):
        with app.state.session_factory() as s:
            s.execute(update(RunScenario).where(RunScenario.external_run_id == ext_id).values(judge_model=None))
            s.commit()
    with TestClient(create_app(judged_settings(tmp_path, default_judge_model="gpt-4.1"))) as client:
        body = finished(client, ext_id)
    details = body["results"][0]["evaluator_details"]
    assert (details["judge_model"], details["judge_model_frozen"]) == ("gpt-4.1", False)

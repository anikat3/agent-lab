"""The LangGraph workflow and LangGraphExecutor, with mocked models (no API calls)."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app import lifecycle as lc
from app.evaluators import Evaluation
from app.execution import ExecutionError, RunSpec, ScenarioOutcome
from app.graph import LangGraphExecutor, build_eval_graph
from app.schemas import AgentSpec, Message, ScenarioSpec, SuiteSpec

from .conftest import FakeChatModel, FakeModels

AGENT = AgentSpec(id="a1", name="Support bot", system_prompt="You are Acme support.", model="gpt-4o-mini")


def scenario(user="What is your refund policy?", required=("30 days",), forbidden=None, eval_type="keyword"):  # type: ignore[no-untyped-def]
    config = {"required": list(required)}
    if forbidden:
        config["forbidden"] = list(forbidden)
    return ScenarioSpec(
        id="s1",
        name="Refunds",
        input_messages=[Message(role="user", content=user)],
        expected_response=None,
        eval_type=eval_type,
        eval_config=config,
    )


def run_spec(*scenarios: ScenarioSpec) -> RunSpec:
    return RunSpec(
        external_run_id="run_x",
        run_id="r",
        agent=AGENT,
        suite=SuiteSpec(id="s", name="Suite"),
        timeout_seconds=300,
        started_at=datetime.now(UTC),
        scenarios=scenarios,
    )


def execute(executor: LangGraphExecutor, sc: ScenarioSpec) -> ScenarioOutcome:
    return asyncio.run(executor(run_spec(sc), sc))


# --- the graph itself ---------------------------------------------------------------------


def test_graph_routes_conditionally_after_the_agent_call():
    graph = build_eval_graph(FakeModels(FakeChatModel(lambda m: "x")), 5).get_graph()
    assert set(graph.nodes) == {"__start__", "call_agent", "evaluate", "judge", "__end__"}
    edges = {(e.source, e.target, e.conditional) for e in graph.edges}
    assert edges == {
        ("__start__", "call_agent", False),
        ("call_agent", "evaluate", True),  # conditional: deterministic evaluators
        ("call_agent", "judge", True),  # conditional: llm_judge
        ("evaluate", "__end__", False),
        ("judge", "__end__", False),
    }


def test_graph_state_after_execution():
    model = FakeChatModel(lambda m: "Refunds are accepted within 30 days.")
    graph = build_eval_graph(FakeModels(model), 5)
    sc = scenario()
    state = asyncio.run(graph.ainvoke({"agent": AGENT, "scenario": sc}))

    assert state["agent"] is AGENT and state["scenario"] is sc  # inputs untouched
    assert state["response"] == "Refunds are accepted within 30 days."
    assert isinstance(state["latency_ms"], int) and state["latency_ms"] >= 0
    assert isinstance(state["evaluation"], Evaluation) and state["evaluation"].passed


def test_agent_node_sends_system_prompt_and_scenario_messages():
    model = FakeChatModel(lambda m: "ok")
    asyncio.run(build_eval_graph(FakeModels(model), 5).ainvoke({"agent": AGENT, "scenario": scenario(user="Hi")}))
    [messages] = model.calls
    assert [(type(m), m.content) for m in messages] == [
        (SystemMessage, "You are Acme support."),
        (HumanMessage, "Hi"),
    ]


def test_latency_measures_the_model_call():
    async def slow(messages):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.05)
        return "30 days"

    state = asyncio.run(
        build_eval_graph(FakeModels(FakeChatModel(slow)), 5).ainvoke({"agent": AGENT, "scenario": scenario()})
    )
    assert 50 <= state["latency_ms"] < 1000


def test_works_with_a_real_langchain_chat_model_class():
    model = GenericFakeChatModel(messages=iter([AIMessage(content="Within 30 days.")]))
    state = asyncio.run(build_eval_graph(FakeModels(model), 5).ainvoke({"agent": AGENT, "scenario": scenario()}))
    assert state["response"] == "Within 30 days." and state["evaluation"].passed


# --- executor outcomes ---------------------------------------------------------------------


def test_passing_scenario_outcome():
    executor = LangGraphExecutor(FakeModels(FakeChatModel(lambda m: "Refunds within 30 days.")), model_timeout_seconds=5)
    out = execute(executor, scenario())
    assert out.passed is True
    assert out.quality_score == 1.0
    assert out.actual_response == "Refunds within 30 days."
    assert out.latency_ms is not None and out.latency_ms >= 0
    assert out.failure_category is None and out.failure_explanation is None
    assert out.evaluator_details["evaluator"] == "keyword"


def test_failed_evaluation_is_an_outcome_not_an_error():
    executor = LangGraphExecutor(FakeModels(FakeChatModel(lambda m: "No refunds, guaranteed.")), model_timeout_seconds=5)
    out = execute(executor, scenario(forbidden=["guarantee"]))
    assert out.passed is False
    assert out.quality_score == 0.0
    assert out.failure_category == "forbidden_content"
    assert "forbidden keyword 'guarantee'" in out.failure_explanation
    assert "missing required keyword '30 days'" in out.failure_explanation
    assert out.actual_response == "No refunds, guaranteed."


def test_per_call_model_timeout():
    async def hang(messages):  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)

    executor = LangGraphExecutor(FakeModels(FakeChatModel(hang)), model_timeout_seconds=0.05)
    with pytest.raises(ExecutionError) as e:
        execute(executor, scenario())
    assert e.value.code == lc.MODEL_TIMEOUT
    assert "0.05s" in e.value.message


def test_unexpected_model_exception_propagates_unchanged():
    def broken(messages):  # type: ignore[no-untyped-def]
        raise ValueError("bug in our code")

    executor = LangGraphExecutor(FakeModels(FakeChatModel(broken)), model_timeout_seconds=5)
    with pytest.raises(ValueError, match="bug in our code"):  # the worker turns this into EXECUTION_ERROR
        execute(executor, scenario())


def test_unsupported_eval_type_fails_before_any_model_call():
    """Launch rejects unknown types; this guards runs stored before a type was removed."""
    model = FakeChatModel(lambda m: "x")
    executor = LangGraphExecutor(FakeModels(model), model_timeout_seconds=5)
    with pytest.raises(ExecutionError) as e:
        execute(executor, scenario(eval_type="regex"))
    assert e.value.code == lc.UNSUPPORTED_EVAL_TYPE
    assert model.calls == []


def test_capabilities():
    executor = LangGraphExecutor(FakeModels(FakeChatModel(lambda m: "x")), model_timeout_seconds=5)
    assert executor.supported_eval_types == {"keyword", "exact", "json_schema", "llm_judge"}
    assert executor.supports_model("gpt-4o-mini")
    assert not executor.supports_model("claude-sonnet-4")


# --- through the real ChatOpenAI client, mocked at the HTTP layer --------------------------


def openai_backed_executor(handler) -> LangGraphExecutor:  # type: ignore[no-untyped-def]
    model = ChatOpenAI(
        model="gpt-4o-mini",
        api_key="sk-test-0123456789abcdef",
        max_retries=0,
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return LangGraphExecutor(FakeModels(model), model_timeout_seconds=5)


def test_end_to_end_with_chat_openai_client():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Within 30 days."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    out = execute(openai_backed_executor(handler), scenario())
    assert out.passed and out.actual_response == "Within 30 days."


def test_auth_failure_is_an_execution_error_not_a_failed_evaluation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-test-****cdef", "code": "invalid_api_key"}})

    with pytest.raises(ExecutionError) as e:
        execute(openai_backed_executor(handler), scenario())
    assert e.value.code == lc.MODEL_AUTH_FAILED
    assert "sk-" not in e.value.message

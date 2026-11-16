"""The evaluation workflow: a two-node LangGraph StateGraph, wrapped as a ScenarioExecutor.

    START -> call_agent -> evaluate -> END

call_agent  builds the messages, calls the agent's model, records response + latency.
evaluate    runs the deterministic evaluator for the scenario's eval_type.

The graph handles exactly one scenario. The worker owns everything around it: claiming,
order, deadlines, persistence and the final status.
"""

import asyncio
import time
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app import lifecycle
from app.config import Settings
from app.evaluators import EVALUATORS, Evaluation
from app.execution import ExecutionError, RunSpec, ScenarioOutcome
from app.llm import ChatModels, ModelSource, build_messages, map_model_error
from app.schemas import AgentSpec, ScenarioSpec


class EvalState(TypedDict):
    # Inputs
    agent: AgentSpec
    scenario: ScenarioSpec
    # Written by call_agent
    response: NotRequired[str]
    latency_ms: NotRequired[int]
    # Written by evaluate
    evaluation: NotRequired[Evaluation]


def build_eval_graph(models: ModelSource, model_timeout_seconds: float) -> CompiledStateGraph:
    async def call_agent(state: EvalState) -> dict:
        agent, scenario = state["agent"], state["scenario"]
        model = models.get(agent.model)
        messages = build_messages(agent, scenario)
        started = time.perf_counter()
        try:
            # Hard ceiling for this one call, retries included. The client also has its own
            # per-request timeout; this guards against retries adding up.
            async with asyncio.timeout(model_timeout_seconds):
                reply = await model.ainvoke(messages)
        except TimeoutError:
            raise ExecutionError(
                lifecycle.MODEL_TIMEOUT, f"Model call exceeded {model_timeout_seconds:g}s for scenario {scenario.name!r}"
            ) from None
        except Exception as e:
            mapped = map_model_error(e, agent.model)
            if mapped is None:
                raise
            raise mapped from e
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {"response": reply.text, "latency_ms": latency_ms}

    async def evaluate(state: EvalState) -> dict:
        scenario = state["scenario"]
        evaluator = EVALUATORS.get(scenario.eval_type)
        if evaluator is None:  # rejected at launch; checked again in case of old stored runs
            raise ExecutionError(
                lifecycle.UNSUPPORTED_EVAL_TYPE, f"No evaluator for eval_type {scenario.eval_type!r} yet"
            )
        return {"evaluation": evaluator(state["response"], scenario.eval_config)}

    graph = StateGraph(EvalState)
    graph.add_node("call_agent", call_agent)
    graph.add_node("evaluate", evaluate)
    graph.add_edge(START, "call_agent")
    graph.add_edge("call_agent", "evaluate")
    graph.add_edge("evaluate", END)
    return graph.compile()


class LangGraphExecutor:
    """ScenarioExecutor backed by the evaluation graph."""

    name = "langgraph"
    # Capabilities checked at launch (see app/execution.py).
    supported_eval_types = frozenset(EVALUATORS)

    def __init__(self, models: ModelSource, *, model_timeout_seconds: float) -> None:
        self._models = models
        self._graph = build_eval_graph(models, model_timeout_seconds)

    def supports_model(self, model: str) -> bool:
        return self._models.supports(model)

    async def aclose(self) -> None:
        """Release the model clients (called on app shutdown)."""
        aclose = getattr(self._models, "aclose", None)
        if aclose is not None:
            await aclose()

    async def __call__(self, run: RunSpec, scenario: ScenarioSpec) -> ScenarioOutcome:
        if scenario.eval_type not in EVALUATORS:
            # Checked before the graph so no model call is spent on a result we can't evaluate.
            raise ExecutionError(
                lifecycle.UNSUPPORTED_EVAL_TYPE, f"No evaluator for eval_type {scenario.eval_type!r} yet"
            )
        final = await self._graph.ainvoke({"agent": run.agent, "scenario": scenario})
        evaluation: Evaluation = final["evaluation"]
        return ScenarioOutcome(
            passed=evaluation.passed,
            quality_score=evaluation.quality_score,
            latency_ms=final["latency_ms"],
            actual_response=final["response"],
            failure_category=evaluation.failure_category,
            failure_explanation=evaluation.failure_explanation,
            evaluator_details=evaluation.details,
        )


def build_executor(settings: Settings) -> tuple[LangGraphExecutor | None, str | None]:
    """The executor the settings ask for, or (None, reason) if it can't be configured."""
    if settings.eval_executor == "none":
        return None, "No executor is configured (set EVAL_EXECUTOR=langgraph)"
    key = settings.openai_api_key
    if key is None or not key.get_secret_value().strip():
        return None, "EVAL_EXECUTOR=langgraph but OPENAI_API_KEY is not set"
    models = ChatModels(
        openai_api_key=key,
        timeout_seconds=settings.model_timeout_seconds,
        max_retries=settings.model_max_retries,
        openai_base_url=settings.openai_base_url,
    )
    return LangGraphExecutor(models, model_timeout_seconds=settings.model_timeout_seconds), None

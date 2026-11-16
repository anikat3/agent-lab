"""The evaluation workflow: a LangGraph StateGraph, wrapped as a ScenarioExecutor.

                          ┌─ "evaluate" ─ deterministic evaluator ─┐
    START -> call_agent ──┤   (keyword, exact, json_schema)        ├─> END
                          └─ "judge" ──── LLM-as-judge (async) ────┘

call_agent  builds the messages, calls the agent's model, records response + latency.
route       picks the next node from the scenario's eval_type (a LangGraph conditional edge).
evaluate    runs the deterministic evaluator; never calls a model.
judge       asks the judge model to grade the response (one more model call).
Both branches write the same `evaluation` field, so the output format is identical.

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
from app.judge import judge_response, resolve_judge_model
from app.llm import ChatModels, ModelSource, build_messages, map_model_error
from app.schemas import AgentSpec, ScenarioSpec


class EvalState(TypedDict):
    # Inputs
    agent: AgentSpec
    scenario: ScenarioSpec
    judge_model: NotRequired[str | None]  # frozen at launch; llm_judge only
    # Written by call_agent
    response: NotRequired[str]
    latency_ms: NotRequired[int]
    # Written by evaluate
    evaluation: NotRequired[Evaluation]


LLM_JUDGE = "llm_judge"
SUPPORTED_EVAL_TYPES = frozenset(EVALUATORS) | {LLM_JUDGE}


def route_evaluation(state: "EvalState") -> str:
    """Conditional edge after call_agent: model-graded or deterministic."""
    return "judge" if state["scenario"].eval_type == LLM_JUDGE else "evaluate"


def build_eval_graph(
    models: ModelSource,
    model_timeout_seconds: float,
    *,
    default_judge_model: str | None = None,
    judge_output_retries: int = 0,
) -> CompiledStateGraph:
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

    async def judge(state: EvalState) -> dict:
        evaluation = await judge_response(
            models=models,
            scenario=state["scenario"],
            response=state["response"],
            frozen_judge_model=state.get("judge_model"),
            default_judge_model=default_judge_model,
            timeout_seconds=model_timeout_seconds,
            output_retries=judge_output_retries,
        )
        return {"evaluation": evaluation}

    graph = StateGraph(EvalState)
    graph.add_node("call_agent", call_agent)
    graph.add_node("evaluate", evaluate)
    graph.add_node("judge", judge)
    graph.add_edge(START, "call_agent")
    graph.add_conditional_edges("call_agent", route_evaluation, {"evaluate": "evaluate", "judge": "judge"})
    graph.add_edge("evaluate", END)
    graph.add_edge("judge", END)
    return graph.compile()


class LangGraphExecutor:
    """ScenarioExecutor backed by the evaluation graph."""

    name = "langgraph"
    # Capabilities checked at launch (see app/execution.py).
    supported_eval_types = SUPPORTED_EVAL_TYPES

    def __init__(
        self,
        models: ModelSource,
        *,
        model_timeout_seconds: float,
        default_judge_model: str | None = None,
        judge_output_retries: int = 0,
    ) -> None:
        self._models = models
        self._default_judge_model = default_judge_model
        self._graph = build_eval_graph(
            models,
            model_timeout_seconds,
            default_judge_model=default_judge_model,
            judge_output_retries=judge_output_retries,
        )

    def supports_model(self, model: str) -> bool:
        return self._models.supports(model)

    def resolve_judge_model(self, scenario: ScenarioSpec) -> str | None:
        """The judge model to freeze with the run at launch (None for non-judge scenarios)."""
        if scenario.eval_type != LLM_JUDGE:
            return None
        return resolve_judge_model(scenario.eval_config, self._default_judge_model)[0]

    def scenario_problems(self, scenario: ScenarioSpec) -> list[str]:
        """Launch-time check of what eval_config validation can't know: is a judge
        configured, and can this engine call it?"""
        if scenario.eval_type != LLM_JUDGE:
            return []
        judge_model, _ = resolve_judge_model(scenario.eval_config, self._default_judge_model)
        if judge_model is None:
            return ["judge_model is not set and the engine has no DEFAULT_JUDGE_MODEL"]
        if not self._models.supports(judge_model):
            return [f"judge_model {judge_model!r} is not supported by this engine (supported: OpenAI models)"]
        return []

    async def aclose(self) -> None:
        """Release the model clients (called on app shutdown)."""
        aclose = getattr(self._models, "aclose", None)
        if aclose is not None:
            await aclose()

    async def __call__(self, run: RunSpec, scenario: ScenarioSpec) -> ScenarioOutcome:
        if scenario.eval_type not in SUPPORTED_EVAL_TYPES:
            # Checked before the graph so no model call is spent on a result we can't evaluate.
            raise ExecutionError(
                lifecycle.UNSUPPORTED_EVAL_TYPE, f"No evaluator for eval_type {scenario.eval_type!r} yet"
            )
        final = await self._graph.ainvoke(
            {"agent": run.agent, "scenario": scenario, "judge_model": run.judge_models.get(scenario.id)}
        )
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
    executor = LangGraphExecutor(
        models,
        model_timeout_seconds=settings.model_timeout_seconds,
        default_judge_model=settings.default_judge_model,
        judge_output_retries=settings.judge_output_retries,
    )
    return executor, None

"""The interface between the worker and whatever actually executes a scenario.

The worker owns everything stateful: claiming runs, ordering scenarios, enforcing the
deadline, persisting results and setting the final status. An executor only answers
"given this agent and this scenario, what happened?". That keeps executors free of
database and lifecycle concerns. Milestone 2B plugs a LangGraph workflow in here.

Executor rules:

- It must be an `async` callable and must not block the event loop. The worker runs on
  the same event loop as the HTTP API, and timeouts work by cancelling the awaiting
  coroutine. Blocking work (sync SDKs, CPU-heavy evaluation) must go through
  `asyncio.to_thread` *and* carry its own timeout, because cancelling the await does
  not stop a thread. The worker discards any result that arrives after the run ended.
- A scenario the agent got wrong is a *result*: return `ScenarioOutcome(passed=False, ...)`.
  This includes a model error for that one scenario, if you want the run to continue.
- Raise `ExecutionError` to fail the whole run with a specific code (e.g. the provider
  rejected the API key). Any other exception fails the run with `EXECUTION_ERROR`.
  Results stored before the failure are kept.

Optional capabilities, checked by POST /runs so work the executor can't do is rejected
before it is stored (an executor without them accepts everything):

- `supported_eval_types: frozenset[str]`: unsupported types -> 422 INVALID_EVAL_CONFIG.
- `supports_model(model: str) -> bool`: unsupported model -> 400 INVALID_REQUEST.
- `scenario_problems(scenario) -> list[str]`: anything else the executor can't run for
  that scenario (e.g. no judge model configured) -> 422 INVALID_EVAL_CONFIG.
- `resolve_judge_model(scenario) -> str | None`: the judge model to freeze with the run.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import AgentSpec, ScenarioSpec, SuiteSpec


@dataclass(frozen=True)
class RunSpec:
    """Everything stored for a claimed run, as the executor sees it."""

    external_run_id: str
    run_id: str
    agent: AgentSpec
    suite: SuiteSpec
    timeout_seconds: int
    started_at: datetime
    scenarios: tuple[ScenarioSpec, ...]
    # llm_judge scenario_id -> judge model frozen at launch (absent for older runs).
    judge_models: Mapping[str, str] = field(default_factory=dict)


class ScenarioOutcome(BaseModel):
    """What happened for one scenario. Mirrors the contract's result fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    quality_score: float | None = Field(default=None, ge=0, le=1)
    latency_ms: int | None = Field(default=None, ge=0)
    actual_response: str | None = None
    failure_category: str | None = None
    failure_explanation: str | None = None
    evaluator_details: dict[str, Any] | None = None


class ScenarioExecutor(Protocol):
    async def __call__(self, run: RunSpec, scenario: ScenarioSpec) -> ScenarioOutcome: ...


class ExecutionError(Exception):
    """Raised by an executor to fail the whole run with a specific error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

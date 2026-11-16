"""Request/response models for the REST contract in docs/api-contract.md.

These mirror the zod schemas in src/lib/api/types.ts and src/lib/api/eval-config.ts.
Request envelopes ignore unknown fields (so the app can add fields without breaking the
engine); eval configs forbid them, matching the app's `.strict()` zod schemas.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
EvalType = Literal["keyword", "exact", "json_schema", "llm_judge"]
EVAL_TYPES: tuple[str, ...] = ("keyword", "exact", "json_schema", "llm_judge")

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# --- POST /runs request -----------------------------------------------------------


class _Request(BaseModel):
    # Strict types: "300" is not an int and "yes" is not a bool, as in the app's zod schemas.
    model_config = ConfigDict(strict=True)


class Message(_Request):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class AgentSpec(_Request):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    system_prompt: str = Field(max_length=20_000)
    model: NonEmptyStr


class SuiteSpec(_Request):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class ScenarioSpec(_Request):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input_messages: list[Message] = Field(min_length=1)
    # Nullable but required, like `z.string().nullable()` in the app.
    expected_response: str | None
    # Validated separately (see eval_config.py) so an unknown type or bad config
    # returns 422 INVALID_EVAL_CONFIG rather than 400 INVALID_REQUEST.
    eval_type: str
    eval_config: dict[str, Any]


class LaunchRequest(_Request):
    # The app always sends a UUID; keeping it as str avoids normalising its casing,
    # since it is also the idempotency key.
    run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F-]{36}$")]
    retry_of_run_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F-]{36}$")] | None
    # Same bounds the app enforces in launchInputSchema.
    timeout_seconds: int = Field(ge=10, le=3600)
    agent: AgentSpec
    suite: SuiteSpec
    scenarios: list[ScenarioSpec] = Field(min_length=1)

    @field_validator("scenarios")
    @classmethod
    def _unique_scenario_ids(cls, scenarios: list[ScenarioSpec]) -> list[ScenarioSpec]:
        # Results are keyed by (run, scenario_id), so ids must be unique within a run.
        seen: set[str] = set()
        for s in scenarios:
            if s.id in seen:
                raise ValueError(f"duplicate scenario id {s.id!r}")
            seen.add(s.id)
        return scenarios


# --- eval_config per eval_type --------------------------------------------------------


class _StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class KeywordConfig(_StrictConfig):
    required: list[NonEmptyStr] = Field(min_length=1)
    forbidden: list[NonEmptyStr] | None = None
    case_sensitive: bool | None = None


class ExactConfig(_StrictConfig):
    expected: str = Field(min_length=1)
    normalize_whitespace: bool | None = None
    case_sensitive: bool | None = None


class JsonSchemaConfig(_StrictConfig):
    schema_: dict[str, Any] = Field(alias="schema")


class LlmJudgeConfig(_StrictConfig):
    rubric: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10)]
    pass_threshold: float = Field(ge=0, le=1)
    judge_model: NonEmptyStr | None = None


# --- responses ------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class LaunchResponse(BaseModel):
    external_run_id: str
    # The contract (and the app's zod schema) only accept "queued" here: it means
    # "accepted", even when a repeated launch hits a run that has already moved on.
    status: Literal["queued"] = "queued"


class Progress(BaseModel):
    completed: int
    total: int


class ResultOut(BaseModel):
    scenario_id: str
    passed: bool
    quality_score: float | None = None
    latency_ms: int | None = None
    actual_response: str | None = None
    failure_category: str | None = None
    failure_explanation: str | None = None
    evaluator_details: dict[str, Any] | None = None


class RunError(BaseModel):
    code: str
    message: str


class RunStatusResponse(BaseModel):
    external_run_id: str
    status: RunStatus
    started_at: datetime | None
    completed_at: datetime | None
    progress: Progress
    results: list[ResultOut]
    error: RunError | None

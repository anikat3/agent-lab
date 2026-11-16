"""Validation of each scenario's eval_type / eval_config.

The app validates the same rules before launching (src/lib/api/eval-config.ts); the engine
checks again because it must never accept work it cannot run.
"""

from typing import Any

from jsonschema import SchemaError, validators
from pydantic import BaseModel, ValidationError

from app.errors import ApiError
from app.schemas import (
    EVAL_TYPES,
    ExactConfig,
    JsonSchemaConfig,
    KeywordConfig,
    LlmJudgeConfig,
    ScenarioSpec,
)

CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "keyword": KeywordConfig,
    "exact": ExactConfig,
    "json_schema": JsonSchemaConfig,
    "llm_judge": LlmJudgeConfig,
}


def config_problems(eval_type: str, config: dict[str, Any]) -> list[str]:
    """Return human-readable problems with one eval config (empty list if it is valid)."""
    model = CONFIG_MODELS.get(eval_type)
    if model is None:
        return [f"unknown eval_type {eval_type!r}; expected one of {', '.join(EVAL_TYPES)}"]
    try:
        parsed = model.model_validate(config)
    except ValidationError as e:
        return [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" if err["loc"] else err["msg"]
            for err in e.errors()
        ]
    if isinstance(parsed, JsonSchemaConfig):
        return _json_schema_problems(parsed.schema_)
    return []


def _json_schema_problems(schema: dict[str, Any]) -> list[str]:
    if not schema:
        return ["schema: must not be empty"]
    # Check against the meta-schema of whichever draft "$schema" names (latest by default).
    validator_cls = validators.validator_for(schema)
    try:
        validator_cls.check_schema(schema)
    except SchemaError as e:
        return [f"schema: {e.message}"]
    return []


def validate_scenario_configs(scenarios: list[ScenarioSpec]) -> None:
    """Raise 422 INVALID_EVAL_CONFIG listing every scenario whose config cannot be run."""
    invalid = []
    for s in scenarios:
        problems = config_problems(s.eval_type, s.eval_config)
        if problems:
            invalid.append({"scenario_id": s.id, "name": s.name, "problems": problems})
    if invalid:
        first = invalid[0]
        raise ApiError(
            422,
            "INVALID_EVAL_CONFIG",
            f'Scenario "{first["name"]}": {"; ".join(first["problems"])}',
            {"scenarios": invalid},
        )

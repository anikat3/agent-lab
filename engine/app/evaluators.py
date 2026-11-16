"""Deterministic evaluators: pure functions from (response, eval_config) to an Evaluation.

No I/O, no LLM. Each evaluator receives a config that already passed the launch-time
validation in app/eval_config.py, and parses it again into its typed model.

Failure metadata never repeats the response text: the full response is already stored
once as the result's actual_response, and copies in evaluator_details would spread it
further. Details describe *where* and *how* a response failed instead.
"""

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import validators
from jsonschema.exceptions import ValidationError

from app.schemas import ExactConfig, JsonSchemaConfig, KeywordConfig

# failure_category values. The dashboard groups failures by these, so keep them stable.
KEYWORD_MISSING = "keyword_missing"
FORBIDDEN_CONTENT = "forbidden_content"
EXACT_MISMATCH = "exact_mismatch"
INVALID_JSON = "invalid_json"
SCHEMA_VIOLATION = "schema_violation"


@dataclass(frozen=True)
class Evaluation:
    passed: bool
    quality_score: float
    failure_category: str | None = None
    failure_explanation: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def evaluate_keywords(response: str, config: KeywordConfig) -> Evaluation:
    """Pass iff every required keyword appears in the response and no forbidden one does.

    Matching is plain substring containment ("30 days" matches "within 30 days."), using
    str.casefold() unless case_sensitive is true. Score is binary: 1.0 pass, 0.0 fail.
    """
    case_sensitive = bool(config.case_sensitive)
    forbidden_terms = config.forbidden or []

    def norm(text: str) -> str:
        return text if case_sensitive else text.casefold()

    haystack = norm(response)
    missing = [k for k in config.required if norm(k) not in haystack]
    forbidden_found = [k for k in forbidden_terms if norm(k) in haystack]
    details: dict[str, Any] = {
        "evaluator": "keyword",
        "case_sensitive": case_sensitive,
        "required": list(config.required),
        "forbidden": list(forbidden_terms),
        "missing_required": missing,
        "found_forbidden": forbidden_found,
    }

    if not missing and not forbidden_found:
        return Evaluation(passed=True, quality_score=1.0, details=details)

    problems = []
    if forbidden_found:
        problems.append(f"contains forbidden {_plural('keyword', forbidden_found)} {_quote(forbidden_found)}")
    if missing:
        problems.append(f"missing required {_plural('keyword', missing)} {_quote(missing)}")
    return Evaluation(
        passed=False,
        quality_score=0.0,
        # Forbidden content wins when both apply: saying something prohibited is the more
        # serious failure. Both problems are still listed in the explanation and details.
        failure_category=FORBIDDEN_CONTENT if forbidden_found else KEYWORD_MISSING,
        failure_explanation="Response " + " and ".join(problems)
        + (" (case-sensitive)" if case_sensitive else "") + ".",
        details=details,
    )


def _plural(word: str, items: list[str]) -> str:
    return word if len(items) == 1 else word + "s"


def _quote(items: list[str]) -> str:
    return ", ".join(repr(i) for i in items)


# --- exact match ---------------------------------------------------------------------------


def _exact_form(text: str, *, case_sensitive: bool, normalize_whitespace: bool) -> str:
    """The form both sides are compared in.

    - Unicode NFC always: "é" typed as one code point or as "e" + combining accent is the
      same character, and should not count as a mismatch.
    - normalize_whitespace: any run of Unicode whitespace (spaces, tabs, newlines, NBSP)
      becomes one space, and leading/trailing whitespace is dropped.
    - case-insensitive: str.casefold(), which also folds e.g. "ß" to "ss".
    Punctuation is never touched.
    """
    text = unicodedata.normalize("NFC", text)
    if normalize_whitespace:
        text = " ".join(text.split())
    if not case_sensitive:
        text = text.casefold()
    return text


def evaluate_exact(response: str, config: ExactConfig) -> Evaluation:
    """Pass iff the response equals config.expected after the configured normalisation.

    Defaults: case-insensitive (like keyword), whitespace significant. Score is binary.
    """
    case_sensitive = bool(config.case_sensitive)
    normalize_whitespace = bool(config.normalize_whitespace)
    actual = _exact_form(response, case_sensitive=case_sensitive, normalize_whitespace=normalize_whitespace)
    expected = _exact_form(config.expected, case_sensitive=case_sensitive, normalize_whitespace=normalize_whitespace)
    details: dict[str, Any] = {
        "evaluator": "exact",
        "case_sensitive": case_sensitive,
        "normalize_whitespace": normalize_whitespace,
        "compared_length": {"response": len(actual), "expected": len(expected)},
    }
    if actual == expected:
        return Evaluation(passed=True, quality_score=1.0, details=details)

    index = next((i for i, (a, b) in enumerate(zip(actual, expected)) if a != b), min(len(actual), len(expected)))
    line = actual.count("\n", 0, index) + 1
    column = index - (actual.rfind("\n", 0, index) + 1) + 1
    details["first_difference"] = {"index": index, "line": line, "column": column}
    # Hints for the two most common near-misses, computed without exposing either text.
    differs_only_by_case = case_sensitive and _exact_form(
        response, case_sensitive=False, normalize_whitespace=normalize_whitespace
    ) == _exact_form(config.expected, case_sensitive=False, normalize_whitespace=normalize_whitespace)
    differs_only_by_whitespace = not normalize_whitespace and _exact_form(
        response, case_sensitive=case_sensitive, normalize_whitespace=True
    ) == _exact_form(config.expected, case_sensitive=case_sensitive, normalize_whitespace=True)
    details["differs_only_by_case"] = differs_only_by_case
    details["differs_only_by_whitespace"] = differs_only_by_whitespace

    if not actual:
        explanation = "Response is empty; expected exact text."
    else:
        where = f"line {line}, column {column}" if "\n" in actual or "\n" in expected else f"character {index + 1}"
        explanation = (
            f"Response does not match the expected text: first difference at {where} "
            f"(response {len(actual)} chars, expected {len(expected)})."
        )
        if differs_only_by_case:
            explanation += " The texts differ only in letter case."
        elif differs_only_by_whitespace:
            explanation += " The texts differ only in whitespace (set normalize_whitespace to ignore it)."
    return Evaluation(
        passed=False,
        quality_score=0.0,
        failure_category=EXACT_MISMATCH,
        failure_explanation=explanation,
        details=details,
    )


# --- JSON Schema -----------------------------------------------------------------------------

MAX_REPORTED_ERRORS = 10


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise _DuplicateKeyError(f"duplicate key(s) {', '.join(repr(d) for d in duplicates)}")
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_json_strictly(text: str) -> Any:
    """RFC 8259 JSON only: no NaN/Infinity, no duplicate keys, no repairs, no fence stripping.

    Surrounding whitespace is allowed (it is insignificant in JSON). Raises ValueError.
    """
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)


def _json_path(path: Any) -> str:
    out = "$"
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _describe(error: ValidationError) -> str:
    """A message built from the schema and the value's *type* only.

    jsonschema's own messages quote the offending value ("'my secret' is not of type
    'number'"), which would copy response content into the metadata.
    """
    kind, expected = error.validator, error.validator_value
    if kind == "required":
        return error.message  # "'status' is a required property": the name comes from the schema
    if kind == "type":
        return f"expected type {expected!r}, got {_json_type(error.instance)}"
    if kind == "additionalProperties" and isinstance(error.instance, dict):
        allowed = set(error.schema.get("properties", {}))
        patterns = [re.compile(p) for p in error.schema.get("patternProperties", {})]
        extra = sorted(k for k in error.instance if k not in allowed and not any(p.search(k) for p in patterns))
        return f"unexpected propert{'y' if len(extra) == 1 else 'ies'} {', '.join(repr(k) for k in extra)}"
    if kind in ("enum", "const"):
        return f"value is not allowed by {kind} {expected!r}"
    return f"fails {kind}: {expected!r}"


def evaluate_json_schema(response: str, config: JsonSchemaConfig) -> Evaluation:
    """Pass iff the response is strict JSON that validates against config.schema.

    The draft is taken from the schema's "$schema" (latest supported draft by default).
    "format" is an annotation and not asserted, as in the JSON Schema spec's default.
    """
    schema = config.schema_
    if not response.strip():
        return Evaluation(
            passed=False,
            quality_score=0.0,
            failure_category=INVALID_JSON,
            failure_explanation="Response is empty; expected a JSON document.",
            details={"evaluator": "json_schema", "valid_json": False, "parse_error": {"message": "empty response"}},
        )
    try:
        instance = parse_json_strictly(response)
    except json.JSONDecodeError as e:
        hint = (
            " It appears to be wrapped in a Markdown code fence; the response must be bare JSON."
            if response.lstrip().startswith("```")
            else ""
        )
        return Evaluation(
            passed=False,
            quality_score=0.0,
            failure_category=INVALID_JSON,
            failure_explanation=f"Response is not valid JSON: {e.msg} at line {e.lineno}, column {e.colno}.{hint}",
            details={
                "evaluator": "json_schema",
                "valid_json": False,
                "parse_error": {"message": e.msg, "line": e.lineno, "column": e.colno, "position": e.pos},
            },
        )
    except RecursionError:
        return Evaluation(
            passed=False,
            quality_score=0.0,
            failure_category=INVALID_JSON,
            failure_explanation="Response JSON is nested too deeply to parse.",
            details={"evaluator": "json_schema", "valid_json": False, "parse_error": {"message": "nested too deeply"}},
        )
    except ValueError as e:  # duplicate keys, NaN/Infinity
        return Evaluation(
            passed=False,
            quality_score=0.0,
            failure_category=INVALID_JSON,
            failure_explanation=f"Response is not valid JSON: {e}.",
            details={"evaluator": "json_schema", "valid_json": False, "parse_error": {"message": str(e)}},
        )

    validator = validators.validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: (list(map(str, e.absolute_path)), e.validator))
    details: dict[str, Any] = {
        "evaluator": "json_schema",
        "valid_json": True,
        "root_type": _json_type(instance),
        "error_count": len(errors),
    }
    if not errors:
        return Evaluation(passed=True, quality_score=1.0, details=details)

    reported = [
        {
            "path": _json_path(e.absolute_path),
            "keyword": e.validator,
            "message": _describe(e),
            "schema_path": "/".join(str(p) for p in e.absolute_schema_path),
        }
        for e in errors[:MAX_REPORTED_ERRORS]
    ]
    details["errors"] = reported
    details["errors_truncated"] = len(errors) > MAX_REPORTED_ERRORS
    first = reported[0]
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return Evaluation(
        passed=False,
        quality_score=0.0,
        failure_category=SCHEMA_VIOLATION,
        failure_explanation=f"Response JSON violates the schema at {first['path']}: {first['message']}{more}.",
        details=details,
    )


# --- registry --------------------------------------------------------------------------------


def _keyword(response: str, config: dict[str, Any]) -> Evaluation:
    return evaluate_keywords(response, KeywordConfig.model_validate(config))


def _exact(response: str, config: dict[str, Any]) -> Evaluation:
    return evaluate_exact(response, ExactConfig.model_validate(config))


def _json_schema(response: str, config: dict[str, Any]) -> Evaluation:
    return evaluate_json_schema(response, JsonSchemaConfig.model_validate(config))


# eval_type -> evaluator. The engine advertises (and accepts at launch) exactly these.
# llm_judge arrives in Milestone 2D.
EVALUATORS: dict[str, Callable[[str, dict[str, Any]], Evaluation]] = {
    "keyword": _keyword,
    "exact": _exact,
    "json_schema": _json_schema,
}

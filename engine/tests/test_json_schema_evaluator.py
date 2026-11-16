"""JSON Schema evaluator (pure function) and launch-time rejection of invalid schemas."""

import json

import pytest

from app.evaluators import INVALID_JSON, MAX_REPORTED_ERRORS, SCHEMA_VIOLATION, evaluate_json_schema
from app.schemas import JsonSchemaConfig

from .conftest import assert_error, keyword_payload, launch

REFUND_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "refund_amount": {"type": "number"}},
    "required": ["status", "refund_amount"],
}


def check(response: str, schema: dict = REFUND_SCHEMA):  # type: ignore[no-untyped-def,type-arg]
    return evaluate_json_schema(response, JsonSchemaConfig.model_validate({"schema": schema}))


# --- valid -----------------------------------------------------------------------------------


def test_valid_json_passes():
    ev = check('{"status": "approved", "refund_amount": 42.5}')
    assert ev.passed is True
    assert ev.quality_score == 1.0
    assert ev.failure_category is None
    assert ev.details == {"evaluator": "json_schema", "valid_json": True, "root_type": "object", "error_count": 0}


def test_surrounding_whitespace_is_fine():
    assert check('\n  {"status": "approved", "refund_amount": 1}\n').passed  # insignificant in JSON


def test_integer_satisfies_number():
    assert check('{"status": "ok", "refund_amount": 10}').passed


# --- invalid JSON ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response,needle",
    [
        ('{"status": "ok", "refund_amount": 1', "Expecting"),
        ("{'status': 'ok'}", "Expecting property name"),
        ('{"status": "ok",}', "Illegal trailing comma"),
        ("Sure! Here is the JSON: {}", "Expecting value"),
        ('{"status": "ok"} trailing text', "Extra data"),
    ],
    ids=["truncated", "single-quotes", "trailing-comma", "prose-prefix", "extra-data"],
)
def test_invalid_json_syntax(response, needle):
    ev = check(response)
    assert ev.passed is False
    assert ev.quality_score == 0.0
    assert ev.failure_category == INVALID_JSON
    assert needle in ev.failure_explanation
    assert ev.details["valid_json"] is False
    assert {"message", "line", "column", "position"} <= set(ev.details["parse_error"])


def test_markdown_fence_is_not_stripped_but_explained():
    ev = check('```json\n{"status": "ok", "refund_amount": 1}\n```')
    assert ev.failure_category == INVALID_JSON
    assert "wrapped in a Markdown code fence" in ev.failure_explanation


def test_empty_response_is_invalid_json():
    ev = check("   ")
    assert ev.failure_category == INVALID_JSON
    assert ev.failure_explanation == "Response is empty; expected a JSON document."


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_constants_are_rejected(constant):
    ev = check(f'{{"status": "ok", "refund_amount": {constant}}}')
    assert ev.failure_category == INVALID_JSON
    assert f"{constant} is not valid JSON" in ev.failure_explanation


def test_duplicate_keys_are_rejected_not_silently_merged():
    ev = check('{"status": "ok", "refund_amount": 1, "status": "denied"}')
    assert ev.failure_category == INVALID_JSON
    assert "duplicate key(s) 'status'" in ev.failure_explanation


def test_absurdly_deep_nesting_is_invalid_json_not_a_crash():
    ev = check("[" * 100_000 + "]" * 100_000, {"type": "array"})
    assert ev.failure_category == INVALID_JSON


# --- schema violations -----------------------------------------------------------------------


def test_missing_required_property():
    ev = check('{"status": "approved"}')
    assert ev.passed is False
    assert ev.failure_category == SCHEMA_VIOLATION
    assert ev.failure_explanation == "Response JSON violates the schema at $: 'refund_amount' is a required property."
    assert ev.details["errors"] == [
        {"path": "$", "keyword": "required", "message": "'refund_amount' is a required property", "schema_path": "required"}
    ]


def test_wrong_property_type():
    ev = check('{"status": "approved", "refund_amount": "forty"}')
    assert ev.failure_explanation == "Response JSON violates the schema at $.refund_amount: expected type 'number', got string."
    assert ev.details["errors"][0]["schema_path"] == "properties/refund_amount/type"


def test_wrong_root_type():
    ev = check('["approved", 42]')
    assert ev.details["root_type"] == "array"
    assert ev.details["errors"][0]["message"] == "expected type 'object', got array"


def test_all_errors_are_reported_with_a_count():
    ev = check('{"refund_amount": "lots"}')
    assert ev.details["error_count"] == 2
    assert {e["keyword"] for e in ev.details["errors"]} == {"required", "type"}
    assert "(+1 more)" in ev.failure_explanation


NESTED = {
    "type": "object",
    "properties": {
        "order": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": "^ORD-[0-9]+$"},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}, "qty": {"type": "integer", "minimum": 1, "maximum": 10}},
                        "required": ["sku", "qty"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["id", "items"],
        }
    },
    "required": ["order"],
}


def test_nested_objects_and_arrays_pass():
    doc = {"order": {"id": "ORD-7", "items": [{"sku": "A1", "qty": 2}, {"sku": "B2", "qty": 10}]}}
    assert check(json.dumps(doc), NESTED).passed


@pytest.mark.parametrize(
    "items,path,keyword,message",
    [
        ([{"sku": "A1", "qty": 0}], "$.order.items[0].qty", "minimum", "fails minimum: 1"),
        ([{"sku": "A1", "qty": 11}], "$.order.items[0].qty", "maximum", "fails maximum: 10"),
        ([{"sku": "A1", "qty": 1.5}], "$.order.items[0].qty", "type", "expected type 'integer', got number"),
        ([{"sku": "A1"}], "$.order.items[0]", "required", "'qty' is a required property"),
        ([], "$.order.items", "minItems", "fails minItems: 1"),
        (
            [{"sku": "A1", "qty": 1, "gift": True, "note": "x"}],
            "$.order.items[0]",
            "additionalProperties",
            "unexpected properties 'gift', 'note'",
        ),
    ],
    ids=["minimum", "maximum", "integer-type", "nested-required", "minItems", "additionalProperties"],
)
def test_nested_violations_report_precise_paths(items, path, keyword, message):
    ev = check(json.dumps({"order": {"id": "ORD-7", "items": items}}), NESTED)
    assert ev.failure_category == SCHEMA_VIOLATION
    [error] = ev.details["errors"]
    assert (error["path"], error["keyword"], error["message"]) == (path, keyword, message)


def test_pattern_violation():
    ev = check(json.dumps({"order": {"id": "7", "items": [{"sku": "A", "qty": 1}]}}), NESTED)
    assert ev.details["errors"][0] == {
        "path": "$.order.id",
        "keyword": "pattern",
        "message": "fails pattern: '^ORD-[0-9]+$'",
        "schema_path": "properties/order/properties/id/pattern",
    }


def test_additional_properties_respects_pattern_properties():
    schema = {"type": "object", "patternProperties": {"^x-": {}}, "additionalProperties": False}
    assert check('{"x-trace": 1}', schema).passed
    ev = check('{"x-trace": 1, "other": 2}', schema)
    assert ev.details["errors"][0]["message"] == "unexpected property 'other'"


def test_enum_and_const():
    schema = {"type": "object", "properties": {"status": {"enum": ["approved", "denied"]}, "v": {"const": 1}}}
    ev = check('{"status": "maybe", "v": 2}', schema)
    assert [e["message"] for e in ev.details["errors"]] == [
        "value is not allowed by enum ['approved', 'denied']",
        "value is not allowed by const 1",
    ]


def test_reported_errors_are_capped():
    schema = {"type": "array", "items": {"type": "integer"}}
    ev = check(json.dumps(["x"] * 25), schema)
    assert ev.details["error_count"] == 25
    assert len(ev.details["errors"]) == MAX_REPORTED_ERRORS
    assert ev.details["errors_truncated"] is True


def test_schema_draft_comes_from_dollar_schema():
    # Draft 4 uses exclusiveMaximum as a boolean modifier; the latest drafts use a number.
    draft4 = {"$schema": "http://json-schema.org/draft-04/schema#", "type": "number", "maximum": 5, "exclusiveMaximum": True}
    assert not check("5", draft4).passed
    assert check("4.9", draft4).passed


def test_violation_metadata_does_not_copy_response_values():
    ev = check('{"status": 4111111111111111, "refund_amount": "Jane Doe SSN 123-45-6789"}')
    blob = repr(ev.details) + ev.failure_explanation
    for fragment in ("4111", "Jane", "123-45"):
        assert fragment not in blob


# --- invalid schemas are rejected at launch (nothing stored, nothing executed) ---------------


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "banana"},
        {"type": "object", "required": "status"},
        {"type": "object", "properties": []},
        {"type": "number", "minimum": "ten"},
        {"type": "object", "additionalProperties": "no"},
        {"type": "array", "items": {"type": "strng"}},
    ],
    ids=["empty", "bad-type", "required-not-array", "properties-not-object", "minimum-not-number",
         "additionalProperties-not-schema", "nested-bad-type"],
)
def test_invalid_schema_is_rejected_at_launch(client, schema):
    payload = keyword_payload({"id": "k", "user": "hi", "required": ["x"]})
    payload["scenarios"].append(
        {
            "id": "j",
            "name": "JSON reply",
            "input_messages": [{"role": "user", "content": "Reply in JSON"}],
            "expected_response": None,
            "eval_type": "json_schema",
            "eval_config": {"schema": schema},
        }
    )
    err = assert_error(launch(client, payload), 422, "INVALID_EVAL_CONFIG")
    [bad] = err["details"]["scenarios"]
    assert bad["scenario_id"] == "j"
    assert bad["problems"][0].startswith("schema:")

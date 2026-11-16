import pytest
from sqlalchemy import func, select

from app.models import Run

from .conftest import AUTH, assert_error, launch


def _no_runs(db):
    assert db.scalar(select(func.count()).select_from(Run)) == 0


# --- 400 INVALID_REQUEST: the request envelope is malformed -----------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("agent"),
        lambda p: p.pop("retry_of_run_id"),  # nullable but required
        lambda p: p.update(run_id="not-a-uuid"),
        lambda p: p.update(timeout_seconds=5),
        lambda p: p.update(timeout_seconds=3601),
        lambda p: p.update(timeout_seconds="300s"),
        lambda p: p.update(scenarios=[]),
        lambda p: p["agent"].update(model="   "),
        lambda p: p["scenarios"][0].update(input_messages=[]),
        lambda p: p["scenarios"][0]["input_messages"][0].update(role="tool"),
        lambda p: p["scenarios"][0]["input_messages"][0].update(content=""),
        lambda p: p["scenarios"][0].pop("expected_response"),
        lambda p: p["scenarios"][1].update(id=p["scenarios"][0]["id"]),
        lambda p: p["scenarios"][0].update(eval_config=["not", "an", "object"]),
    ],
    ids=[
        "missing-agent",
        "missing-retry_of_run_id",
        "bad-run-id",
        "timeout-too-small",
        "timeout-too-large",
        "timeout-not-int",
        "no-scenarios",
        "blank-model",
        "no-messages",
        "bad-role",
        "empty-content",
        "missing-expected_response",
        "duplicate-scenario-ids",
        "eval_config-not-object",
    ],
)
def test_malformed_request_is_400(client, db, payload, mutate):
    mutate(payload)
    err = assert_error(launch(client, payload), 400, "INVALID_REQUEST")
    assert err["details"]["issues"]
    _no_runs(db)


def test_validation_errors_do_not_echo_payload_content(client, payload):
    payload["scenarios"][0]["input_messages"][0] = {"role": "tool", "content": "SECRET-PROMPT"}
    resp = launch(client, payload)
    assert resp.status_code == 400
    assert "SECRET-PROMPT" not in resp.text


def test_invalid_json_body_is_400(client, payload):
    resp = client.post(
        "/runs",
        content=b"{not json",
        headers={**AUTH, "content-type": "application/json", "Idempotency-Key": payload["run_id"]},
    )
    assert_error(resp, 400, "INVALID_REQUEST")


# --- 422 INVALID_EVAL_CONFIG: the envelope is fine but a scenario can't be run --------------


@pytest.mark.parametrize(
    "eval_type,config",
    [
        ("regex", {"pattern": ".*"}),
        ("keyword", {}),
        ("keyword", {"required": []}),
        ("keyword", {"required": ["  "]}),
        ("keyword", {"required": ["ok"], "unexpected": True}),
        ("exact", {"expected": ""}),
        ("exact", {"expected": "hi", "case_sensitive": "yes"}),
        ("json_schema", {}),
        ("json_schema", {"schema": {}}),
        ("json_schema", {"schema": {"type": "banana"}}),
        ("json_schema", {"schema": {"type": "object", "required": "status"}}),
        ("llm_judge", {"rubric": "too short", "pass_threshold": 0.5}),
        ("llm_judge", {"rubric": "Is the answer polite and correct?", "pass_threshold": 1.5}),
        ("llm_judge", {"rubric": "Is the answer polite and correct?"}),
    ],
    ids=[
        "unknown-type",
        "keyword-missing-required",
        "keyword-empty-required",
        "keyword-blank-keyword",
        "keyword-extra-key",
        "exact-empty",
        "exact-bad-flag",
        "json_schema-missing-schema",
        "json_schema-empty-schema",
        "json_schema-bad-type",
        "json_schema-bad-required",
        "llm_judge-short-rubric",
        "llm_judge-threshold-out-of-range",
        "llm_judge-missing-threshold",
    ],
)
def test_unrunnable_eval_config_is_422(client, db, payload, eval_type, config):
    payload["scenarios"][1].update(eval_type=eval_type, eval_config=config)
    err = assert_error(launch(client, payload), 422, "INVALID_EVAL_CONFIG")
    [bad] = err["details"]["scenarios"]
    assert bad["scenario_id"] == payload["scenarios"][1]["id"]
    assert bad["problems"]
    _no_runs(db)


def test_all_four_eval_types_are_accepted(client, payload):
    base = payload["scenarios"][0]
    configs = {
        "keyword": {"required": ["30 days"]},
        "exact": {"expected": "Refunds within 30 days.", "normalize_whitespace": True},
        "json_schema": {"schema": {"type": "object"}},
        "llm_judge": {"rubric": "Mentions the 30 day window.", "pass_threshold": 0.7, "judge_model": "gpt-4o"},
    }
    payload["scenarios"] = [
        {**base, "id": f"s-{t}", "eval_type": t, "eval_config": c} for t, c in configs.items()
    ]
    assert launch(client, payload).status_code == 202

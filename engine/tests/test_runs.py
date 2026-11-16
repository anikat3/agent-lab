from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Run, RunScenario, ScenarioResult

from .conftest import AUTH, assert_error, launch


def test_launch_returns_202_and_stores_run(client, db, payload):
    resp = launch(client, payload)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["external_run_id"].startswith("run_")

    run = db.scalars(select(Run)).one()
    assert run.external_run_id == body["external_run_id"]
    assert run.run_id == payload["run_id"]
    assert run.status == "queued"
    assert run.timeout_seconds == 300
    assert run.agent == payload["agent"]
    assert run.suite == payload["suite"]
    assert run.started_at is None and run.completed_at is None
    assert run.created_at.tzinfo is not None

    scenarios = db.scalars(select(RunScenario).order_by(RunScenario.position)).all()
    assert [s.scenario_id for s in scenarios] == [s["id"] for s in payload["scenarios"]]
    first, second = scenarios
    assert first.input_messages == payload["scenarios"][0]["input_messages"]
    assert first.eval_config == payload["scenarios"][0]["eval_config"]
    assert second.expected_response is None
    assert second.eval_type == "json_schema"


def test_get_run_reports_queued_with_no_results(client, payload):
    ext_id = launch(client, payload).json()["external_run_id"]
    resp = client.get(f"/runs/{ext_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "external_run_id": ext_id,
        "status": "queued",
        "started_at": None,
        "completed_at": None,
        "progress": {"completed": 0, "total": 2},
        "results": [],
        "error": None,
    }


def test_get_unknown_run_is_404(client):
    assert_error(client.get("/runs/run_doesnotexist", headers=AUTH), 404, "RUN_NOT_FOUND")


def test_get_run_reflects_rows_written_by_a_worker(client, db, payload):
    """The read path the Milestone 2 worker will rely on: rows written directly to the
    database (as the worker will) show up in progress, results, status and error."""
    ext_id = launch(client, payload).json()["external_run_id"]
    run = db.get(Run, ext_id)
    started = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    run.status = "failed"
    run.started_at = started
    run.completed_at = started + timedelta(seconds=5)
    run.error_code = "RUN_TIMEOUT"
    run.error_message = "Run exceeded 300s"
    db.add(
        ScenarioResult(
            external_run_id=ext_id,
            scenario_id=payload["scenarios"][0]["id"],
            passed=False,
            quality_score=0.0,
            latency_ms=1234,
            actual_response="We do not offer refunds.",
            failure_category="keyword_missing",
            failure_explanation="Missing required keyword '30 days'",
            evaluator_details={"missing": ["30 days"]},
        )
    )
    db.commit()

    body = client.get(f"/runs/{ext_id}", headers=AUTH).json()
    assert body["status"] == "failed"
    assert body["progress"] == {"completed": 1, "total": 2}
    assert body["error"] == {"code": "RUN_TIMEOUT", "message": "Run exceeded 300s"}
    assert body["started_at"] == "2026-11-16T12:00:00Z"
    assert body["completed_at"] == "2026-11-16T12:00:05Z"
    [result] = body["results"]
    assert result == {
        "scenario_id": payload["scenarios"][0]["id"],
        "passed": False,
        "quality_score": 0.0,
        "latency_ms": 1234,
        "actual_response": "We do not offer refunds.",
        "failure_category": "keyword_missing",
        "failure_explanation": "Missing required keyword '30 days'",
        "evaluator_details": {"missing": ["30 days"]},
    }


def test_timestamps_are_stored_and_returned_as_utc(client, db, payload):
    ext_id = launch(client, payload).json()["external_run_id"]
    run = db.get(Run, ext_id)
    run.started_at = datetime(2026, 11, 16, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    db.commit()
    db.expire_all()
    assert db.get(Run, ext_id).started_at == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert client.get(f"/runs/{ext_id}", headers=AUTH).json()["started_at"] == "2026-11-16T12:00:00Z"

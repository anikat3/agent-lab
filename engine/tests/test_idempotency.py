from sqlalchemy import func, select

from app import repository
from app.models import Run, RunScenario
from app.schemas import LaunchRequest

from .conftest import AUTH, assert_error, launch


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_repeated_launch_returns_same_external_run_id(client, db, payload):
    first = launch(client, payload)
    second = launch(client, payload)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert _count(db, Run) == 1
    assert _count(db, RunScenario) == 2


def test_repeat_still_returns_queued_after_run_has_moved_on(client, db, payload):
    """The launch response may only say "queued" (it means "accepted"), even on replay."""
    ext_id = launch(client, payload).json()["external_run_id"]
    db.get(Run, ext_id).status = "running"
    db.commit()
    assert launch(client, payload).json() == {"external_run_id": ext_id, "status": "queued"}


def test_different_run_ids_create_different_runs(client, db, payload):
    a = launch(client, payload).json()["external_run_id"]
    payload["run_id"] = "11111111-2222-4333-8444-555555555555"
    b = launch(client, payload).json()["external_run_id"]
    assert a != b
    assert _count(db, Run) == 2


def test_same_run_id_with_different_payload_is_409(client, db, payload):
    launch(client, payload)
    payload["timeout_seconds"] = 600
    assert_error(launch(client, payload), 409, "DUPLICATE_RUN")
    assert db.scalars(select(Run)).one().timeout_seconds == 300


def test_unknown_extra_fields_do_not_change_the_fingerprint(client, payload):
    first = launch(client, payload).json()
    payload["some_future_field"] = "ignored"
    assert launch(client, payload).json() == first


def test_missing_idempotency_header_falls_back_to_run_id(client, payload):
    first = client.post("/runs", json=payload, headers=AUTH)
    second = launch(client, payload)
    assert first.status_code == 202
    assert first.json() == second.json()


def test_idempotency_key_must_match_run_id(client, db, payload):
    resp = launch(client, payload, **{"Idempotency-Key": "11111111-2222-4333-8444-555555555555"})
    assert_error(resp, 400, "INVALID_REQUEST")
    assert _count(db, Run) == 0


def test_concurrent_insert_race_replays_the_winner(client, db, payload, monkeypatch):
    """Simulate two requests that both pass the "not found" check before either commits:
    the unique index rejects the loser, which then returns the winner's run."""
    winner = launch(client, payload).json()["external_run_id"]

    real_find = repository._find_by_run_id
    calls = {"n": 0}

    def find_misses_first_time(session, run_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(session, run_id)

    monkeypatch.setattr(repository, "_find_by_run_id", find_misses_first_time)
    outcome = repository.create_or_get_run(db, LaunchRequest.model_validate(payload))
    assert outcome.created is False
    assert outcome.run.external_run_id == winner
    assert _count(db, Run) == 1

"""RunStore: atomic claiming, fenced writes, terminal-state protection and recovery."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app import lifecycle as lc
from app.execution import ScenarioOutcome
from app.models import Run, ScenarioResult
from app.run_store import RunNotOwned

from .conftest import queue_run

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
LEASE = 30.0
PASS = ScenarioOutcome(passed=True, quality_score=1.0, latency_ms=10, actual_response="ok")


def _run(db, ext_id) -> Run:
    db.expire_all()
    return db.get(Run, ext_id)


def _results(db, ext_id) -> list[ScenarioResult]:
    db.expire_all()
    return list(db.scalars(select(ScenarioResult).where(ScenarioResult.external_run_id == ext_id)))


# --- claiming -----------------------------------------------------------------------


def test_claim_moves_queued_to_running_and_returns_spec(client, store, db):
    ext_id = queue_run(client)
    spec = store.claim_next("w1", T0, LEASE)

    assert spec.external_run_id == ext_id
    assert spec.started_at == T0
    assert spec.agent.model == "gpt-4o-mini"
    assert [s.id for s in spec.scenarios] == [
        "a1a1a1a1-0000-4000-8000-000000000001",
        "a1a1a1a1-0000-4000-8000-000000000002",
    ]
    assert spec.scenarios[0].input_messages[0].content == "What is your refund policy?"

    run = _run(db, ext_id)
    assert run.status == lc.RUNNING
    assert run.worker_id == "w1"
    assert run.started_at == T0
    assert run.lease_expires_at == T0 + timedelta(seconds=LEASE)
    assert run.completed_at is None


def test_claim_returns_none_when_nothing_queued(store):
    assert store.claim_next("w1", T0, LEASE) is None


def test_a_claimed_run_cannot_be_claimed_again(client, store):
    queue_run(client)
    assert store.claim_next("w1", T0, LEASE) is not None
    assert store.claim_next("w2", T0, LEASE) is None


def test_claims_oldest_first(client, store, db):
    first, second = queue_run(client), queue_run(client)
    db.execute(update(Run).where(Run.external_run_id == first).values(created_at=T0))
    db.execute(update(Run).where(Run.external_run_id == second).values(created_at=T0 - timedelta(minutes=1)))
    db.commit()
    assert store.claim_next("w1", T0, LEASE).external_run_id == second
    assert store.claim_next("w1", T0, LEASE).external_run_id == first


@pytest.mark.parametrize("terminal", [lc.COMPLETED, lc.FAILED, lc.CANCELLED])
def test_terminal_runs_are_never_claimed(client, store, db, terminal):
    ext_id = queue_run(client)
    db.execute(update(Run).where(Run.external_run_id == ext_id).values(status=terminal))
    db.commit()
    assert store.claim_next("w1", T0, LEASE) is None
    assert _run(db, ext_id).status == terminal


def test_many_threads_racing_for_one_run_only_one_wins(client, store, db):
    ext_id = queue_run(client)
    barrier = threading.Barrier(8)
    wins: list[str] = []

    def contend(i: int) -> None:
        barrier.wait()
        if store.claim_next(f"w{i}", T0, LEASE) is not None:
            wins.append(f"w{i}")

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1
    assert _run(db, ext_id).worker_id == wins[0]


def test_concurrent_workers_claim_each_run_exactly_once(client, store, db):
    ids = {queue_run(client) for _ in range(20)}
    barrier = threading.Barrier(4)
    claimed: dict[str, list[str]] = {f"w{i}": [] for i in range(4)}

    def drain(worker_id: str) -> None:
        barrier.wait()
        while (spec := store.claim_next(worker_id, T0, LEASE)) is not None:
            claimed[worker_id].append(spec.external_run_id)

    threads = [threading.Thread(target=drain, args=(w,)) for w in claimed]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_claims = [ext for claims in claimed.values() for ext in claims]
    assert sorted(all_claims) == sorted(ids)  # every run claimed, none twice
    for worker_id, claims in claimed.items():
        for ext in claims:
            assert _run(db, ext).worker_id == worker_id


# --- leases and fenced writes -------------------------------------------------------


def test_renew_lease_only_for_owner(client, store, db):
    ext_id = queue_run(client)
    store.claim_next("w1", T0, LEASE)
    later = T0 + timedelta(seconds=10)
    assert store.renew_lease(ext_id, "w1", later, LEASE) is True
    assert _run(db, ext_id).lease_expires_at == later + timedelta(seconds=LEASE)
    assert store.renew_lease(ext_id, "w2", later, LEASE) is False


def test_record_result_writes_one_row_per_scenario_even_if_repeated(client, store, db):
    ext_id = queue_run(client)
    spec = store.claim_next("w1", T0, LEASE)
    sid = spec.scenarios[0].id
    store.record_result(ext_id, "w1", sid, PASS, T0, LEASE)
    store.record_result(ext_id, "w1", sid, PASS.model_copy(update={"latency_ms": 99}), T0, LEASE)
    [row] = _results(db, ext_id)
    assert row.latency_ms == 99


def test_record_result_renews_the_lease(client, store, db):
    ext_id = queue_run(client)
    spec = store.claim_next("w1", T0, LEASE)
    later = T0 + timedelta(seconds=20)
    store.record_result(ext_id, "w1", spec.scenarios[0].id, PASS, later, LEASE)
    assert _run(db, ext_id).lease_expires_at == later + timedelta(seconds=LEASE)


def test_record_result_rejected_for_non_owner(client, store, db):
    ext_id = queue_run(client)
    spec = store.claim_next("w1", T0, LEASE)
    with pytest.raises(RunNotOwned):
        store.record_result(ext_id, "intruder", spec.scenarios[0].id, PASS, T0, LEASE)
    assert _results(db, ext_id) == []


def test_record_result_rejected_for_unclaimed_run(client, store, db):
    ext_id = queue_run(client)
    with pytest.raises(RunNotOwned):
        store.record_result(ext_id, "w1", "whatever", PASS, T0, LEASE)
    assert _run(db, ext_id).status == lc.QUEUED


@pytest.mark.parametrize("ending", [lc.COMPLETED, lc.FAILED, lc.CANCELLED])
def test_no_late_writes_after_run_ended(client, store, db, ending):
    ext_id = queue_run(client)
    spec = store.claim_next("w1", T0, LEASE)
    store.record_result(ext_id, "w1", spec.scenarios[0].id, PASS, T0, LEASE)
    if ending == lc.CANCELLED:  # what a future cancel endpoint will do
        db.execute(update(Run).where(Run.external_run_id == ext_id).values(status=lc.CANCELLED))
        db.commit()
    else:
        assert store.finish_run(ext_id, "w1", ending, T0, "X" if ending == lc.FAILED else None)

    with pytest.raises(RunNotOwned):
        store.record_result(ext_id, "w1", spec.scenarios[1].id, PASS, T0, LEASE)
    assert store.renew_lease(ext_id, "w1", T0, LEASE) is False
    assert len(_results(db, ext_id)) == 1
    assert _run(db, ext_id).status == ending


# --- finishing ----------------------------------------------------------------------


def test_finish_sets_terminal_status_and_completed_at(client, store, db):
    ext_id = queue_run(client)
    store.claim_next("w1", T0, LEASE)
    done = T0 + timedelta(seconds=7)
    assert store.finish_run(ext_id, "w1", lc.COMPLETED, done) is True
    run = _run(db, ext_id)
    assert (run.status, run.started_at, run.completed_at) == (lc.COMPLETED, T0, done)
    assert run.lease_expires_at is None
    assert run.error_code is None and run.error_message is None


def test_failed_records_error(client, store, db):
    ext_id = queue_run(client)
    store.claim_next("w1", T0, LEASE)
    store.finish_run(ext_id, "w1", lc.FAILED, T0, lc.EXECUTION_ERROR, "boom")
    run = _run(db, ext_id)
    assert (run.status, run.error_code, run.error_message) == (lc.FAILED, lc.EXECUTION_ERROR, "boom")


def test_terminal_status_cannot_be_overwritten(client, store, db):
    ext_id = queue_run(client)
    store.claim_next("w1", T0, LEASE)
    store.finish_run(ext_id, "w1", lc.FAILED, T0, lc.RUN_TIMEOUT, "too slow")
    # e.g. an executor that ignored cancellation finishing late and reporting success
    assert store.finish_run(ext_id, "w1", lc.COMPLETED, T0 + timedelta(seconds=5)) is False
    run = _run(db, ext_id)
    assert (run.status, run.error_code, run.completed_at) == (lc.FAILED, lc.RUN_TIMEOUT, T0)


def test_finish_by_non_owner_is_rejected(client, store, db):
    ext_id = queue_run(client)
    store.claim_next("w1", T0, LEASE)
    assert store.finish_run(ext_id, "w2", lc.COMPLETED, T0) is False
    assert _run(db, ext_id).status == lc.RUNNING


def test_queued_run_cannot_jump_to_completed(client, store, db):
    ext_id = queue_run(client)
    assert store.finish_run(ext_id, "w1", lc.COMPLETED, T0) is False
    assert _run(db, ext_id).status == lc.QUEUED


def test_finish_rejects_non_terminal_status(client, store):
    ext_id = queue_run(client)
    store.claim_next("w1", T0, LEASE)
    with pytest.raises(ValueError):
        store.finish_run(ext_id, "w1", lc.RUNNING, T0)


# --- recovery -----------------------------------------------------------------------


def test_live_lease_is_not_recovered(client, store, db):
    ext_id = queue_run(client)
    store.claim_next("w1", T0, LEASE)
    assert store.recover_expired_leases(T0 + timedelta(seconds=LEASE - 1)) == []
    assert _run(db, ext_id).status == lc.RUNNING


def test_expired_lease_is_failed_as_interrupted_keeping_partial_results(client, store, db):
    ext_id = queue_run(client)
    spec = store.claim_next("w1", T0, LEASE)
    store.record_result(ext_id, "w1", spec.scenarios[0].id, PASS, T0, LEASE)

    later = T0 + timedelta(seconds=LEASE + 1)
    assert store.recover_expired_leases(later) == [ext_id]
    run = _run(db, ext_id)
    assert (run.status, run.error_code) == (lc.FAILED, lc.WORKER_INTERRUPTED)
    assert "retry" in run.error_message
    assert run.started_at == T0 and run.completed_at == later
    assert [r.scenario_id for r in _results(db, ext_id)] == [spec.scenarios[0].id]

    # The presumed-dead worker comes back: it can no longer write anything.
    with pytest.raises(RunNotOwned):
        store.record_result(ext_id, "w1", spec.scenarios[1].id, PASS, later, LEASE)
    assert store.finish_run(ext_id, "w1", lc.COMPLETED, later) is False
    assert store.recover_expired_leases(later + timedelta(hours=1)) == []  # recovered once


def test_recovery_ignores_queued_and_finished_runs(client, store, db):
    queued = queue_run(client)
    finished = queue_run(client)
    db.execute(update(Run).where(Run.external_run_id == finished).values(status=lc.COMPLETED))
    db.commit()
    assert store.recover_expired_leases(T0 + timedelta(days=1)) == []
    assert _run(db, queued).status == lc.QUEUED
    assert _run(db, finished).status == lc.COMPLETED


def test_running_run_without_lease_is_treated_as_orphaned(client, store, db):
    ext_id = queue_run(client)
    db.execute(update(Run).where(Run.external_run_id == ext_id).values(status=lc.RUNNING, started_at=T0))
    db.commit()
    assert store.recover_expired_leases(T0) == [ext_id]


def test_result_count_matches_scenarios_after_full_run(client, store, db):
    ext_id = queue_run(client)
    spec = store.claim_next("w1", T0, LEASE)
    for s in spec.scenarios:
        store.record_result(ext_id, "w1", s.id, PASS, T0, LEASE)
    store.finish_run(ext_id, "w1", lc.COMPLETED, T0)
    count = db.scalar(select(func.count()).select_from(ScenarioResult).where(ScenarioResult.external_run_id == ext_id))
    assert count == len(spec.scenarios)

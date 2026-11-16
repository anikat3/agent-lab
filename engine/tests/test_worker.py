"""Worker behaviour, using test-only executors. No LLM is ever called."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app import lifecycle as lc
from app.execution import ExecutionError, RunSpec, ScenarioOutcome
from app.main import create_app
from app.models import Run, ScenarioResult
from app.run_store import RunStore
from app.schemas import ScenarioSpec
from app.worker import SHUTDOWN_MESSAGE, Worker

from .conftest import make_api_app, make_settings, queue_run, run_status, wait_until

S1 = "a1a1a1a1-0000-4000-8000-000000000001"
S2 = "a1a1a1a1-0000-4000-8000-000000000002"


def outcome(sid: str) -> ScenarioOutcome:
    return ScenarioOutcome(passed=True, quality_score=1.0, latency_ms=5, actual_response=f"answer for {sid}")


class Recorder:
    """Test executor: records calls, answers instantly unless told otherwise per scenario."""

    def __init__(self, on: dict | None = None) -> None:  # type: ignore[type-arg]
        self.calls: list[str] = []
        self.runs: list[RunSpec] = []
        self.on = on or {}

    async def __call__(self, run: RunSpec, scenario: ScenarioSpec) -> ScenarioOutcome:
        self.calls.append(scenario.id)
        self.runs.append(run)
        behaviour = self.on.get(scenario.id)
        if behaviour is not None:
            return await behaviour(run, scenario)
        return outcome(scenario.id)


async def hang(run, scenario):  # type: ignore[no-untyped-def]
    await asyncio.Event().wait()  # never set


def _set(db, ext_id, **values) -> None:  # type: ignore[no-untyped-def]
    db.execute(update(Run).where(Run.external_run_id == ext_id).values(**values))
    db.commit()


def _results(db, ext_id) -> list[str]:  # type: ignore[no-untyped-def]
    db.expire_all()
    return [
        r.scenario_id
        for r in db.scalars(
            select(ScenarioResult).where(ScenarioResult.external_run_id == ext_id).order_by(ScenarioResult.id)
        )
    ]


def run_worker_once(store: RunStore, executor, **kw) -> bool:  # type: ignore[no-untyped-def]
    return asyncio.run(Worker(store, executor, **kw).run_once())


# --- happy path and state transitions --------------------------------------------------


def test_completed_run_reports_results_through_the_api(client, store):
    ext_id = queue_run(client)
    executor = Recorder()
    before = datetime.now(UTC)
    assert run_worker_once(store, executor) is True

    assert executor.calls == [S1, S2]  # sequential, in launch order
    assert executor.runs[0].agent.system_prompt == "You are a helpful support agent."

    body = run_status(client, ext_id)
    assert body["status"] == "completed"
    assert body["error"] is None
    assert body["progress"] == {"completed": 2, "total": 2}
    assert [r["scenario_id"] for r in body["results"]] == [S1, S2]
    assert body["results"][0]["actual_response"] == f"answer for {S1}"
    started = datetime.fromisoformat(body["started_at"])
    completed = datetime.fromisoformat(body["completed_at"])
    assert before <= started <= completed <= datetime.now(UTC)


def test_run_is_running_with_partial_progress_mid_execution(client, store):
    ext_id = queue_run(client)
    seen: dict = {}  # type: ignore[type-arg]

    async def peek(run, scenario):  # type: ignore[no-untyped-def]
        seen.update(await asyncio.to_thread(run_status, client, ext_id))
        return outcome(scenario.id)

    run_worker_once(store, Recorder({S2: peek}))
    assert seen["status"] == "running"
    assert seen["started_at"] is not None and seen["completed_at"] is None
    assert seen["progress"] == {"completed": 1, "total": 2}
    assert run_status(client, ext_id)["status"] == "completed"


def test_nothing_queued_means_nothing_executed(store):
    executor = Recorder()
    assert run_worker_once(store, executor) is False
    assert executor.calls == []


# --- executor failures -----------------------------------------------------------------


def test_executor_exception_fails_run_and_keeps_partial_results(client, store, db):
    ext_id = queue_run(client)

    async def boom(run, scenario):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider exploded")

    run_worker_once(store, Recorder({S2: boom}))
    body = run_status(client, ext_id)
    assert body["status"] == "failed"
    assert body["error"]["code"] == lc.EXECUTION_ERROR
    assert "RuntimeError: provider exploded" in body["error"]["message"]
    assert body["completed_at"] is not None
    assert body["progress"] == {"completed": 1, "total": 2}
    assert _results(db, ext_id) == [S1]


def test_execution_error_sets_its_own_code(client, store):
    ext_id = queue_run(client)

    async def bad_key(run, scenario):  # type: ignore[no-untyped-def]
        raise ExecutionError("MODEL_AUTH_FAILED", "Provider rejected the API key")

    run_worker_once(store, Recorder({S1: bad_key}))
    assert run_status(client, ext_id)["error"] == {"code": "MODEL_AUTH_FAILED", "message": "Provider rejected the API key"}


def test_invalid_executor_output_fails_run(client, store):
    ext_id = queue_run(client)

    async def wrong_type(run, scenario):  # type: ignore[no-untyped-def]
        return {"passed": True}

    run_worker_once(store, Recorder({S1: wrong_type}))
    assert run_status(client, ext_id)["error"]["code"] == lc.INVALID_EXECUTOR_OUTPUT


def test_failed_scenario_is_a_result_not_a_run_failure(client, store):
    ext_id = queue_run(client)

    async def wrong_answer(run, scenario):  # type: ignore[no-untyped-def]
        return ScenarioOutcome(passed=False, quality_score=0.0, failure_category="keyword_missing")

    run_worker_once(store, Recorder({S1: wrong_answer}))
    body = run_status(client, ext_id)
    assert body["status"] == "completed"
    assert body["results"][0]["passed"] is False


def test_executor_timeout_error_is_not_a_run_timeout(client, store):
    ext_id = queue_run(client)

    async def http_timeout(run, scenario):  # type: ignore[no-untyped-def]
        raise TimeoutError("read timed out")

    run_worker_once(store, Recorder({S1: http_timeout}))
    assert run_status(client, ext_id)["error"]["code"] == lc.EXECUTION_ERROR


# --- run-wide timeout --------------------------------------------------------------------


def test_run_timeout_fails_run_and_keeps_completed_results(client, store, db):
    ext_id = queue_run(client)
    _set(db, ext_id, timeout_seconds=1)  # below the API's minimum, to keep the test fast
    started = time.monotonic()
    run_worker_once(store, Recorder({S2: hang}))
    assert time.monotonic() - started < 3

    body = run_status(client, ext_id)
    assert body["status"] == "failed"
    assert body["error"] == {"code": lc.RUN_TIMEOUT, "message": "Run exceeded its 1s timeout after 1/2 scenarios"}
    assert _results(db, ext_id) == [S1]


def test_deadline_is_measured_from_started_at(client, store):
    """If the deadline has already passed by the time execution begins, nothing runs."""
    ext_id = queue_run(client)
    t0 = datetime.now(UTC)
    times = iter([t0, t0 + timedelta(seconds=301)])  # claim at t0; deadline is t0 + 300s

    executor = Recorder()
    run_worker_once(store, executor, clock=lambda: next(times, t0 + timedelta(seconds=302)))
    assert executor.calls == []
    body = run_status(client, ext_id)
    assert body["error"]["code"] == lc.RUN_TIMEOUT
    assert datetime.fromisoformat(body["started_at"]) == t0


def test_blocking_executor_cannot_write_after_timeout(client, store, db):
    """A thread can't be cancelled: the run fails on time anyway, and the result the
    thread produces later is discarded."""
    ext_id = queue_run(client)
    _set(db, ext_id, timeout_seconds=1)

    async def blocking(run, scenario):  # type: ignore[no-untyped-def]
        await asyncio.to_thread(time.sleep, 1.5)  # e.g. a sync SDK call without its own timeout
        return outcome(scenario.id)

    async def main() -> float:
        started = time.monotonic()
        await Worker(store, Recorder({S1: blocking})).run_once()
        return time.monotonic() - started
        # (asyncio.run then waits for the abandoned thread before returning)

    elapsed = asyncio.run(main())
    assert elapsed < 1.4  # the run was failed on time, without waiting for the thread
    assert run_status(client, ext_id)["error"]["code"] == lc.RUN_TIMEOUT
    # The thread has finished by now; its result was never written.
    assert _results(db, ext_id) == []
    assert run_status(client, ext_id)["status"] == "failed"


# --- concurrency -----------------------------------------------------------------------


def test_two_workers_never_execute_the_same_run(client, store):
    ext_ids = {queue_run(client) for _ in range(3)}
    executor = Recorder()

    async def slow(run, scenario):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.01)
        return outcome(scenario.id)

    executor.on = {S1: slow, S2: slow}

    async def drain(worker: Worker) -> None:
        while await worker.run_once():
            pass

    async def main() -> None:
        await asyncio.gather(drain(Worker(store, executor, worker_id="w1")), drain(Worker(store, executor, worker_id="w2")))

    asyncio.run(main())
    executed = [r.external_run_id for r in executor.runs]
    assert sorted(executed) == sorted([e for e in ext_ids for _ in (S1, S2)])  # each run's 2 scenarios, once
    for ext_id in ext_ids:
        assert run_status(client, ext_id)["status"] == "completed"


def test_heartbeat_keeps_a_long_run_from_being_recovered(client, store, db):
    """A run that outlives its lease is safe as long as the owner keeps renewing."""
    ext_id = queue_run(client)

    async def slow(run, scenario):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.4)
        return outcome(scenario.id)

    async def main() -> None:
        worker = Worker(store, Recorder({S1: slow, S2: slow}), lease_seconds=0.3)
        rival = Worker(store, Recorder(), lease_seconds=0.3)  # e.g. another process's sweep
        run = asyncio.create_task(worker.run_once())
        while not run.done():
            await rival.recover()
            await asyncio.sleep(0.03)
        await run

    asyncio.run(main())
    assert run_status(client, ext_id)["status"] == "completed"


def test_worker_stops_when_run_is_ended_by_someone_else(client, store, db):
    """E.g. a future cancel endpoint: the worker's next write is rejected, it stops
    executing, and it does not overwrite the status."""
    ext_id = queue_run(client)

    async def cancel_meanwhile(run, scenario):  # type: ignore[no-untyped-def]
        await asyncio.to_thread(_set, db, ext_id, status=lc.CANCELLED, completed_at=datetime.now(UTC))
        return outcome(scenario.id)

    executor = Recorder({S1: cancel_meanwhile})
    run_worker_once(store, executor)
    assert executor.calls == [S1]  # S2 never started
    assert run_status(client, ext_id)["status"] == "cancelled"
    assert _results(db, ext_id) == []


def test_lost_lease_detected_by_heartbeat_stops_before_next_scenario(client, store, db):
    ext_id = queue_run(client)

    async def lose_lease(run, scenario):  # type: ignore[no-untyped-def]
        await asyncio.to_thread(_set, db, ext_id, worker_id="someone-else")
        await asyncio.sleep(0.15)  # long enough for a heartbeat (lease 0.3 / 3)
        return outcome(scenario.id)

    executor = Recorder({S1: lose_lease})
    run_worker_once(store, executor, lease_seconds=0.3)
    assert executor.calls == [S1]
    assert _results(db, ext_id) == []
    assert run_status(client, ext_id)["status"] == "running"  # not ours to finish


def test_poll_loop_survives_database_errors(client, store):
    ext_id = queue_run(client)
    real_claim = store.claim_next
    failures = {"left": 2}

    def flaky_claim(*args):  # type: ignore[no-untyped-def]
        if failures["left"]:
            failures["left"] -= 1
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        return real_claim(*args)

    store.claim_next = flaky_claim  # type: ignore[method-assign]

    async def main() -> None:
        worker = Worker(store, Recorder(), poll_interval=0.01)
        worker.start()
        while (await asyncio.to_thread(run_status, client, ext_id))["status"] != "completed":
            await asyncio.sleep(0.02)
        await worker.stop(grace_seconds=1)

    asyncio.run(asyncio.wait_for(main(), timeout=5))
    assert failures["left"] == 0


# --- app integration: no executor, lifespan worker, shutdown, restart -------------------


def test_api_only_app_leaves_runs_queued(app, client):
    """start_worker=False: launches are accepted, but nothing in this process executes them."""
    ext_id = queue_run(client)
    assert app.state.worker is None
    time.sleep(0.2)
    body = run_status(client, ext_id)
    assert body["status"] == "queued"
    assert body["started_at"] is None and body["results"] == []


def test_lifespan_worker_executes_launched_runs(tmp_path):
    with TestClient(create_app(make_settings(tmp_path), executor=Recorder())) as client:
        ext_id = queue_run(client)
        body = wait_until(lambda: (b := run_status(client, ext_id))["status"] == "completed" and b)
        assert body["progress"] == {"completed": 2, "total": 2}


def test_shutdown_interrupts_in_flight_run_and_keeps_partial_results(tmp_path):
    settings = make_settings(tmp_path, worker_shutdown_grace_seconds=0.1)
    app = create_app(settings, executor=Recorder({S2: hang}))
    with TestClient(app) as client:
        ext_id = queue_run(client)
        wait_until(lambda: run_status(client, ext_id)["progress"]["completed"] == 1)
    # Server stopped. Start a fresh one (no executor) to read what was persisted.
    with TestClient(create_app(settings)) as client:
        body = run_status(client, ext_id)
    assert body["status"] == "failed"
    assert body["error"] == {"code": lc.WORKER_INTERRUPTED, "message": SHUTDOWN_MESSAGE}
    assert body["completed_at"] is not None
    assert [r["scenario_id"] for r in body["results"]] == [S1]


def test_shutdown_lets_in_flight_run_finish_within_grace(tmp_path):
    async def slowish(run, scenario):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.2)
        return outcome(scenario.id)

    settings = make_settings(tmp_path, worker_shutdown_grace_seconds=5)
    with TestClient(create_app(settings, executor=Recorder({S1: slowish, S2: slowish}))) as client:
        ext_id = queue_run(client)
        wait_until(lambda: run_status(client, ext_id)["status"] == "running")
    with TestClient(create_app(settings)) as client:
        assert run_status(client, ext_id)["status"] == "completed"


def test_restart_recovers_crashed_run_without_rerunning_it(tmp_path):
    settings = make_settings(tmp_path, worker_lease_seconds=30)
    with TestClient(make_api_app(tmp_path, worker_lease_seconds=30)) as client:
        ext_id = queue_run(client)
        store: RunStore = client.app.state.run_store  # type: ignore[attr-defined]
        # A worker that claimed the run a minute ago, stored one result, then died.
        long_ago = datetime.now(UTC) - timedelta(seconds=60)
        store.claim_next("dead-worker", long_ago, 30)
        store.record_result(ext_id, "dead-worker", S1, outcome(S1), long_ago, 30)

    executor = Recorder()
    with TestClient(create_app(settings, executor=executor)) as client:  # the restart
        body = run_status(client, ext_id)
        time.sleep(0.1)  # give the new worker a chance to (wrongly) pick it up
    assert body["status"] == "failed"
    assert body["error"]["code"] == lc.WORKER_INTERRUPTED
    assert [r["scenario_id"] for r in body["results"]] == [S1]
    assert executor.calls == []  # interrupted work is never re-executed automatically


def test_another_process_starting_does_not_interrupt_a_live_run(tmp_path):
    settings = make_settings(tmp_path, worker_lease_seconds=1, worker_shutdown_grace_seconds=0)
    with TestClient(create_app(settings, executor=Recorder({S2: hang}))) as first:
        ext_id = queue_run(first)
        wait_until(lambda: run_status(first, ext_id)["status"] == "running")
        time.sleep(1.2)  # longer than the lease: only heartbeats keep it alive
        with TestClient(create_app(settings)) as second:  # second process, startup recovery
            assert run_status(second, ext_id)["status"] == "running"
        assert run_status(first, ext_id)["status"] == "running"

"""Background worker: claims queued runs and drives them to a terminal status.

One worker per process, running as an asyncio task beside the HTTP API, processing one
run at a time and its scenarios sequentially. The HTTP layer never executes anything; it
only stores runs (POST) and reads them (GET). The worker never handles HTTP.

Per run:
    claim (queued -> running, lease taken)
      -> for each scenario: executor(run, scenario) -> record_result (fenced)
      -> completed | failed(RUN_TIMEOUT | EXECUTION_ERROR | <executor code>)
    meanwhile a heartbeat task renews the lease every lease/3 seconds.

The database is the source of truth. Blocking SQLAlchemy calls go through
asyncio.to_thread so they never stall the event loop the API also runs on.
"""

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import OperationalError

from app import lifecycle
from app.execution import ExecutionError, RunSpec, ScenarioExecutor, ScenarioOutcome
from app.run_store import RunNotOwned, RunStore

logger = logging.getLogger(__name__)

SHUTDOWN_MESSAGE = (
    "The engine shut down before this run finished. Results stored before the shutdown "
    "were kept; retry the run to execute it again."
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Outcome:
    """How a run ended, as decided by the worker."""

    status: str
    error_code: str | None = None
    error_message: str | None = None


class Worker:
    def __init__(
        self,
        store: RunStore,
        executor: ScenarioExecutor,
        *,
        worker_id: str | None = None,
        poll_interval: float = 1.0,
        lease_seconds: float = 30.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.executor = executor
        self.worker_id = worker_id or make_worker_id()
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.clock = clock
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"worker:{self.worker_id}")

    async def stop(self, grace_seconds: float) -> None:
        """Stop claiming new runs; give the current run `grace_seconds` to finish, then
        interrupt it (it is marked failed with WORKER_INTERRUPTED)."""
        self._stopping.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except TimeoutError:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        logger.info("Worker %s started", self.worker_id)
        last_recovery = float("-inf")
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            processed = False
            try:
                # Sweep for orphaned runs about once per lease period.
                if loop.time() - last_recovery >= self.lease_seconds:
                    await self.recover()
                    last_recovery = loop.time()
                processed = await self.run_once()
            except OperationalError:
                # Database busy beyond busy_timeout, or briefly unavailable: back off, retry.
                logger.warning("Worker %s: database unavailable, retrying", self.worker_id, exc_info=True)
            except Exception:
                logger.exception("Worker %s: unexpected error in poll loop", self.worker_id)
            if not processed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
        logger.info("Worker %s stopped", self.worker_id)

    async def recover(self) -> list[str]:
        recovered = await asyncio.to_thread(self.store.recover_expired_leases, self.clock())
        if recovered:
            logger.warning("Marked interrupted runs as failed: %s", recovered)
        return recovered

    # --- one run --------------------------------------------------------------------

    async def run_once(self) -> bool:
        """Claim and fully process one queued run. Returns False if none was queued."""
        run = await asyncio.to_thread(self.store.claim_next, self.worker_id, self.clock(), self.lease_seconds)
        if run is None:
            return False
        logger.info("Worker %s claimed %s (%d scenarios)", self.worker_id, run.external_run_id, len(run.scenarios))
        await self._process(run)
        return True

    async def _process(self, run: RunSpec) -> None:
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(run.external_run_id, lost))
        try:
            outcome = await self._execute(run, lost)
        except asyncio.CancelledError:
            # Shutdown: record that this run was interrupted, then let cancellation proceed.
            await self._finish(run, Outcome(lifecycle.FAILED, lifecycle.WORKER_INTERRUPTED, SHUTDOWN_MESSAGE))
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        if outcome is None:
            logger.warning("Worker %s lost ownership of %s; leaving it alone", self.worker_id, run.external_run_id)
            return
        await self._finish(run, outcome)

    async def _execute(self, run: RunSpec, lost: asyncio.Event) -> Outcome | None:
        """Run every scenario in order. Returns how the run ended, or None if this worker
        lost ownership (someone else has already decided the run's fate)."""
        total = len(run.scenarios)
        done = 0
        remaining = lifecycle.remaining_seconds(run.started_at, run.timeout_seconds, self.clock())
        if remaining <= 0:
            return _timeout_outcome(run, done, total)

        timeout = asyncio.timeout(remaining)
        try:
            async with timeout:
                for scenario in run.scenarios:
                    if lost.is_set():
                        return None
                    outcome = await self.executor(run, scenario)
                    if not isinstance(outcome, ScenarioOutcome):
                        raise ExecutionError(
                            lifecycle.INVALID_EXECUTOR_OUTPUT,
                            f"Executor returned {type(outcome).__name__} for scenario {scenario.id!r}, "
                            "expected ScenarioOutcome",
                        )
                    await asyncio.to_thread(
                        self.store.record_result,
                        run.external_run_id,
                        self.worker_id,
                        scenario.id,
                        outcome,
                        self.clock(),
                        self.lease_seconds,
                    )
                    done += 1
        except RunNotOwned:
            return None
        except TimeoutError:
            if timeout.expired():
                return _timeout_outcome(run, done, total)
            # A TimeoutError raised *by the executor* (e.g. an HTTP client timeout) is an
            # execution failure, not the run-wide deadline.
            logger.warning("Executor timed out on %s", run.external_run_id, exc_info=True)
            return Outcome(lifecycle.FAILED, lifecycle.EXECUTION_ERROR, f"Executor timed out ({done}/{total} scenarios done)")
        except ExecutionError as e:
            return Outcome(lifecycle.FAILED, e.code, e.message)
        except Exception as e:
            logger.exception("Executor failed on %s", run.external_run_id)
            return Outcome(lifecycle.FAILED, lifecycle.EXECUTION_ERROR, f"{type(e).__name__}: {e}"[:1000])
        return Outcome(lifecycle.COMPLETED)

    async def _finish(self, run: RunSpec, outcome: Outcome) -> None:
        finished = await asyncio.to_thread(
            self.store.finish_run,
            run.external_run_id,
            self.worker_id,
            outcome.status,
            self.clock(),
            outcome.error_code,
            outcome.error_message,
        )
        if finished:
            logger.info("Run %s -> %s %s", run.external_run_id, outcome.status, outcome.error_code or "")
        else:
            logger.warning("Run %s was no longer ours to finish (wanted %s)", run.external_run_id, outcome.status)

    async def _heartbeat(self, external_run_id: str, lost: asyncio.Event) -> None:
        interval = self.lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await asyncio.to_thread(
                    self.store.renew_lease, external_run_id, self.worker_id, self.clock(), self.lease_seconds
                )
            except OperationalError:
                logger.warning("Lease renewal for %s failed; will retry", external_run_id, exc_info=True)
                continue
            if not ok:
                lost.set()
                return


def _timeout_outcome(run: RunSpec, done: int, total: int) -> Outcome:
    return Outcome(
        lifecycle.FAILED,
        lifecycle.RUN_TIMEOUT,
        f"Run exceeded its {run.timeout_seconds}s timeout after {done}/{total} scenarios",
    )

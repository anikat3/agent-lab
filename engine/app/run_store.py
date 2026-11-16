"""Database operations used by the worker.

Every method is one short transaction on its own session. Every state change is a
*conditional* UPDATE: "move this run to X only if it is still in state Y (and still owned
by me)". The database applies that check and the write as one atomic step, which gives
two guarantees without any locks held in Python:

- Claiming is exclusive: of several workers racing for a queued run, exactly one UPDATE
  matches `status = 'queued'`; the others match zero rows.
- Terminal states are final, and stale workers are fenced off: once a run is completed,
  failed or cancelled, or its lease was taken over, every later write from its old worker
  matches zero rows and is rejected.

SQLite allows one writer at a time. Each write transaction here *starts* with its UPDATE,
so it takes the write lock first and waits up to busy_timeout if another writer holds it.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import Update, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app import lifecycle
from app.execution import RunSpec, ScenarioOutcome
from app.models import Run, ScenarioResult
from app.schemas import AgentSpec, Message, ScenarioSpec, SuiteSpec


class RunNotOwned(Exception):
    """The worker no longer owns this run (it ended, or its lease was taken over)."""


INTERRUPTED_MESSAGE = (
    "The worker executing this run stopped before it finished (crash, restart or deploy). "
    "Results stored before the interruption were kept; retry the run to execute it again."
)


class RunStore:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    # --- claiming -------------------------------------------------------------------

    def claim_next(self, worker_id: str, now: datetime, lease_seconds: float) -> RunSpec | None:
        """Atomically move the oldest queued run to running and return it, or None."""
        with self._session_factory() as session:
            candidates = session.scalars(
                select(Run.external_run_id).where(Run.status == lifecycle.QUEUED).order_by(Run.created_at).limit(10)
            ).all()
            session.rollback()  # end the read before starting a write transaction
            for external_run_id in candidates:
                claimed = session.execute(
                    _transition(external_run_id, lifecycle.RUNNING).values(
                        worker_id=worker_id,
                        started_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                    )
                )
                if claimed.rowcount == 1:
                    session.commit()
                    return self._load_spec(session, external_run_id)
                session.rollback()  # another worker got it first; try the next one
        return None

    # --- while running --------------------------------------------------------------

    def renew_lease(self, external_run_id: str, worker_id: str, now: datetime, lease_seconds: float) -> bool:
        """Extend this worker's lease. False means the worker no longer owns the run."""
        with self._session_factory() as session:
            renewed = session.execute(
                _owned(external_run_id, worker_id).values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            session.commit()
            return renewed.rowcount == 1

    def record_result(
        self,
        external_run_id: str,
        worker_id: str,
        scenario_id: str,
        outcome: ScenarioOutcome,
        now: datetime,
        lease_seconds: float,
    ) -> None:
        """Store (or replace) one scenario's result, only if the worker still owns the run.

        The ownership check and the result write are one transaction: the conditional
        UPDATE takes the write lock (SQLite) or locks the run row (Postgres), so the run
        cannot be finished, recovered or cancelled between the check and the write.
        Raises RunNotOwned if the run has ended or been taken over.
        """
        with self._session_factory() as session:
            owned = session.execute(
                _owned(external_run_id, worker_id).values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            if owned.rowcount != 1:
                session.rollback()
                raise RunNotOwned(external_run_id)
            values = outcome.model_dump()
            existing = session.scalars(
                select(ScenarioResult).where(
                    ScenarioResult.external_run_id == external_run_id,
                    ScenarioResult.scenario_id == scenario_id,
                )
            ).one_or_none()
            if existing is None:
                session.add(ScenarioResult(external_run_id=external_run_id, scenario_id=scenario_id, **values))
            else:  # writing the same scenario twice replaces it; never a second row
                for key, value in values.items():
                    setattr(existing, key, value)
            session.commit()

    # --- ending ---------------------------------------------------------------------

    def finish_run(
        self,
        external_run_id: str,
        worker_id: str,
        status: str,
        now: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Move an owned running run to a terminal status. False if it was not ours to end."""
        if not lifecycle.is_terminal(status):
            raise ValueError(f"{status!r} is not a terminal status")
        with self._session_factory() as session:
            finished = session.execute(
                _owned(external_run_id, worker_id, new_status=status).values(
                    status=status,
                    completed_at=now,
                    lease_expires_at=None,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            session.commit()
            return finished.rowcount == 1

    def recover_expired_leases(self, now: datetime) -> list[str]:
        """Fail running runs whose owner stopped renewing its lease. Returns their ids.

        Only runs with an *expired* lease are touched, so a live worker in another process
        is never mistaken for a dead one. Stored results are kept and nothing is re-run.
        """
        with self._session_factory() as session:
            # One statement: the expiry check and the write are atomic, so a lease renewed
            # a moment earlier is respected.
            recovered = session.scalars(
                update(Run)
                .where(
                    Run.status == lifecycle.RUNNING,
                    or_(Run.lease_expires_at.is_(None), Run.lease_expires_at < now),
                )
                .values(
                    status=lifecycle.FAILED,
                    completed_at=now,
                    lease_expires_at=None,
                    error_code=lifecycle.WORKER_INTERRUPTED,
                    error_message=INTERRUPTED_MESSAGE,
                )
                .returning(Run.external_run_id)
                .execution_options(synchronize_session=False)
            ).all()
            session.commit()
            return list(recovered)

    # --- helpers --------------------------------------------------------------------

    def _load_spec(self, session: Session, external_run_id: str) -> RunSpec:
        run = session.scalars(
            select(Run).where(Run.external_run_id == external_run_id).options(selectinload(Run.scenarios))
        ).one()
        assert run.started_at is not None
        # Stored data was validated on the way in; strict=False only lets nested dicts
        # from the JSON columns become models again.
        return RunSpec(
            external_run_id=run.external_run_id,
            run_id=run.run_id,
            agent=AgentSpec.model_validate(run.agent, strict=False),
            suite=SuiteSpec.model_validate(run.suite, strict=False),
            timeout_seconds=run.timeout_seconds,
            started_at=run.started_at,
            scenarios=tuple(
                ScenarioSpec(
                    id=s.scenario_id,
                    name=s.name,
                    input_messages=[Message.model_validate(m, strict=False) for m in s.input_messages],
                    expected_response=s.expected_response,
                    eval_type=s.eval_type,
                    eval_config=s.eval_config,
                )
                for s in run.scenarios
            ),
            judge_models={s.scenario_id: s.judge_model for s in run.scenarios if s.judge_model},
        )


def _transition(external_run_id: str, new_status: str) -> Update:
    """UPDATE that only matches if the run's current status may move to `new_status`."""
    return (
        update(Run)
        .where(Run.external_run_id == external_run_id, Run.status.in_(lifecycle.sources_for(new_status)))
        .values(status=new_status)
        .execution_options(synchronize_session=False)
    )


def _owned(external_run_id: str, worker_id: str, new_status: str | None = None) -> Update:
    """UPDATE that only matches a running run owned by `worker_id`."""
    if new_status is not None and not lifecycle.can_transition(lifecycle.RUNNING, new_status):
        raise ValueError(f"cannot move a running run to {new_status!r}")
    return (
        update(Run)
        .where(
            Run.external_run_id == external_run_id,
            Run.status == lifecycle.RUNNING,
            Run.worker_id == worker_id,
        )
        .execution_options(synchronize_session=False)
    )

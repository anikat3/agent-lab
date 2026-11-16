"""Pure run-lifecycle rules: statuses, allowed transitions, error codes, deadlines.

The engine-side transitions are the subset of the app's (src/lib/api/engine-logic.ts)
that starts at `queued`, since the app owns `pending`.
"""

from datetime import datetime, timedelta

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, FAILED, CANCELLED}),
    RUNNING: frozenset({COMPLETED, FAILED, CANCELLED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}

# Run-level error codes reported in GET /runs/{id} -> error.code.
RUN_TIMEOUT = "RUN_TIMEOUT"  # run exceeded timeout_seconds (defined by the contract)
WORKER_INTERRUPTED = "WORKER_INTERRUPTED"  # worker crashed, restarted or shut down mid-run
EXECUTION_ERROR = "EXECUTION_ERROR"  # executor raised an unexpected exception
INVALID_EXECUTOR_OUTPUT = "INVALID_EXECUTOR_OUTPUT"  # executor returned something unusable


def can_transition(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, frozenset())


def is_terminal(status: str) -> bool:
    return status in TRANSITIONS and not TRANSITIONS[status]


def sources_for(new: str) -> frozenset[str]:
    """Statuses a run may be in for a move to `new` to be allowed."""
    return frozenset(s for s, targets in TRANSITIONS.items() if new in targets)


def deadline(started_at: datetime, timeout_seconds: int) -> datetime:
    return started_at + timedelta(seconds=timeout_seconds)


def remaining_seconds(started_at: datetime, timeout_seconds: int, now: datetime) -> float:
    """Seconds left before the run-wide deadline, measured from the run's actual start."""
    return (deadline(started_at, timeout_seconds) - now).total_seconds()

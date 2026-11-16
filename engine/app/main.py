"""Application factory. Run with: uvicorn app.main:create_app --factory"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.errors import register_error_handlers
from app.execution import ScenarioExecutor
from app.graph import build_executor
from app.migrations import run_migrations
from app.routes import router
from app.run_store import RunStore
from app.worker import Worker, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionStatus:
    available: bool
    executor: str | None
    reason: str | None


def create_app(
    settings: Settings | None = None,
    *,
    executor: ScenarioExecutor | None = None,
    start_worker: bool = True,
) -> FastAPI:
    """Build the app.

    The executor comes from `executor` if given (tests), otherwise from settings
    (EVAL_EXECUTOR). Without one, no worker starts and POST /runs answers 503: the engine
    never accepts a run that nothing will execute, and never reports one as executed.

    `start_worker=False` serves the API with an executor configured but no worker in this
    process (tests; later, a separate worker process).
    """
    settings = settings or get_settings()
    _configure_logging()
    if executor is None:
        executor, reason = build_executor(settings)
    else:
        reason = None
    execution = ExecutionStatus(
        available=executor is not None,
        executor=_executor_name(executor) if executor is not None else None,
        reason=reason,
    )

    engine = make_engine(settings.database_url, settings.sqlite_busy_timeout_ms)
    session_factory = make_session_factory(engine)
    store = RunStore(session_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        run_migrations(engine)
        # Safe in every process: only runs whose owner stopped renewing its lease are touched.
        recovered = store.recover_expired_leases(utcnow())
        if recovered:
            logger.warning("Marked %d interrupted run(s) as failed: %s", len(recovered), recovered)

        worker: Worker | None = None
        if executor is None:
            logger.warning("Execution unavailable: %s. New launches will be refused with 503.", reason)
        elif start_worker:
            worker = Worker(
                store,
                executor,
                poll_interval=settings.worker_poll_interval_seconds,
                lease_seconds=settings.worker_lease_seconds,
            )
            worker.start()
            logger.info("Execution available: executor=%s", execution.executor)
        app.state.worker = worker
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop(settings.worker_shutdown_grace_seconds)
            # After the worker has stopped, so no model call is still using the client.
            executor_aclose = getattr(executor, "aclose", None)
            if executor_aclose is not None:
                await executor_aclose()
            engine.dispose()

    app = FastAPI(title="AgentLab Evaluation Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.run_store = store
    app.state.executor = executor
    app.state.execution = execution
    app.state.worker = None
    register_error_handlers(app)
    app.include_router(router)
    return app


def _executor_name(executor: object) -> str:
    return getattr(executor, "name", None) or getattr(executor, "__name__", None) or type(executor).__name__


def _configure_logging() -> None:
    """Show the engine's INFO logs (claims, run outcomes) next to uvicorn's own output."""
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False

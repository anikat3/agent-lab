"""Application factory. Run with: uvicorn app.main:create_app --factory"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.errors import register_error_handlers
from app.execution import ScenarioExecutor
from app.migrations import run_migrations
from app.routes import router
from app.run_store import RunStore
from app.worker import Worker, utcnow

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, executor: ScenarioExecutor | None = None) -> FastAPI:
    """Build the app. Without an `executor` no worker starts and queued runs stay queued:
    the engine never reports a run as executed unless something really executed it."""
    settings = settings or get_settings()
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
            logger.warning("No executor configured: the worker is not running and queued runs will stay queued.")
        else:
            worker = Worker(
                store,
                executor,
                poll_interval=settings.worker_poll_interval_seconds,
                lease_seconds=settings.worker_lease_seconds,
            )
            worker.start()
        app.state.worker = worker
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop(settings.worker_shutdown_grace_seconds)
            engine.dispose()

    app = FastAPI(title="AgentLab Evaluation Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.run_store = store
    app.state.worker = None
    register_error_handlers(app)
    app.include_router(router)
    return app

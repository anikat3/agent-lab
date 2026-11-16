"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.errors import register_error_handlers
from app.routes import router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Creates missing tables. Fine while the schema is young; switch to Alembic
        # migrations once a deployed database holds data worth keeping.
        Base.metadata.create_all(engine)
        yield
        engine.dispose()

    app = FastAPI(title="AgentLab Evaluation Engine", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = make_session_factory(engine)
    register_error_handlers(app)
    app.include_router(router)
    return app

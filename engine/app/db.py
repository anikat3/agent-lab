"""SQLAlchemy engine and session setup."""

from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    SQLite has no timezone type and hands back naive datetimes; without this the API
    would emit timestamps without an offset, which the app would read as local time.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC values")
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str, sqlite_busy_timeout_ms: int = 5000) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    engine = create_engine(
        database_url,
        # The API's thread pool and the worker's asyncio.to_thread calls both use pooled
        # connections from threads other than the one that opened them.
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")  # SQLite ignores FKs unless asked
            # WAL: readers never block the writer and the writer never blocks readers, so
            # API polls keep working while the worker writes results. (No-op in memory.)
            cur.execute("PRAGMA journal_mode=WAL")
            # Safe with WAL; only the last transactions can be lost on power failure.
            cur.execute("PRAGMA synchronous=NORMAL")
            # SQLite allows one writer at a time. Instead of failing immediately with
            # "database is locked", wait this long for the other writer to finish.
            cur.execute(f"PRAGMA busy_timeout={int(sqlite_busy_timeout_ms)}")
            cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()

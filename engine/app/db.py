"""SQLAlchemy engine and session setup."""

import sqlite3
import time
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
            # SQLite allows one writer at a time. Instead of failing immediately with
            # "database is locked", wait this long for the other writer to finish.
            # Set first so the statements below also wait.
            cur.execute(f"PRAGMA busy_timeout={int(sqlite_busy_timeout_ms)}")
            cur.execute("PRAGMA foreign_keys=ON")  # SQLite ignores FKs unless asked
            # WAL: readers never block the writer and the writer never blocks readers, so
            # API polls keep working while the worker writes results. (No-op in memory.)
            _enable_wal(cur, sqlite_busy_timeout_ms / 1000)
            # Safe with WAL; only the last transactions can be lost on power failure.
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


def _enable_wal(cursor, budget_seconds: float) -> str:  # type: ignore[no-untyped-def]
    """Switch to WAL mode, retrying while another connection holds the lock.

    When several connections open a brand-new database at once, `PRAGMA journal_mode=WAL`
    can fail with "database is locked" straight away: SQLite does not apply busy_timeout
    to the journal-mode switch. Without this retry, one of two engine processes starting
    together on a fresh database could crash at startup. The mode persists in the file,
    so once any connection has switched it, later calls return immediately.
    """
    deadline = time.monotonic() + max(budget_seconds, 0.1)
    delay = 0.005
    while True:
        try:
            return cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()

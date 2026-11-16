"""SQLite configuration, concurrency behaviour and schema migrations."""

import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.db import Base, make_engine
from app.main import create_app
from app.migrations import MIGRATIONS, run_migrations
from app.repository import create_or_get_run
from app.schemas import LaunchRequest

from .conftest import assert_error, launch, make_api_app, make_settings, queue_run, run_status

# --- connection configuration --------------------------------------------------------


def test_sqlite_pragmas_are_applied(tmp_path, new_engine):
    engine = new_engine(f"sqlite:///{tmp_path / 'p.db'}", sqlite_busy_timeout_ms=1234)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 1234
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL
    engine.dispose()


def test_app_uses_configured_busy_timeout(tmp_path):
    app = create_app(make_settings(tmp_path, sqlite_busy_timeout_ms=2500))
    with TestClient(app):
        with app.state.session_factory() as s:
            assert s.execute(text("PRAGMA busy_timeout")).scalar() == 2500


# --- concurrency ----------------------------------------------------------------------


@pytest.fixture
def raw_conn(tmp_path):  # type: ignore[no-untyped-def]
    """Opens independent sqlite3 connections (standing in for another process); closes them after."""
    opened: list[sqlite3.Connection] = []

    def open_() -> sqlite3.Connection:
        conn = sqlite3.connect(tmp_path / "test.db", isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=0")
        opened.append(conn)
        return conn

    yield open_
    for conn in opened:
        conn.close()


@pytest.fixture
def new_engine():  # type: ignore[no-untyped-def]
    """make_engine() whose engines are disposed after the test."""
    engines = []

    def make(url: str, **kwargs):  # type: ignore[no-untyped-def]
        engine = make_engine(url, **kwargs)
        engines.append(engine)
        return engine

    yield make
    for engine in engines:
        engine.dispose()


def test_reads_are_not_blocked_by_an_open_write_transaction(client, tmp_path, raw_conn):
    ext_id = queue_run(client)
    other = raw_conn()
    other.execute("BEGIN IMMEDIATE")  # hold the write lock, as the worker does mid-write
    other.execute("UPDATE runs SET status='running' WHERE external_run_id=?", (ext_id,))
    try:
        # WAL: the API still reads, and sees the last committed state.
        assert run_status(client, ext_id)["status"] == "queued"
    finally:
        other.execute("COMMIT")
    assert run_status(client, ext_id)["status"] == "running"


def test_writer_waits_for_the_lock_instead_of_failing(client, tmp_path, payload, raw_conn):
    other = raw_conn()
    other.execute("BEGIN IMMEDIATE")
    released = threading.Event()

    def release_soon() -> None:
        time.sleep(0.3)
        other.execute("COMMIT")
        released.set()

    threading.Thread(target=release_soon).start()
    started = time.monotonic()
    resp = launch(client, payload)  # needs the write lock that `other` holds
    assert resp.status_code == 202, resp.text
    assert released.is_set()
    assert time.monotonic() - started >= 0.25


def test_lock_held_past_busy_timeout_returns_503(tmp_path, payload, raw_conn):
    app = make_api_app(tmp_path, sqlite_busy_timeout_ms=50)
    with TestClient(app) as client:
        other = raw_conn()
        other.execute("BEGIN IMMEDIATE")
        try:
            err = assert_error(launch(client, payload), 503, "UNAVAILABLE")
            assert "retry" in err["message"].lower()
        finally:
            other.execute("ROLLBACK")
        # The contract says AgentLab retries 503s; the retry now succeeds.
        assert launch(client, payload).status_code == 202


def test_concurrent_duplicate_launches_create_one_run(app, client, payload):
    """Real threads with their own sessions launching the same run_id at once."""
    req = LaunchRequest.model_validate(payload)
    barrier = threading.Barrier(8)
    ids: list[str] = []

    def go() -> None:
        with app.state.session_factory() as session:
            barrier.wait()
            ids.append(create_or_get_run(session, req).run.external_run_id)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 8 and len(set(ids)) == 1
    with app.state.session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM runs")).scalar() == 1


# --- migrations -----------------------------------------------------------------------


def _versions(engine) -> list[int]:
    with engine.connect() as conn:
        return list(conn.scalars(text("SELECT version FROM schema_migrations ORDER BY version")))


def test_fresh_database_gets_all_migrations(tmp_path, new_engine):
    engine = new_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    assert run_migrations(engine) == [1, 2, 3]
    assert _versions(engine) == [1, 2, 3]
    assert run_migrations(engine) == []  # idempotent


def test_migrated_schema_matches_the_orm_models(tmp_path, new_engine):
    engine = new_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    run_migrations(engine)
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        db_cols = {c["name"]: c["nullable"] for c in insp.get_columns(table.name)}
        orm_cols = {c.name: c.nullable for c in table.columns}
        assert db_cols == orm_cols, table.name
        db_indexes = {i["name"] for i in insp.get_indexes(table.name)}
        assert {i.name for i in table.indexes} <= db_indexes, table.name


def test_milestone_1_database_is_upgraded_without_losing_data(tmp_path, new_engine):
    url = f"sqlite:///{tmp_path / 'm1.db'}"
    engine = new_engine(url)
    run_migrations(engine, MIGRATIONS[:1])  # a database exactly as Milestone 1 left it
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE schema_migrations"))  # M1 had no migrations table
        conn.execute(
            text(
                "INSERT INTO runs (external_run_id, run_id, request_hash, timeout_seconds, agent, suite, status, created_at)"
                " VALUES ('run_old', 'rid', 'h', 300, '{}', '{}', 'queued', '2026-11-01 10:00:00.000000')"
            )
        )

    assert run_migrations(engine) == [1, 2, 3]
    with engine.connect() as conn:
        row = conn.execute(text("SELECT run_id, status, worker_id, lease_expires_at FROM runs")).one()
    assert tuple(row) == ("rid", "queued", None, None)


def test_failed_migration_rolls_back_completely(tmp_path, new_engine):
    engine = new_engine(f"sqlite:///{tmp_path / 'f.db'}")

    def half_then_fail(conn) -> None:  # type: ignore[no-untyped-def]
        conn.execute(text("ALTER TABLE runs ADD COLUMN half_done INTEGER"))
        raise RuntimeError("migration bug")

    with pytest.raises(RuntimeError):
        run_migrations(engine, [*MIGRATIONS, (99, "broken", half_then_fail)])

    # Nothing from the failed call persisted, not even the earlier migrations from the same call.
    assert not inspect(engine).has_table("runs")
    assert run_migrations(engine) == [1, 2, 3]
    assert "half_done" not in {c["name"] for c in inspect(engine).get_columns("runs")}


def test_concurrent_startups_apply_migrations_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'race.db'}"
    barrier = threading.Barrier(4)
    applied: list[list[int]] = []
    errors: list[BaseException] = []

    def start() -> None:
        engine = make_engine(url)
        barrier.wait()
        try:
            applied.append(run_migrations(engine))
        except BaseException as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(applied) == [[], [], [], [1, 2, 3]]


def test_app_startup_runs_migrations(tmp_path, new_engine):
    app = create_app(make_settings(tmp_path))
    with TestClient(app):
        pass
    assert _versions(new_engine(f"sqlite:///{tmp_path / 'test.db'}")) == [1, 2, 3]



def _open_fresh_database_concurrently(tmp_path, races: int, connections: int = 4) -> list[BaseException]:  # type: ignore[no-untyped-def]
    errors: list[BaseException] = []
    for i in range(races):
        url = f"sqlite:///{tmp_path / f'fresh-{i}.db'}"
        barrier = threading.Barrier(connections)

        def connect() -> None:
            engine = make_engine(url)
            barrier.wait()
            try:
                with engine.connect() as conn:
                    assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            except BaseException as e:  # surfaced by the caller's assert
                errors.append(e)
            finally:
                engine.dispose()

        threads = [threading.Thread(target=connect) for _ in range(connections)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    return errors


def test_simultaneous_first_connections_all_get_wal(tmp_path):
    """Regression: `PRAGMA journal_mode=WAL` fails with "database is locked" when several
    connections open a brand-new database at once (SQLite skips busy_timeout for the
    journal-mode switch). This made test_concurrent_startups_apply_migrations_once fail
    intermittently, and could crash one of two engine processes starting together."""
    # 150 races detected the bug in 10/10 runs with the retry removed (40 races: only 4/10).
    assert _open_fresh_database_concurrently(tmp_path, races=150) == []

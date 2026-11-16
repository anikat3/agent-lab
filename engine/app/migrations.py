"""Minimal versioned schema migrations.

Each migration runs once, in order, inside a single transaction together with the row
that records it in `schema_migrations`. SQLite supports transactional DDL, so a failed
migration leaves the schema exactly as it was. On SQLite the transaction is opened with
BEGIN IMMEDIATE, which takes the write lock up front: two processes starting at once
apply migrations one after the other instead of racing.

Migrations describe the schema *as it was* at that version, never the current ORM models,
so a fresh database and an upgraded one end up identical.

This is deliberately small. Move to Alembic when the engine targets Postgres in
production or needs data migrations and downgrades.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Connection,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

logger = logging.getLogger(__name__)

Migration = tuple[int, str, Callable[[Connection], None]]


def _v1_baseline(conn: Connection) -> None:
    """The Milestone 1 schema. Existing M1 databases already have these tables (and are
    skipped by checkfirst); new databases get them created here."""
    md = MetaData()
    tz = DateTime(timezone=True)
    Table(
        "runs",
        md,
        Column("external_run_id", String(64), primary_key=True),
        Column("run_id", String(64), nullable=False, unique=True, index=True),
        Column("request_hash", String(64), nullable=False),
        Column("retry_of_run_id", String(64)),
        Column("timeout_seconds", Integer, nullable=False),
        Column("agent", JSON, nullable=False),
        Column("suite", JSON, nullable=False),
        Column("status", String(16), nullable=False, index=True),
        Column("error_code", String(64)),
        Column("error_message", Text),
        Column("created_at", tz, nullable=False),
        Column("started_at", tz),
        Column("completed_at", tz),
    )
    fk = ForeignKey("runs.external_run_id", ondelete="CASCADE")
    Table(
        "run_scenarios",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("external_run_id", String(64), fk, nullable=False),
        Column("scenario_id", String(64), nullable=False),
        Column("position", Integer, nullable=False),
        Column("name", String(200), nullable=False),
        Column("input_messages", JSON, nullable=False),
        Column("expected_response", Text),
        Column("eval_type", String(32), nullable=False),
        Column("eval_config", JSON, nullable=False),
        UniqueConstraint("external_run_id", "scenario_id"),
    )
    Table(
        "scenario_results",
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("external_run_id", String(64), ForeignKey("runs.external_run_id", ondelete="CASCADE"), nullable=False),
        Column("scenario_id", String(64), nullable=False),
        Column("passed", Boolean, nullable=False),
        Column("quality_score", Float),
        Column("latency_ms", Integer),
        Column("actual_response", Text),
        Column("failure_category", String(64)),
        Column("failure_explanation", Text),
        Column("evaluator_details", JSON),
        Column("created_at", tz, nullable=False),
        UniqueConstraint("external_run_id", "scenario_id"),
    )
    md.create_all(conn, checkfirst=True)


def _v2_worker_lease(conn: Connection) -> None:
    """Ownership lease for running runs (Milestone 2A)."""
    ts = DateTime(timezone=True).compile(dialect=conn.dialect)
    conn.execute(text("ALTER TABLE runs ADD COLUMN worker_id VARCHAR(100)"))
    conn.execute(text(f"ALTER TABLE runs ADD COLUMN lease_expires_at {ts}"))
    # Serves both "next queued run" and "running runs with an expired lease".
    conn.execute(text("CREATE INDEX ix_runs_status_lease ON runs (status, lease_expires_at)"))


MIGRATIONS: list[Migration] = [
    (1, "baseline", _v1_baseline),
    (2, "worker_lease", _v2_worker_lease),
]


def run_migrations(engine: Engine, migrations: list[Migration] = MIGRATIONS) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call."""
    applied_now: list[int] = []
    with engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, applied_at VARCHAR(40) NOT NULL)"
                )
            )
            done = set(conn.scalars(text("SELECT version FROM schema_migrations")))
            for version, name, migrate in migrations:
                if version in done:
                    continue
                migrate(conn)
                conn.execute(
                    text("INSERT INTO schema_migrations (version, name, applied_at) VALUES (:v, :n, :t)"),
                    {"v": version, "n": name, "t": datetime.now(UTC).isoformat()},
                )
                applied_now.append(version)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    if applied_now:
        logger.info("Applied schema migrations %s", applied_now)
    return applied_now

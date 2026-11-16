"""Database tables.

runs              one row per launched run (keyed by the app's run_id for idempotency)
run_scenarios     the scenarios to execute, frozen at launch time
scenario_results  one row per evaluated scenario, written by the execution worker (Milestone 2)
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class Run(Base):
    __tablename__ = "runs"

    external_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The app's run id doubles as the idempotency key; the unique index is what makes
    # concurrent duplicate launches safe.
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    retry_of_run_id: Mapped[str | None] = mapped_column(String(64))
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    agent: Mapped[dict[str, Any]] = mapped_column(JSON)
    suite: Mapped[dict[str, Any]] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    scenarios: Mapped[list["RunScenario"]] = relationship(
        back_populates="run", order_by="RunScenario.position", cascade="all, delete-orphan"
    )
    results: Mapped[list["ScenarioResult"]] = relationship(
        back_populates="run", order_by="ScenarioResult.id", cascade="all, delete-orphan"
    )


class RunScenario(Base):
    __tablename__ = "run_scenarios"
    __table_args__ = (UniqueConstraint("external_run_id", "scenario_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_run_id: Mapped[str] = mapped_column(ForeignKey("runs.external_run_id", ondelete="CASCADE"))
    scenario_id: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)  # order the app sent them in
    name: Mapped[str] = mapped_column(String(200))
    input_messages: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    expected_response: Mapped[str | None] = mapped_column(Text)
    eval_type: Mapped[str] = mapped_column(String(32))
    eval_config: Mapped[dict[str, Any]] = mapped_column(JSON)

    run: Mapped[Run] = relationship(back_populates="scenarios")


class ScenarioResult(Base):
    __tablename__ = "scenario_results"
    # Same key the app upserts on, so a scenario can only ever have one result per run.
    __table_args__ = (UniqueConstraint("external_run_id", "scenario_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_run_id: Mapped[str] = mapped_column(ForeignKey("runs.external_run_id", ondelete="CASCADE"))
    scenario_id: Mapped[str] = mapped_column(String(64))
    passed: Mapped[bool]
    quality_score: Mapped[float | None]
    latency_ms: Mapped[int | None]
    actual_response: Mapped[str | None] = mapped_column(Text)
    failure_category: Mapped[str | None] = mapped_column(String(64))
    failure_explanation: Mapped[str | None] = mapped_column(Text)
    evaluator_details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="results")

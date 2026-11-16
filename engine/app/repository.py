"""Persistence operations for runs. Routes call these; they never build queries themselves."""

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import Run, RunScenario
from app.schemas import LaunchRequest


class DuplicateRunError(Exception):
    """The run_id was already used for a launch with a different payload."""


@dataclass(frozen=True)
class CreateRunOutcome:
    run: Run
    created: bool  # False when an identical earlier launch was replayed


def request_fingerprint(req: LaunchRequest) -> str:
    """SHA-256 of the validated request in canonical JSON (sorted keys, no whitespace)."""
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def new_external_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"


def create_or_get_run(session: Session, req: LaunchRequest) -> CreateRunOutcome:
    """Store a new queued run, or return the existing one if this run_id was seen before.

    Raises DuplicateRunError if the run_id exists with a different payload.
    """
    fingerprint = request_fingerprint(req)

    existing = _find_by_run_id(session, req.run_id)
    if existing is not None:
        return _replay(existing, fingerprint)

    run = Run(
        external_run_id=new_external_run_id(),
        run_id=req.run_id,
        request_hash=fingerprint,
        retry_of_run_id=req.retry_of_run_id,
        timeout_seconds=req.timeout_seconds,
        agent=req.agent.model_dump(mode="json"),
        suite=req.suite.model_dump(mode="json"),
        status="queued",
        scenarios=[
            RunScenario(
                scenario_id=s.id,
                position=i,
                name=s.name,
                input_messages=[m.model_dump(mode="json") for m in s.input_messages],
                expected_response=s.expected_response,
                eval_type=s.eval_type,
                eval_config=s.eval_config,
            )
            for i, s in enumerate(req.scenarios)
        ],
    )
    session.add(run)
    try:
        session.commit()  # run + all scenarios in one transaction
    except IntegrityError:
        # Another request with the same run_id committed between our lookup and insert.
        # The unique index on run_id rejected ours, so the other one wins: replay it.
        session.rollback()
        existing = _find_by_run_id(session, req.run_id)
        if existing is None:
            raise
        return _replay(existing, fingerprint)
    return CreateRunOutcome(run=run, created=True)


def get_run(session: Session, external_run_id: str) -> Run | None:
    stmt = (
        select(Run)
        .where(Run.external_run_id == external_run_id)
        .options(selectinload(Run.scenarios), selectinload(Run.results))
    )
    return session.scalars(stmt).one_or_none()


def _find_by_run_id(session: Session, run_id: str) -> Run | None:
    return session.scalars(select(Run).where(Run.run_id == run_id)).one_or_none()


def _replay(existing: Run, fingerprint: str) -> CreateRunOutcome:
    if existing.request_hash != fingerprint:
        raise DuplicateRunError(existing.run_id)
    return CreateRunOutcome(run=existing, created=False)

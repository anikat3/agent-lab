"""HTTP endpoints. Thin layer: validate, call the repository, shape the response."""

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.orm import Session

from app import __version__
from app.auth import require_api_key
from app.db import get_session
from app.errors import ApiError
from app.eval_config import validate_scenario_configs
from app.models import Run
from app.repository import DuplicateRunError, create_or_get_run, get_run
from app.schemas import (
    HealthResponse,
    LaunchRequest,
    LaunchResponse,
    Progress,
    ResultOut,
    RunError,
    RunStatusResponse,
)

# The contract sends the bearer token on every request, /health included.
router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@router.post("/runs", response_model=LaunchResponse, status_code=status.HTTP_202_ACCEPTED)
def launch_run(
    req: LaunchRequest,
    session: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> LaunchResponse:
    # The contract says the key *is* the run id. A mismatch means a client bug, and
    # silently picking one of the two would make retries behave unpredictably.
    if idempotency_key is not None and idempotency_key != req.run_id:
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key header must equal run_id")

    validate_scenario_configs(req.scenarios)

    try:
        outcome = create_or_get_run(session, req)
    except DuplicateRunError:
        raise ApiError(
            409,
            "DUPLICATE_RUN",
            f"run_id {req.run_id} was already launched with a different payload",
        ) from None
    return LaunchResponse(external_run_id=outcome.run.external_run_id)


@router.get("/runs/{external_run_id}", response_model=RunStatusResponse)
def get_run_status(external_run_id: str, session: Session = Depends(get_session)) -> RunStatusResponse:
    run = get_run(session, external_run_id)
    if run is None:
        raise ApiError(404, "RUN_NOT_FOUND", f"No run with id {external_run_id}")
    return _to_status_response(run)


def _to_status_response(run: Run) -> RunStatusResponse:
    return RunStatusResponse(
        external_run_id=run.external_run_id,
        status=run.status,  # type: ignore[arg-type]  # constrained by the worker that writes it
        started_at=run.started_at,
        completed_at=run.completed_at,
        progress=Progress(completed=len(run.results), total=len(run.scenarios)),
        results=[
            ResultOut(
                scenario_id=r.scenario_id,
                passed=r.passed,
                quality_score=r.quality_score,
                latency_ms=r.latency_ms,
                actual_response=r.actual_response,
                failure_category=r.failure_category,
                failure_explanation=r.failure_explanation,
                evaluator_details=r.evaluator_details,
            )
            for r in run.results
        ],
        error=RunError(code=run.error_code, message=run.error_message or "")
        if run.status == "failed" and run.error_code
        else None,
    )

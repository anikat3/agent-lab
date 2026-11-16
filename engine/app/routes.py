"""HTTP endpoints. Thin layer: validate, call the repository, shape the response."""

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.orm import Session

from app import __version__
from app.auth import require_api_key
from app.db import get_session
from app.errors import ApiError
from app.eval_config import validate_executor_support, validate_scenario_configs
from app.models import Run
from app.repository import DuplicateRunError, create_or_get_run, get_run
from app.schemas import (
    ExecutionInfo,
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
def health(request: Request) -> HealthResponse:
    execution = request.app.state.execution
    return HealthResponse(
        version=__version__,
        execution=ExecutionInfo(available=execution.available, executor=execution.executor, reason=execution.reason),
    )


@router.post("/runs", response_model=LaunchResponse, status_code=status.HTTP_202_ACCEPTED)
def launch_run(
    req: LaunchRequest,
    request: Request,
    session: Session = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> LaunchResponse:
    # Accepting a run nothing will execute would leave it queued forever. 503 is the
    # contract's "retry later" code; the app shows the reason once its retries run out.
    execution = request.app.state.execution
    if not execution.available:
        raise ApiError(503, "UNAVAILABLE", f"Run execution is unavailable: {execution.reason}")

    # The contract says the key *is* the run id. A mismatch means a client bug, and
    # silently picking one of the two would make retries behave unpredictably.
    if idempotency_key is not None and idempotency_key != req.run_id:
        raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key header must equal run_id")

    validate_scenario_configs(req.scenarios)
    validate_executor_support(req, request.app.state.executor)

    try:
        outcome = create_or_get_run(session, req, freeze_judge_models(req, request.app.state.executor))
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


def freeze_judge_models(req: LaunchRequest, executor: object) -> dict[str, str]:
    """scenario_id -> judge model, resolved now so it is stored with the run. An idempotent
    replay returns the run stored the first time, with the models frozen back then."""
    resolve = getattr(executor, "resolve_judge_model", None)
    if resolve is None:
        return {}
    return {s.id: model for s in req.scenarios if (model := resolve(s))}


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

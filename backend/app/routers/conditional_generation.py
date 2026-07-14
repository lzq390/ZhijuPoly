from __future__ import annotations

from time import perf_counter
from typing import Callable

from fastapi import APIRouter, HTTPException, Request, status

from app.models import (
    ConditionalGenerationCandidate,
    ConditionalGenerationJobCreateResponse,
    ConditionalGenerationJobStatusResponse,
    ConditionalGenerationTgRequest,
    ConditionalGenerationTgResponse,
    ConditionalGenerationTgStatusResponse,
)
from app.services.conditional_generation import (
    ConditionalGenerationResult,
    count_attachment_points,
    run_conditional_generation,
    to_model_smiles,
)
from app.services.conditional_generation_runtime import missing_artifact_paths
from app.services.conditional_generation_jobs import ConditionalGenerationJobCapacityError
from app.services.in_memory_jobs import JobGoneError, JobNotFoundError, JobStoreCapacityError
from app.services.structure_2d import generate_2d_svg
from app.utils.exceptions import ModelArtifactError


router = APIRouter(prefix="/api/v1/conditional-generation", tags=["conditional-generation"])
GenerationRunner = Callable[[ConditionalGenerationTgRequest], ConditionalGenerationTgResponse]


def _validate_generation_input(request_body: ConditionalGenerationTgRequest) -> None:
    normalized = to_model_smiles(request_body.smiles)
    if normalized is None:
        raise HTTPException(status_code=422, detail="invalid smiles")
    if count_attachment_points(normalized) < 2:
        raise HTTPException(status_code=422, detail="input polymer must contain at least two attachment points")


def _generation_status_for_app(app) -> ConditionalGenerationTgStatusResponse:
    settings = app.state.settings
    missing = []
    for path in missing_artifact_paths(settings.gen_model_dir_path):
        if path.parent == settings.gen_model_dir_path:
            missing.append(path.name)
        else:
            missing.append(str(path.relative_to(settings.gen_model_dir_path)))
    available = settings.gen_model_enabled and not missing
    if available:
        message = "conditional generation service is available"
    elif not settings.gen_model_enabled:
        message = "conditional generation service is disabled"
    else:
        message = "conditional generation artifacts are missing"
    return ConditionalGenerationTgStatusResponse(
        enabled=settings.gen_model_enabled,
        available=available,
        model_dir=str(settings.gen_model_dir_path),
        missing_artifacts=missing,
        message=message,
    )


def _build_response(
    request_body: ConditionalGenerationTgRequest,
    search_result: ConditionalGenerationResult,
    elapsed_ms: float,
) -> ConditionalGenerationTgResponse:
    results = [
        ConditionalGenerationCandidate(
            rank=candidate.rank,
            generated_smiles=candidate.generated_smiles,
            structure_svg=generate_2d_svg(candidate.generated_smiles),
            predicted_tg=candidate.predicted_tg,
            tg_error=candidate.tg_error,
            similarity_score=candidate.similarity_score,
            sa_score=candidate.sa_score,
        )
        for candidate in search_result.candidates
    ]
    return ConditionalGenerationTgResponse(
        input_smiles=request_body.smiles,
        normalized_input_smiles=search_result.input_smiles_rdkit,
        delta_tg=search_result.delta_tg,
        query_time_ms=elapsed_ms,
        requested_count=search_result.requested_count,
        returned_count=len(results),
        attempts=search_result.attempts,
        filter_counter=search_result.filter_counter,
        results=results,
    )


def _run_generation_response(
    request_body: ConditionalGenerationTgRequest,
    app,
    *,
    timeout_seconds: float | None = None,
) -> ConditionalGenerationTgResponse:
    started_at = perf_counter()
    runner: GenerationRunner | None = getattr(app.state, "conditional_generation_runner", None)
    registry = getattr(app.state, "gpu_runtime_registry", None)
    try:
        if registry is None:
            raise ModelArtifactError("GPU runtime registry is unavailable")
        with registry.inference_session(
            "conditional_generation",
            timeout_seconds=(
                app.state.settings.gpu_async_queue_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
        ) as runtime:
            try:
                if runner is not None:
                    response = runner(request_body)
                else:
                    result = run_conditional_generation(
                        input_smiles=request_body.smiles,
                        delta_tg=request_body.delta_tg,
                        candidate_count=request_body.candidate_count,
                        top_k=request_body.top_k,
                        temperature=request_body.temperature,
                        runtime=runtime,
                    )
            except Exception as exc:
                failure_kind = registry.record_inference_failure("conditional_generation", exc)
                if failure_kind == "oom":
                    raise ModelArtifactError(
                        "conditional generation GPU memory is exhausted; retry after current GPU work finishes"
                    ) from exc
                raise
            registry.record_inference_success("conditional_generation")
    except ModelArtifactError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if runner is not None:
        return response
    return _build_response(request_body, result, (perf_counter() - started_at) * 1000)


@router.get("/tg/status", response_model=ConditionalGenerationTgStatusResponse)
async def get_tg_generation_status(request: Request) -> ConditionalGenerationTgStatusResponse:
    return _generation_status_for_app(request.app)


@router.post(
    "/tg/jobs",
    response_model=ConditionalGenerationJobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_tg_generation_job(
    request_body: ConditionalGenerationTgRequest,
    request: Request,
) -> ConditionalGenerationJobCreateResponse:
    service_status = _generation_status_for_app(request.app)
    if not service_status.enabled:
        raise HTTPException(status_code=503, detail=service_status.message)
    if not service_status.available:
        raise HTTPException(
            status_code=503,
            detail=f"{service_status.message}: {', '.join(service_status.missing_artifacts)}",
        )

    _validate_generation_input(request_body)
    manager = request.app.state.conditional_generation_job_manager
    try:
        job = manager.create_job(
            request_body,
            lambda remaining_seconds: _run_generation_response(
                request_body,
                request.app,
                timeout_seconds=remaining_seconds,
            ),
            timeout_seconds=request.app.state.settings.gpu_async_queue_timeout_seconds,
        )
    except (ConditionalGenerationJobCapacityError, JobStoreCapacityError) as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="conditional generation job executor is unavailable",
        ) from exc
    return ConditionalGenerationJobCreateResponse(job_id=job.job_id, status=job.status)


@router.get("/tg/jobs/{job_id}", response_model=ConditionalGenerationJobStatusResponse)
async def get_tg_generation_job(job_id: str, request: Request) -> ConditionalGenerationJobStatusResponse:
    manager = request.app.state.conditional_generation_job_manager
    try:
        return manager.get_job(job_id)
    except JobGoneError as exc:
        raise HTTPException(status_code=410, detail="Job result is no longer available") from exc
    except (JobNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc

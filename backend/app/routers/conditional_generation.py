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
)
from app.services.conditional_generation import (
    ConditionalGenerationResult,
    count_attachment_points,
    run_conditional_generation,
    to_model_smiles,
)
from app.services.conditional_generation_runtime import TorchConditionalGenerationRuntime
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


def _runtime_for_app(app) -> TorchConditionalGenerationRuntime:
    runtime = getattr(app.state, "conditional_generation_runtime", None)
    if runtime is None:
        settings = app.state.settings
        runtime = TorchConditionalGenerationRuntime(
            model_dir=settings.gen_model_dir_path,
            device=settings.gen_device,
        )
        app.state.conditional_generation_runtime = runtime
    return runtime


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


def _run_generation_response(request_body: ConditionalGenerationTgRequest, app) -> ConditionalGenerationTgResponse:
    started_at = perf_counter()
    runner: GenerationRunner | None = getattr(app.state, "conditional_generation_runner", None)
    if runner is not None:
        return runner(request_body)

    runtime = _runtime_for_app(app)
    try:
        result = run_conditional_generation(
            input_smiles=request_body.smiles,
            delta_tg=request_body.delta_tg,
            candidate_count=request_body.candidate_count,
            top_k=request_body.top_k,
            temperature=request_body.temperature,
            runtime=runtime,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelArtifactError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return _build_response(request_body, result, (perf_counter() - started_at) * 1000)


@router.post(
    "/tg/jobs",
    response_model=ConditionalGenerationJobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_tg_generation_job(
    request_body: ConditionalGenerationTgRequest,
    request: Request,
) -> ConditionalGenerationJobCreateResponse:
    settings = request.app.state.settings
    if not settings.gen_model_enabled:
        raise HTTPException(status_code=503, detail="conditional generation service is disabled")

    _validate_generation_input(request_body)
    manager = request.app.state.conditional_generation_job_manager
    job = manager.create_job(
        request_body,
        lambda: _run_generation_response(request_body, request.app),
    )
    return ConditionalGenerationJobCreateResponse(job_id=job.job_id, status=job.status)


@router.get("/tg/jobs/{job_id}", response_model=ConditionalGenerationJobStatusResponse)
async def get_tg_generation_job(job_id: str, request: Request) -> ConditionalGenerationJobStatusResponse:
    manager = request.app.state.conditional_generation_job_manager
    try:
        return manager.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc

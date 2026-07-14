from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.models import MonomerRetrosynthesisRequest, MonomerRetrosynthesisResponse
from app.services.gpu_runtime_registry import (
    GpuQueueFullError,
    GpuQueueStoppedError,
    GpuQueueTimeoutError,
)
from app.services.monomer_retrosynthesis import predict_monomer_precursors, validate_retrosynthesis_input
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError


router = APIRouter(prefix="/api/v1", tags=["monomer-retrosynthesis"])


def _run_retrosynthesis_inference(app, request_body: MonomerRetrosynthesisRequest) -> MonomerRetrosynthesisResponse:
    settings = app.state.settings
    registry = getattr(app.state, "gpu_runtime_registry", None)
    if registry is None:
        raise ModelArtifactError("GPU runtime registry is unavailable")

    validate_retrosynthesis_input(request_body.smiles)
    with registry.inference_session(
        "retrosynthesis",
        timeout_seconds=settings.gpu_sync_queue_timeout_seconds,
    ) as runtime:
        try:
            response = predict_monomer_precursors(
                request_body.smiles,
                target_role=request_body.target_role,
                num_beams=request_body.num_beams,
                num_return_sequences=request_body.num_return_sequences,
                max_new_tokens=request_body.max_new_tokens,
                model_id=settings.retro_model_id,
                device=settings.retro_device,
                runtime=runtime,
            )
        except InvalidSmilesError:
            raise
        except Exception as exc:
            failure_kind = registry.record_inference_failure("retrosynthesis", exc)
            if failure_kind == "oom":
                raise ModelArtifactError(
                    "retrosynthesis GPU memory is exhausted; retry after current GPU work finishes"
                ) from exc
            raise
        registry.record_inference_success("retrosynthesis")
    return response


@router.post("/monomer-retrosynthesis", response_model=MonomerRetrosynthesisResponse)
async def monomer_retrosynthesis(
    request_body: MonomerRetrosynthesisRequest,
    request: Request,
) -> MonomerRetrosynthesisResponse:
    settings = request.app.state.settings
    if not settings.retro_model_enabled:
        raise HTTPException(status_code=503, detail="retrosynthesis service is disabled")

    try:
        return await run_in_threadpool(
            _run_retrosynthesis_inference,
            request.app,
            request_body,
        )
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelArtifactError as exc:
        raise HTTPException(status_code=503, detail="retrosynthesis service is unavailable") from exc
    except GpuQueueFullError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except (GpuQueueTimeoutError, GpuQueueStoppedError) as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

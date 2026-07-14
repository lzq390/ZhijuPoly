from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.models import (
    PolytaoDescriptorRequest,
    PolytaoDescriptorResponse,
    PolytaoDescriptorValue,
    PolytaoGenerationRequest,
    PolytaoJobCreateResponse,
    PolytaoJobStatusResponse,
    PolytaoStatusResponse,
)
from app.services.in_memory_jobs import (
    BoundedInMemoryJobStore,
    JobGoneError,
    JobNotFoundError,
    JobStoreCapacityError,
)
from app.services.polytao import (
    default_polytao_params,
    descriptor_response_items,
    polytao_descriptor_values,
    polytao_prompt_from_descriptors,
)
from app.services.polytao_jobs import PolytaoJobCapacityError, PolytaoJobManager
from app.services.polytao_runtime import BackendPolytaoRuntime
from app.services.structure_2d import generate_2d_svg


router = APIRouter(prefix="/api/v1/conditional-generation/polytao", tags=["polytao"])


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        client_ip = forwarded_for.split(",", 1)[0].strip()
        if client_ip:
            return client_ip
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _rate_limit_hits_for_app(app) -> dict[str, list[float]]:
    hits = getattr(app.state, "polytao_rate_limit_hits", None)
    if hits is None:
        hits = {}
        app.state.polytao_rate_limit_hits = hits
    return hits


def _enforce_submit_rate_limit(request: Request, settings) -> None:
    limit = settings.polytao_rate_limit_per_ip_per_minute
    window_seconds = settings.polytao_rate_limit_window_seconds
    client_ip = _client_ip(request)
    now = time.monotonic()
    cutoff = now - window_seconds
    hits_by_ip = _rate_limit_hits_for_app(request.app)
    recent_hits = [hit for hit in hits_by_ip.get(client_ip, []) if hit >= cutoff]
    if len(recent_hits) >= limit:
        hits_by_ip[client_ip] = recent_hits
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="PolyTAO submit rate limit exceeded; please wait before submitting another job",
        )
    recent_hits.append(now)
    hits_by_ip[client_ip] = recent_hits


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runtime_for_app(app) -> BackendPolytaoRuntime:
    runtime = getattr(app.state, "polytao_runtime", None)
    if runtime is None:
        settings = app.state.settings
        runtime = BackendPolytaoRuntime(
            model_dir=settings.polytao_model_dir_path,
            device=settings.polytao_device,
            model_id=settings.polytao_model_id,
            model_revision=settings.polytao_model_revision,
        )
        app.state.polytao_runtime = runtime
    return runtime


def _job_manager_for_app(app) -> PolytaoJobManager:
    manager = getattr(app.state, "polytao_job_manager", None)
    if manager is None:
        settings = app.state.settings
        store = getattr(app.state, "in_memory_job_store", None)
        if store is None:
            store = BoundedInMemoryJobStore()
            app.state.in_memory_job_store = store
        manager = PolytaoJobManager(
            max_workers=settings.polytao_job_threads,
            max_active_jobs=settings.polytao_max_active_jobs,
            store=store,
        )
        app.state.polytao_job_manager = manager
    return manager


def _status_response_for_app(app) -> PolytaoStatusResponse:
    settings = app.state.settings
    if not settings.polytao_enabled:
        return PolytaoStatusResponse(
            enabled=False,
            available=False,
            worker_base_url_configured=False,
            worker_mode="backend-in-memory",
            db_configured=None,
            db_ready=None,
            default_params=default_polytao_params(),
            message="PolyTAO backend runtime is disabled",
        )

    runtime = _runtime_for_app(app)
    configuration_probe = runtime.probe()
    configuration_ready = configuration_probe.model_files_ready and configuration_probe.runtime_ready
    registry = getattr(app.state, "gpu_runtime_registry", None)
    registry_shutdown = False
    if registry is not None:
        runtime_state = registry.model_snapshots().get("polytao")
        if runtime_state is None:
            registry_accepting = False
            runtime_ready = False
            runtime_loading = False
            runtime_error = "PolyTAO runtime is not registered"
            fatal_error = True
            load_error_retryable = False
        else:
            registry_accepting = registry.accepting_inferences
            registry_shutdown = not registry_accepting
            runtime_ready = bool(runtime_state["ready"])
            runtime_loading = bool(runtime_state["loading"])
            runtime_error = (
                str(runtime_state["error"])
                if runtime_state["error"] is not None
                else configuration_probe.runtime_error
            )
            fatal_error = bool(runtime_state.get("fatal", False))
            load_error_retryable = bool(runtime_state.get("error_retryable", False))
    else:
        registry_accepting = False
        runtime_ready = False
        runtime_loading = False
        runtime_error = "GPU runtime registry is unavailable"
        fatal_error = True
        load_error_retryable = False

    if runtime_ready:
        runtime_phase = "ready"
    elif runtime_loading:
        runtime_phase = "loading"
    elif runtime_error:
        runtime_phase = "error"
    else:
        runtime_phase = "cold"

    deterministic_runtime_error = bool(runtime_error) and not load_error_retryable
    manager = _job_manager_for_app(app)
    runtime_available = (
        configuration_ready
        and not fatal_error
        and not deterministic_runtime_error
        and manager.accepting
        and registry_accepting
    )
    if not manager.accepting or registry_shutdown:
        message = "PolyTAO backend runtime is shutting down and is not accepting jobs"
    elif not configuration_ready or fatal_error or deterministic_runtime_error:
        message = (
            f"PolyTAO backend runtime is not ready: {runtime_error}"
            if runtime_error
            else "PolyTAO backend runtime is not ready"
        )
    elif runtime_phase == "loading":
        message = "PolyTAO backend runtime is loading"
    elif runtime_phase == "cold":
        message = "PolyTAO backend runtime is available and will load on the first job"
    elif runtime_phase == "error":
        message = (
            f"PolyTAO backend runtime load failed; the next job will retry: {runtime_error}"
            if runtime_error
            else "PolyTAO backend runtime load failed; the next job will retry"
        )
    else:
        message = "PolyTAO backend runtime is ready"

    return PolytaoStatusResponse(
        enabled=True,
        available=runtime_available,
        worker_base_url_configured=False,
        worker_status=runtime_phase,
        worker_mode="backend-in-memory",
        db_configured=None,
        db_ready=None,
        db_error=None,
        runtime_ready=runtime_ready,
        runtime_error=runtime_error,
        active_jobs=manager.active_jobs,
        model_id=settings.polytao_model_id,
        model_revision=settings.polytao_model_revision,
        default_params=default_polytao_params(),
        worker_version=None,
        message=message,
    )


@router.get("/status", response_model=PolytaoStatusResponse)
async def get_polytao_status(request: Request) -> PolytaoStatusResponse:
    return await run_in_threadpool(_status_response_for_app, request.app)


@router.post("/descriptors", response_model=PolytaoDescriptorResponse)
async def calculate_polytao_descriptors(request_body: PolytaoDescriptorRequest) -> PolytaoDescriptorResponse:
    started_at = time.perf_counter()
    try:
        canonical_smiles, descriptors = polytao_descriptor_values(request_body.smiles)
        prompt = polytao_prompt_from_descriptors(descriptors)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PolytaoDescriptorResponse(
        input_smiles=request_body.smiles,
        canonical_smiles=canonical_smiles,
        descriptors=[PolytaoDescriptorValue(**item) for item in descriptor_response_items(descriptors)],
        prompt=prompt,
        query_time_ms=(time.perf_counter() - started_at) * 1000,
    )


@router.post("/jobs", response_model=PolytaoJobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_polytao_job(
    request_body: PolytaoGenerationRequest,
    request: Request,
) -> PolytaoJobCreateResponse:
    settings = request.app.state.settings
    if not settings.polytao_enabled:
        raise HTTPException(status_code=503, detail="PolyTAO backend runtime is disabled")

    try:
        prompt = polytao_prompt_from_descriptors(request_body.descriptors)
        canonical_smiles = None
        if request_body.input_smiles:
            canonical_smiles, _descriptors = polytao_descriptor_values(request_body.input_smiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service_status = await run_in_threadpool(_status_response_for_app, request.app)
    if not service_status.available:
        raise HTTPException(status_code=503, detail=service_status.message)
    _enforce_submit_rate_limit(request, settings)

    registry = getattr(request.app.state, "gpu_runtime_registry", None)

    def run_generation(remaining_seconds: float):
        if registry is None:
            raise RuntimeError("GPU runtime registry is unavailable")
        with registry.inference_session(
            "polytao",
            timeout_seconds=remaining_seconds,
        ) as runtime:
            try:
                result = runtime.generate(
                    prompt=prompt,
                    candidate_count=request_body.candidate_count,
                    temperature=request_body.temperature,
                    top_k=request_body.top_k,
                    top_p=request_body.top_p,
                    max_length=request_body.max_length,
                )
            except Exception as exc:
                failure_kind = registry.record_inference_failure("polytao", exc)
                if failure_kind == "oom":
                    raise RuntimeError(
                        "PolyTAO GPU memory is exhausted; retry after current GPU work finishes"
                    ) from exc
                raise
            registry.record_inference_success("polytao")
        _attach_result_svgs(result.result)
        return result

    try:
        job = _job_manager_for_app(request.app).create_job(
            input_smiles=request_body.input_smiles,
            canonical_smiles=canonical_smiles,
            prompt=prompt,
            requested_count=request_body.candidate_count,
            runner=run_generation,
            timeout_seconds=settings.gpu_async_queue_timeout_seconds,
        )
    except (PolytaoJobCapacityError, JobStoreCapacityError) as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return PolytaoJobCreateResponse(job_id=job.job_id, status=job.status)


@router.get("/jobs/{job_id}", response_model=PolytaoJobStatusResponse)
async def get_polytao_job(job_id: str, request: Request) -> PolytaoJobStatusResponse:
    try:
        return _job_manager_for_app(request.app).get_job(job_id)
    except JobGoneError as exc:
        raise HTTPException(status_code=410, detail="Job result is no longer available") from exc
    except (JobNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


def _attach_result_svgs(result: dict[str, Any]) -> None:
    candidates = result.get("results")
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("structure_svg"):
            continue
        smiles = _optional_str(candidate.get("generated_smiles"))
        if not smiles:
            continue
        candidate["structure_svg"] = generate_2d_svg(smiles)

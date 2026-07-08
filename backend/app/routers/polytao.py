from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status

from app.models import (
    PolytaoDescriptorRequest,
    PolytaoDescriptorResponse,
    PolytaoDescriptorValue,
    PolytaoGenerationRequest,
    PolytaoJobCreateResponse,
    PolytaoJobStatusResponse,
    PolytaoStatusResponse,
)
from app.postgres_database import postgres_connection
from app.services.polytao import (
    default_polytao_params,
    descriptor_response_items,
    polytao_descriptor_values,
    polytao_prompt_from_descriptors,
)
from app.services.polytao_repository import (
    count_active_polytao_jobs_postgres,
    create_polytao_job_postgres,
    get_polytao_job_postgres,
    mark_polytao_job_failed_postgres,
    mark_polytao_job_submitted_postgres,
)
from app.services.polytao_worker_client import (
    PolytaoWorkerClient,
    PolytaoWorkerError,
    PolytaoWorkerSubmitPayload,
)
from app.services.structure_2d import generate_2d_svg


router = APIRouter(prefix="/api/v1/conditional-generation/polytao", tags=["polytao"])
_ACTIVE_CAPACITY_ADVISORY_LOCK_ID = 782184012681


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


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _worker_client_for_app(app) -> PolytaoWorkerClient:
    override = getattr(app.state, "polytao_worker_client", None)
    if override is not None:
        return override

    settings = app.state.settings
    if not settings.polytao_worker_base_url:
        raise HTTPException(status_code=503, detail="PolyTAO worker is not configured")
    client = PolytaoWorkerClient(
        base_url=settings.polytao_worker_base_url,
        timeout_seconds=settings.polytao_worker_timeout_seconds,
    )
    app.state.polytao_worker_client = client
    return client


def _worker_unavailable_message(health: dict[str, Any]) -> str | None:
    worker_status = str(health.get("status") or "unknown")
    db_configured = _optional_bool(health.get("db_configured"))
    runtime_ready = _optional_bool(health.get("runtime_ready"))

    if worker_status != "ok":
        return f"PolyTAO worker health is {worker_status}"
    if db_configured is not True:
        return "PolyTAO worker database is not configured"
    if runtime_ready is not True:
        runtime_error = _optional_str(health.get("runtime_error"))
        if runtime_error:
            return f"PolyTAO worker runtime is not ready: {runtime_error}"
        return "PolyTAO worker runtime is not ready"
    return None


def _status_response_from_health(*, settings, health: dict[str, Any]) -> PolytaoStatusResponse:
    unavailable_message = _worker_unavailable_message(health)
    available = unavailable_message is None
    default_params = health.get("default_params")
    if not isinstance(default_params, dict):
        default_params = default_polytao_params()
    return PolytaoStatusResponse(
        enabled=True,
        available=available,
        worker_base_url_configured=bool(settings.polytao_worker_base_url),
        worker_status=str(health.get("status") or "unknown"),
        worker_mode=_optional_str(health.get("mode")),
        db_configured=_optional_bool(health.get("db_configured")),
        runtime_ready=_optional_bool(health.get("runtime_ready")),
        runtime_error=_optional_str(health.get("runtime_error")),
        active_jobs=_optional_int(health.get("active_jobs")),
        model_id=_optional_str(health.get("model_id")),
        model_revision=_optional_str(health.get("model_revision")),
        default_params=default_params,
        worker_version=_optional_str(health.get("worker_version")),
        message="PolyTAO worker is ready" if available else unavailable_message,
    )


@router.get("/status", response_model=PolytaoStatusResponse)
async def get_polytao_status(request: Request) -> PolytaoStatusResponse:
    settings = request.app.state.settings
    if not settings.polytao_submit_enabled:
        return PolytaoStatusResponse(
            enabled=False,
            available=False,
            worker_base_url_configured=bool(settings.polytao_worker_base_url),
            default_params=default_polytao_params(),
            message="PolyTAO submissions are disabled",
        )

    configured = bool(settings.polytao_worker_base_url) or getattr(request.app.state, "polytao_worker_client", None) is not None
    if not configured:
        return PolytaoStatusResponse(
            enabled=False,
            available=False,
            worker_base_url_configured=False,
            default_params=default_polytao_params(),
            message="PolyTAO worker is disabled until POLYTAO_WORKER_BASE_URL is configured",
        )

    try:
        health = _worker_client_for_app(request.app).get_health()
    except PolytaoWorkerError as exc:
        return PolytaoStatusResponse(
            enabled=True,
            available=False,
            worker_base_url_configured=bool(settings.polytao_worker_base_url),
            worker_status="unreachable",
            default_params=default_polytao_params(),
            message=str(exc),
        )
    return _status_response_from_health(settings=settings, health=health)


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


def _create_pending_job_with_capacity_guard(
    settings,
    *,
    job_id: str,
    request_body: PolytaoGenerationRequest,
    prompt: str,
    canonical_smiles: str | None,
) -> None:
    with postgres_connection(settings.app_postgres_dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ACTIVE_CAPACITY_ADVISORY_LOCK_ID,))
        active_jobs = count_active_polytao_jobs_postgres(connection)
        if active_jobs >= settings.polytao_max_active_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="PolyTAO job capacity is full; please wait for the current job to finish",
            )
        create_polytao_job_postgres(
            connection,
            job_id=job_id,
            input_smiles=request_body.input_smiles,
            canonical_smiles=canonical_smiles,
            descriptor_prompt=prompt,
            descriptors=request_body.descriptors,
            request_data=request_body.model_dump(),
            requested_count=request_body.candidate_count,
        )


@router.post("/jobs", response_model=PolytaoJobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_polytao_job(
    request_body: PolytaoGenerationRequest,
    request: Request,
) -> PolytaoJobCreateResponse:
    settings = request.app.state.settings
    if not settings.polytao_submit_enabled:
        raise HTTPException(status_code=503, detail="PolyTAO submissions are disabled")
    if not settings.polytao_worker_base_url and getattr(request.app.state, "polytao_worker_client", None) is None:
        raise HTTPException(status_code=503, detail="PolyTAO worker is not configured")

    try:
        prompt = polytao_prompt_from_descriptors(request_body.descriptors)
        canonical_smiles = None
        if request_body.input_smiles:
            canonical_smiles, _descriptors = polytao_descriptor_values(request_body.input_smiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        client = _worker_client_for_app(request.app)
        health = client.get_health()
    except PolytaoWorkerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    unavailable_message = _worker_unavailable_message(health)
    if unavailable_message is not None:
        raise HTTPException(status_code=503, detail=unavailable_message)
    _enforce_submit_rate_limit(request, settings)

    job_id = uuid4().hex
    _create_pending_job_with_capacity_guard(
        settings,
        job_id=job_id,
        request_body=request_body,
        prompt=prompt,
        canonical_smiles=canonical_smiles,
    )

    try:
        submission = client.submit_job(
            PolytaoWorkerSubmitPayload(
                job_id=job_id,
                descriptors=request_body.descriptors,
                prompt=prompt,
                input_smiles=request_body.input_smiles,
                canonical_smiles=canonical_smiles,
                candidate_count=request_body.candidate_count,
                temperature=request_body.temperature,
                top_k=request_body.top_k,
                top_p=request_body.top_p,
                max_length=request_body.max_length,
            )
        )
    except PolytaoWorkerError as exc:
        error_message = str(exc)
        with postgres_connection(settings.app_postgres_dsn) as connection:
            mark_polytao_job_failed_postgres(connection, job_id, error_message)
        raise HTTPException(status_code=503, detail=error_message) from exc

    with postgres_connection(settings.app_postgres_dsn) as connection:
        mark_polytao_job_submitted_postgres(
            connection,
            job_id=job_id,
            worker_id=submission.worker_id,
            worker_job_id=submission.worker_job_id,
            worker_version=submission.worker_version,
        )
        job = get_polytao_job_postgres(connection, job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return PolytaoJobCreateResponse(job_id=job["job_id"], status=job["status"])


@router.get("/jobs/{job_id}", response_model=PolytaoJobStatusResponse)
async def get_polytao_job(job_id: str, request: Request) -> PolytaoJobStatusResponse:
    with postgres_connection(request.app.state.settings.app_postgres_dsn) as connection:
        job = get_polytao_job_postgres(connection, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    _attach_structure_svgs(job)
    return PolytaoJobStatusResponse(**job)


def _attach_structure_svgs(job: dict[str, Any]) -> None:
    result = job.get("result")
    if not isinstance(result, dict):
        return
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

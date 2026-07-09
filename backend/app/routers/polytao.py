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
)
from app.services.polytao_jobs import PolytaoJobManager
from app.services.polytao_runtime import BackendPolytaoRuntime, RuntimeProbe
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
        manager = PolytaoJobManager(
            app_postgres_dsn=settings.app_postgres_dsn,
            max_workers=settings.polytao_job_workers,
        )
        app.state.polytao_job_manager = manager
    return manager


def _polytao_db_health(settings) -> tuple[bool, bool, str | None, int | None]:
    if not settings.app_postgres_dsn:
        return False, False, "APP_POSTGRES_DSN is not configured", None
    try:
        with postgres_connection(settings.app_postgres_dsn) as connection:
            row = connection.execute("SELECT to_regclass('generation.polytao_jobs') AS table_name").fetchone()
            table_name = row["table_name"] if row is not None else None
            if table_name is None:
                return True, False, "generation.polytao_jobs table is missing", None
            active_jobs = count_active_polytao_jobs_postgres(connection)
    except Exception as exc:
        return True, False, str(exc)[:240], None
    return True, True, None, active_jobs


def _runtime_unavailable_message(probe: RuntimeProbe) -> str | None:
    if probe.runtime_ready:
        return None
    if probe.runtime_error:
        return f"PolyTAO backend runtime is not ready: {probe.runtime_error}"
    return "PolyTAO backend runtime is not ready"


def _status_response_for_app(app) -> PolytaoStatusResponse:
    settings = app.state.settings
    if not settings.polytao_enabled:
        return PolytaoStatusResponse(
            enabled=False,
            available=False,
            worker_base_url_configured=False,
            default_params=default_polytao_params(),
            message="PolyTAO backend runtime is disabled",
        )

    db_configured, db_ready, db_error, active_jobs = _polytao_db_health(settings)
    probe = _runtime_for_app(app).probe()
    runtime_message = _runtime_unavailable_message(probe)
    if not db_configured:
        message = "PolyTAO backend database is not configured"
    elif not db_ready:
        message = f"PolyTAO backend database is not ready: {db_error}" if db_error else "PolyTAO backend database is not ready"
    elif runtime_message:
        message = runtime_message
    else:
        message = "PolyTAO backend runtime is ready"

    return PolytaoStatusResponse(
        enabled=True,
        available=db_configured and db_ready and probe.runtime_ready,
        worker_base_url_configured=False,
        worker_status=None,
        worker_mode=None,
        db_configured=db_configured,
        db_ready=db_ready,
        db_error=db_error,
        runtime_ready=probe.runtime_ready,
        runtime_error=probe.runtime_error,
        active_jobs=active_jobs,
        model_id=settings.polytao_model_id,
        model_revision=settings.polytao_model_revision,
        default_params=default_polytao_params(),
        worker_version=None,
        message=message,
    )


@router.get("/status", response_model=PolytaoStatusResponse)
async def get_polytao_status(request: Request) -> PolytaoStatusResponse:
    return _status_response_for_app(request.app)


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
    if not settings.polytao_enabled:
        raise HTTPException(status_code=503, detail="PolyTAO backend runtime is disabled")

    try:
        prompt = polytao_prompt_from_descriptors(request_body.descriptors)
        canonical_smiles = None
        if request_body.input_smiles:
            canonical_smiles, _descriptors = polytao_descriptor_values(request_body.input_smiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service_status = _status_response_for_app(request.app)
    if not service_status.available:
        raise HTTPException(status_code=503, detail=service_status.message)
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
        _job_manager_for_app(request.app).submit_job(
            job_id,
            lambda: _runtime_for_app(request.app).generate(
                prompt=prompt,
                candidate_count=request_body.candidate_count,
                temperature=request_body.temperature,
                top_k=request_body.top_k,
                top_p=request_body.top_p,
                max_length=request_body.max_length,
            ),
        )
    except Exception as exc:
        error_message = str(exc)
        with postgres_connection(settings.app_postgres_dsn) as connection:
            mark_polytao_job_failed_postgres(connection, job_id, error_message)
        raise HTTPException(status_code=503, detail=error_message) from exc

    with postgres_connection(settings.app_postgres_dsn) as connection:
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

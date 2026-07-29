from __future__ import annotations

import time
import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from app.models import (
    MonomerMdJobCreateResponse,
    MonomerMdJobPageResponse,
    MonomerMdJobStatus,
    MonomerMdJobStatusResponse,
    MonomerMdProtocol,
    MonomerMdProtocolCatalogResponse,
    MonomerMdRunMode,
    MonomerMdRunRequest,
    MonomerMdStatusResponse,
)
from app.postgres_database import postgres_connection
from app.services.monomer_md_repository import (
    create_monomer_md_job_postgres,
    get_monomer_md_job_postgres,
    get_monomer_md_mode_capacity_postgres,
    list_monomer_md_jobs_postgres,
    mark_monomer_md_artifacts_deleted_postgres,
    mark_monomer_md_job_failed_postgres,
    mark_monomer_md_job_submitted_postgres,
    reconcile_and_get_active_monomer_md_capacity_postgres,
    request_monomer_md_job_cancel_postgres,
)
from app.services.monomer_md_worker_client import (
    MonomerMdWorkerClient,
    MonomerMdWorkerError,
    MonomerMdWorkerSubmitPayload,
)
from app.services.smiles_utils import standardize_smiles
from app.services.monomer_md_protocols import (
    DEMO_PROTOCOL,
    FORMAL_PROTOCOLS,
    estimate_requested_steps,
    formal_protocol_metadata,
    validate_formal_config,
)


router = APIRouter(prefix="/api/v1/monomer-md", tags=["monomer-md"])
_ACTIVE_CAPACITY_ADVISORY_LOCK_ID = 742128925057001
_FORMAL_MAX_RUNNING_JOBS = 1
_FORMAL_MAX_QUEUED_JOBS = 2
_FORMAL_MAX_ACTIVE_JOBS = _FORMAL_MAX_RUNNING_JOBS + _FORMAL_MAX_QUEUED_JOBS


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
    hits = getattr(app.state, "monomer_md_rate_limit_hits", None)
    if hits is None:
        hits = {}
        app.state.monomer_md_rate_limit_hits = hits
    return hits


def _enforce_submit_rate_limit(request: Request, settings) -> None:
    limit = settings.monomer_md_rate_limit_per_ip_per_minute
    window_seconds = settings.monomer_md_rate_limit_window_seconds
    client_ip = _client_ip(request)
    now = time.monotonic()
    cutoff = now - window_seconds
    hits_by_ip = _rate_limit_hits_for_app(request.app)
    recent_hits = [hit for hit in hits_by_ip.get(client_ip, []) if hit >= cutoff]
    if len(recent_hits) >= limit:
        hits_by_ip[client_ip] = recent_hits
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="monomer MD submit rate limit exceeded; please wait before submitting another job",
        )
    recent_hits.append(now)
    hits_by_ip[client_ip] = recent_hits


def _raise_active_job_capacity_error() -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="monomer MD job capacity is full; please wait for the active job to finish",
    )


def _raise_formal_job_capacity_error() -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="formal ByteFF2 monomer MD capacity is full; please wait for the current formal job to finish",
    )


def _create_pending_job_with_capacity_guard(
    settings,
    *,
    job_id: str,
    input_smiles: str,
    canonical_smiles: str,
    requested_steps: int,
    protocol: str = DEMO_PROTOCOL,
    run_mode: str = "demo",
    config_json: dict[str, Any] | None = None,
    components: dict[str, Any] | None = None,
) -> None:
    with postgres_connection(settings.app_postgres_dsn) as connection:
        active_jobs, _ = reconcile_and_get_active_monomer_md_capacity_postgres(
            connection,
            advisory_lock_id=_ACTIVE_CAPACITY_ADVISORY_LOCK_ID,
        )
        if active_jobs >= settings.monomer_md_max_active_jobs:
            _raise_active_job_capacity_error()
        mode_capacity = get_monomer_md_mode_capacity_postgres(connection)
        formal_active = mode_capacity["formal_running"] + mode_capacity["formal_queued"]
        if run_mode == "formal":
            if mode_capacity["demo_active"] > 0 or formal_active >= _FORMAL_MAX_ACTIVE_JOBS:
                _raise_formal_job_capacity_error()
        elif mode_capacity["demo_active"] > 0 or formal_active > 0:
            _raise_active_job_capacity_error()
        create_monomer_md_job_postgres(
            connection,
            job_id=job_id,
            input_smiles=input_smiles,
            canonical_smiles=canonical_smiles,
            requested_steps=requested_steps,
            protocol=protocol,
            run_mode=run_mode,
            config_json=config_json,
            components=components,
        )


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


def _validate_monomer_smiles(smiles: str) -> str:
    if "*" in smiles:
        raise HTTPException(status_code=422, detail="monomer MD requires a single-molecule SMILES without attachment points")
    try:
        canonical_smiles = standardize_smiles(smiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "*" in canonical_smiles:
        raise HTTPException(status_code=422, detail="monomer MD requires a single-molecule SMILES without attachment points")
    return canonical_smiles


def _worker_client_for_app(app) -> MonomerMdWorkerClient:
    override = getattr(app.state, "monomer_md_worker_client", None)
    if override is not None:
        return override

    settings = app.state.settings
    if not settings.monomer_md_worker_base_url:
        raise HTTPException(status_code=503, detail="monomer MD worker is not configured")
    client = MonomerMdWorkerClient(
        base_url=settings.monomer_md_worker_base_url,
        timeout_seconds=settings.monomer_md_worker_timeout_seconds,
    )
    app.state.monomer_md_worker_client = client
    return client


def _worker_unavailable_message(health: dict[str, Any]) -> str | None:
    worker_status = str(health.get("status") or "unknown")
    worker_mode = str(health.get("mode") or "unknown")
    db_configured = _optional_bool(health.get("db_configured"))
    byteff2_root_exists = _optional_bool(health.get("byteff2_root_exists"))
    runtime_ready = _optional_bool(health.get("runtime_ready"))
    accepting_jobs = _optional_bool(health.get("accepting_jobs"))
    draining = _optional_bool(health.get("draining"))

    if worker_status != "ok":
        return f"monomer MD worker health is {worker_status}"
    if db_configured is not True:
        return "monomer MD worker database is not configured"
    if worker_mode == "real" and byteff2_root_exists is not True:
        return "monomer MD worker ByteFF2 root is not available"
    if worker_mode == "real" and runtime_ready is not True:
        runtime_error = _optional_str(health.get("runtime_error"))
        if runtime_error:
            return f"monomer MD worker runtime is not ready: {runtime_error}"
        return "monomer MD worker runtime is not ready"
    if accepting_jobs is False:
        if draining is True:
            return "monomer MD worker is draining for deployment"
        return "monomer MD worker is not accepting jobs"
    return None


def _formal_queue_broker_transition(
    health: dict[str, Any],
    *,
    run_mode: str,
) -> bool:
    active_jobs = _optional_int(health.get("active_jobs"))
    max_active_jobs = _optional_int(health.get("max_active_jobs"))
    broker_error = _optional_str(health.get("gpu_broker_error")) or ""
    return (
        run_mode == "formal"
        and str(health.get("status") or "") == "degraded"
        and str(health.get("mode") or "") == "real"
        and _optional_bool(health.get("db_configured")) is True
        and _optional_bool(health.get("byteff2_root_exists")) is True
        and _optional_bool(health.get("runtime_ready")) is True
        and _optional_bool(health.get("gpu_broker_enabled")) is True
        and _optional_bool(health.get("gpu_broker_ready")) is False
        and _optional_bool(health.get("draining")) is not True
        and active_jobs is not None
        and max_active_jobs is not None
        and 0 < active_jobs < max_active_jobs
        and broker_error.startswith(
            (
                "broker_timeout:",
                "gpu_broker_unavailable:",
                "broker_unavailable:",
            )
        )
    )


def _status_response_from_health(
    *,
    settings,
    health: dict[str, Any],
    database_active_jobs: int | None,
    database_mode_capacity: dict[str, int] | None = None,
    oldest_active_heartbeat_age_seconds: int | None = None,
    database_error: str | None = None,
) -> MonomerMdStatusResponse:
    worker_unavailable_message = _worker_unavailable_message(health)
    health_unavailable_message = worker_unavailable_message
    if worker_unavailable_message in {
        "monomer MD worker is draining for deployment",
        "monomer MD worker is not accepting jobs",
    }:
        health_unavailable_message = None
    available = health_unavailable_message is None and database_error is None
    worker_status = str(health.get("status") or "unknown")
    worker_active_jobs = _optional_int(health.get("active_jobs"))
    accepting_jobs = _optional_bool(health.get("accepting_jobs"))
    if accepting_jobs is None:
        accepting_jobs = True
    draining = _optional_bool(health.get("draining")) is True
    database_busy = (
        database_active_jobs is not None
        and database_active_jobs >= settings.monomer_md_max_active_jobs
    )
    worker_max_active_jobs = _optional_int(health.get("max_active_jobs"))
    worker_busy = (
        worker_active_jobs is not None
        and worker_max_active_jobs is not None
        and worker_active_jobs >= worker_max_active_jobs
    )
    busy = database_busy or worker_busy
    mode_capacity = database_mode_capacity or {
        "demo_active": 0,
        "formal_running": 0,
        "formal_queued": 0,
    }
    formal_active = mode_capacity["formal_running"] + mode_capacity["formal_queued"]
    can_submit = (
        available
        and accepting_jobs
        and not busy
        and mode_capacity["demo_active"] == 0
        and formal_active == 0
    )
    formal_can_submit = (
        available
        and accepting_jobs
        and not database_busy
        and mode_capacity["demo_active"] == 0
        and formal_active < _FORMAL_MAX_ACTIVE_JOBS
    )
    if database_error is not None:
        message = "monomer MD database capacity check failed"
    elif draining:
        message = "monomer MD worker is draining for deployment"
    elif busy:
        message = "monomer MD job capacity is full; please wait for the active job to finish"
    elif can_submit:
        message = "monomer MD worker is ready"
    else:
        message = worker_unavailable_message or "monomer MD worker is not accepting jobs"
    return MonomerMdStatusResponse(
        enabled=True,
        available=available,
        default_steps=settings.monomer_md_default_steps,
        worker_base_url_configured=bool(settings.monomer_md_worker_base_url),
        worker_status=worker_status,
        worker_mode=_optional_str(health.get("mode")),
        db_configured=_optional_bool(health.get("db_configured")),
        byteff2_root_exists=_optional_bool(health.get("byteff2_root_exists")),
        runtime_ready=_optional_bool(health.get("runtime_ready")),
        runtime_error=_optional_str(health.get("runtime_error")),
        active_jobs=worker_active_jobs,
        database_active_jobs=database_active_jobs,
        oldest_active_heartbeat_age_seconds=oldest_active_heartbeat_age_seconds,
        max_active_jobs=settings.monomer_md_max_active_jobs,
        accepting_jobs=accepting_jobs,
        draining=draining,
        busy=busy,
        can_submit=can_submit,
        formal_running_jobs=mode_capacity["formal_running"],
        formal_queued_jobs=mode_capacity["formal_queued"],
        formal_max_running_jobs=_FORMAL_MAX_RUNNING_JOBS,
        formal_max_queued_jobs=_FORMAL_MAX_QUEUED_JOBS,
        formal_can_submit=formal_can_submit,
        protocols=health.get("protocols") if isinstance(health.get("protocols"), dict) else {},
        message=message,
    )


def _database_active_job_count(
    settings,
) -> tuple[int | None, int | None, dict[str, int] | None, str | None]:
    try:
        with postgres_connection(settings.app_postgres_dsn) as connection:
            count, oldest_age = reconcile_and_get_active_monomer_md_capacity_postgres(
                connection,
                advisory_lock_id=_ACTIVE_CAPACITY_ADVISORY_LOCK_ID,
            )
            return count, oldest_age, get_monomer_md_mode_capacity_postgres(connection), None
    except Exception as exc:
        return None, None, None, str(exc)


def _get_job(dsn: str, job_id: str) -> dict[str, Any] | None:
    with postgres_connection(dsn) as connection:
        return get_monomer_md_job_postgres(connection, job_id)


def _list_jobs(
    dsn: str,
    *,
    run_mode: str | None,
    active_only: bool,
    protocol: str | None,
    job_status: str | None,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    with postgres_connection(dsn) as connection:
        return list_monomer_md_jobs_postgres(
            connection,
            run_mode=run_mode,
            active_only=active_only,
            protocol=protocol,
            status=job_status,
            page=page,
            page_size=page_size,
        )


def _request_job_cancel(
    dsn: str,
    job_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    with postgres_connection(dsn) as connection:
        return request_monomer_md_job_cancel_postgres(connection, job_id=job_id)


def _mark_job_failed_after_submit_error(
    dsn: str,
    job_id: str,
    error_message: str,
) -> None:
    with postgres_connection(dsn) as connection:
        mark_monomer_md_job_failed_postgres(
            connection,
            job_id,
            error_message,
            "worker_submit_failed",
        )


def _mark_job_submitted_and_get(
    dsn: str,
    job_id: str,
    worker_id: str,
    worker_job_id: str,
    worker_version: str,
) -> dict[str, Any] | None:
    with postgres_connection(dsn) as connection:
        mark_monomer_md_job_submitted_postgres(
            connection,
            job_id=job_id,
            worker_id=worker_id,
            worker_job_id=worker_job_id,
            worker_version=worker_version,
        )
        return get_monomer_md_job_postgres(connection, job_id)


def _mark_artifacts_deleted_and_get(
    dsn: str,
    job_id: str,
    message: str,
) -> dict[str, Any] | None:
    with postgres_connection(dsn) as connection:
        mark_monomer_md_artifacts_deleted_postgres(
            connection,
            job_id=job_id,
            message=message,
        )
        return get_monomer_md_job_postgres(connection, job_id)


def _formal_protocol_unavailable_message(health: dict[str, Any], protocol: str) -> str | None:
    protocols = health.get("protocols")
    if not isinstance(protocols, dict):
        return "monomer MD worker does not report formal ByteFF2 protocol readiness"
    protocol_health = protocols.get(protocol)
    if not isinstance(protocol_health, dict):
        return f"monomer MD worker does not support ByteFF2 {protocol}"
    if protocol_health.get("supported") is not True:
        return f"monomer MD worker does not support ByteFF2 {protocol}"
    if protocol_health.get("runtime_ready") is not True:
        runtime_error = _optional_str(protocol_health.get("runtime_error"))
        if runtime_error:
            return f"ByteFF2 {protocol} runtime is not ready: {runtime_error}"
        return f"ByteFF2 {protocol} runtime is not ready"
    return None


def _formal_smiles_label(config: dict[str, Any]) -> str:
    return json.dumps(config.get("smiles", {}), ensure_ascii=False, sort_keys=True)


def _job_request_details(request_body: MonomerMdRunRequest) -> dict[str, Any]:
    if request_body.run_mode == "demo" and request_body.protocol == DEMO_PROTOCOL:
        smiles = request_body.smiles or ""
        canonical_smiles = _validate_monomer_smiles(smiles)
        return {
            "protocol": DEMO_PROTOCOL,
            "run_mode": "demo",
            "input_smiles": smiles,
            "canonical_smiles": canonical_smiles,
            "requested_steps": None,
            "config_json": {},
            "components": {},
        }

    try:
        config = validate_formal_config(request_body.config_json or {}, request_body.protocol)
        requested_steps = estimate_requested_steps(request_body.protocol, config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "protocol": request_body.protocol,
        "run_mode": "formal",
        "input_smiles": _formal_smiles_label(config),
        "canonical_smiles": _formal_smiles_label(config),
        "requested_steps": requested_steps,
        "config_json": config,
        "components": {
            "components": config.get("components", {}),
            "smiles": config.get("smiles", {}),
        },
    }


@router.get("/status", response_model=MonomerMdStatusResponse)
async def get_monomer_md_status(request: Request) -> MonomerMdStatusResponse:
    settings = request.app.state.settings
    if not settings.monomer_md_submit_enabled:
        return MonomerMdStatusResponse(
            enabled=False,
            available=False,
            default_steps=settings.monomer_md_default_steps,
            worker_base_url_configured=bool(settings.monomer_md_worker_base_url),
            message="monomer MD submissions are disabled",
        )

    configured = bool(settings.monomer_md_worker_base_url) or getattr(request.app.state, "monomer_md_worker_client", None) is not None
    if not configured:
        return MonomerMdStatusResponse(
            enabled=False,
            available=False,
            default_steps=settings.monomer_md_default_steps,
            worker_base_url_configured=False,
            message="monomer MD worker is disabled until MONOMER_MD_WORKER_BASE_URL is configured",
        )

    try:
        client = _worker_client_for_app(request.app)
        health = await run_in_threadpool(client.get_health)
    except MonomerMdWorkerError as exc:
        return MonomerMdStatusResponse(
            enabled=True,
            available=False,
            default_steps=settings.monomer_md_default_steps,
            worker_base_url_configured=bool(settings.monomer_md_worker_base_url),
            worker_status="unreachable",
            message=str(exc),
        )

    (
        database_active_jobs,
        oldest_active_heartbeat_age_seconds,
        database_mode_capacity,
        database_error,
    ) = await run_in_threadpool(_database_active_job_count, settings)
    return _status_response_from_health(
        settings=settings,
        health=health,
        database_active_jobs=database_active_jobs,
        database_mode_capacity=database_mode_capacity,
        oldest_active_heartbeat_age_seconds=oldest_active_heartbeat_age_seconds,
        database_error=database_error,
    )


@router.get("/protocols", response_model=MonomerMdProtocolCatalogResponse)
async def get_monomer_md_protocols(request: Request) -> MonomerMdProtocolCatalogResponse:
    settings = request.app.state.settings
    protocol_items = formal_protocol_metadata()
    if not settings.monomer_md_submit_enabled:
        return MonomerMdProtocolCatalogResponse(
            enabled=False,
            available=False,
            protocols=protocol_items,
            message="monomer MD submissions are disabled",
        )
    if not settings.monomer_md_worker_base_url and getattr(request.app.state, "monomer_md_worker_client", None) is None:
        return MonomerMdProtocolCatalogResponse(
            enabled=False,
            available=False,
            protocols=protocol_items,
            message="monomer MD worker is not configured",
        )
    try:
        client = _worker_client_for_app(request.app)
        health = await run_in_threadpool(client.get_health)
    except MonomerMdWorkerError as exc:
        return MonomerMdProtocolCatalogResponse(
            enabled=True,
            available=False,
            protocols=protocol_items,
            message=str(exc),
        )

    unavailable_message = _worker_unavailable_message(health)
    worker_available = unavailable_message is None
    health_protocols = health.get("protocols") if isinstance(health.get("protocols"), dict) else {}
    enriched = []
    for item in protocol_items:
        protocol = item["protocol"]
        protocol_health = health_protocols.get(protocol) if isinstance(health_protocols, dict) else None
        if isinstance(protocol_health, dict):
            enriched.append({**item, **protocol_health})
        else:
            enriched.append({**item, "supported": False, "runtime_ready": False, "runtime_error": "worker did not report protocol readiness"})
    return MonomerMdProtocolCatalogResponse(
        enabled=True,
        available=worker_available and any(item.get("runtime_ready") is True for item in enriched),
        protocols=enriched,
        message="formal ByteFF2 protocol catalog is available" if worker_available else unavailable_message or "monomer MD worker is not available",
    )


@router.post("/jobs", response_model=MonomerMdJobCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_monomer_md_job(request_body: MonomerMdRunRequest, request: Request) -> MonomerMdJobCreateResponse:
    settings = request.app.state.settings
    if not settings.monomer_md_submit_enabled:
        raise HTTPException(status_code=503, detail="monomer MD submissions are disabled")
    if not settings.monomer_md_worker_base_url and getattr(request.app.state, "monomer_md_worker_client", None) is None:
        raise HTTPException(status_code=503, detail="monomer MD worker is not configured")

    details = await run_in_threadpool(_job_request_details, request_body)
    try:
        client = _worker_client_for_app(request.app)
        health = await run_in_threadpool(client.get_health)
    except MonomerMdWorkerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    unavailable_message = _worker_unavailable_message(health)
    queued_formal_transition = _formal_queue_broker_transition(
        health,
        run_mode=details["run_mode"],
    )
    capacity_only_rejection = (
        unavailable_message == "monomer MD worker is not accepting jobs"
        and _optional_int(health.get("active_jobs")) is not None
        and _optional_int(health.get("active_jobs")) == _optional_int(health.get("max_active_jobs"))
    )
    if (
        unavailable_message is not None
        and not capacity_only_rejection
        and not queued_formal_transition
    ):
        raise HTTPException(status_code=503, detail=unavailable_message)
    if details["run_mode"] == "formal":
        formal_unavailable_message = _formal_protocol_unavailable_message(health, details["protocol"])
        if formal_unavailable_message is not None:
            raise HTTPException(status_code=503, detail=formal_unavailable_message)
    _enforce_submit_rate_limit(request, settings)

    job_id = uuid4().hex
    requested_steps = details["requested_steps"] or settings.monomer_md_default_steps
    await run_in_threadpool(
        _create_pending_job_with_capacity_guard,
        settings,
        job_id=job_id,
        input_smiles=details["input_smiles"],
        canonical_smiles=details["canonical_smiles"],
        requested_steps=requested_steps,
        protocol=details["protocol"],
        run_mode=details["run_mode"],
        config_json=details["config_json"],
        components=details["components"],
    )

    try:
        submission = await run_in_threadpool(
            client.submit_job,
            MonomerMdWorkerSubmitPayload(
                job_id=job_id,
                smiles=details["input_smiles"],
                canonical_smiles=details["canonical_smiles"],
                steps=requested_steps,
                protocol=details["protocol"],
                run_mode=details["run_mode"],
                config_json=details["config_json"],
            )
        )
    except MonomerMdWorkerError as exc:
        error_message = str(exc)
        await run_in_threadpool(
            _mark_job_failed_after_submit_error,
            settings.app_postgres_dsn,
            job_id,
            error_message,
        )
        raise HTTPException(status_code=503, detail=error_message) from exc

    job = await run_in_threadpool(
        _mark_job_submitted_and_get,
        settings.app_postgres_dsn,
        job_id,
        submission.worker_id,
        submission.worker_job_id,
        submission.worker_version,
    )

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return MonomerMdJobCreateResponse(job_id=job["job_id"], status=job["status"])


@router.get("/jobs/{job_id}", response_model=MonomerMdJobStatusResponse)
async def get_monomer_md_job(job_id: str, request: Request) -> MonomerMdJobStatusResponse:
    job = await run_in_threadpool(
        _get_job,
        request.app.state.settings.app_postgres_dsn,
        job_id,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return MonomerMdJobStatusResponse(**job)


@router.get("/jobs", response_model=MonomerMdJobPageResponse)
async def list_monomer_md_jobs(
    request: Request,
    run_mode: MonomerMdRunMode | None = None,
    active_only: bool = False,
    protocol: MonomerMdProtocol | None = None,
    job_status: MonomerMdJobStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> MonomerMdJobPageResponse:
    items, total = await run_in_threadpool(
        _list_jobs,
        request.app.state.settings.app_postgres_dsn,
        run_mode=run_mode,
        active_only=active_only,
        protocol=protocol,
        job_status=job_status,
        page=page,
        page_size=page_size,
    )
    return MonomerMdJobPageResponse(
        items=[MonomerMdJobStatusResponse(**item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/jobs/{job_id}/cancel", response_model=MonomerMdJobStatusResponse)
async def cancel_monomer_md_job(
    job_id: str,
    request: Request,
    response: Response,
) -> MonomerMdJobStatusResponse:
    settings = request.app.state.settings
    current = await run_in_threadpool(_get_job, settings.app_postgres_dsn, job_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if current["status"] == "pending":
        raise HTTPException(status_code=409, detail="job has not yet been accepted by the monomer MD worker")
    if current["status"] in {"completed", "failed", "cancelled"}:
        response.status_code = status.HTTP_200_OK
        return MonomerMdJobStatusResponse(**current)

    updated, changed = await run_in_threadpool(
        _request_job_cancel,
        settings.app_postgres_dsn,
        job_id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        client = _worker_client_for_app(request.app)
        await run_in_threadpool(client.cancel_job, job_id)
    except MonomerMdWorkerError as exc:
        # The durable cancel_requested state is intentionally retained.  A
        # repeated request can redeliver cancellation, and worker recovery
        # converges an orphaned request after restart.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    response.status_code = status.HTTP_202_ACCEPTED
    if not changed and updated["status"] not in {"cancel_requested", "cancelled"}:
        response.status_code = status.HTTP_200_OK
    refreshed = await run_in_threadpool(_get_job, settings.app_postgres_dsn, job_id)
    return MonomerMdJobStatusResponse(**(refreshed or updated))


@router.delete("/jobs/{job_id}/artifacts", response_model=MonomerMdJobStatusResponse)
async def delete_monomer_md_job_artifacts(job_id: str, request: Request) -> MonomerMdJobStatusResponse:
    settings = request.app.state.settings
    job = await run_in_threadpool(_get_job, settings.app_postgres_dsn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in {"pending", "submitted", "running"}:
        raise HTTPException(status_code=409, detail="cannot delete artifacts for an active monomer MD job")
    if job.get("artifact_deleted_at"):
        return MonomerMdJobStatusResponse(**job)

    try:
        client = _worker_client_for_app(request.app)
        deletion = await run_in_threadpool(client.delete_artifacts, job_id)
    except MonomerMdWorkerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    message = _optional_str(deletion.get("message")) or "artifacts deleted"
    updated = await run_in_threadpool(
        _mark_artifacts_deleted_and_get,
        settings.app_postgres_dsn,
        job_id,
        message,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return MonomerMdJobStatusResponse(**updated)

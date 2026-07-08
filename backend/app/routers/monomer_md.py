from __future__ import annotations

import time
import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status

from app.models import (
    MonomerMdJobCreateResponse,
    MonomerMdJobStatusResponse,
    MonomerMdProtocolCatalogResponse,
    MonomerMdRunRequest,
    MonomerMdStatusResponse,
)
from app.postgres_database import postgres_connection
from app.services.monomer_md_repository import (
    count_active_formal_monomer_md_jobs_postgres,
    count_active_monomer_md_jobs_postgres,
    create_monomer_md_job_postgres,
    get_monomer_md_job_postgres,
    mark_monomer_md_artifacts_deleted_postgres,
    mark_monomer_md_job_failed_postgres,
    mark_monomer_md_job_submitted_postgres,
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
        detail="monomer MD job capacity is full; please wait for the current demo job to finish",
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
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_ACTIVE_CAPACITY_ADVISORY_LOCK_ID,))
        active_jobs = count_active_monomer_md_jobs_postgres(connection)
        if active_jobs >= settings.monomer_md_max_active_jobs:
            _raise_active_job_capacity_error()
        if run_mode == "formal" and count_active_formal_monomer_md_jobs_postgres(connection) >= 1:
            _raise_formal_job_capacity_error()
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
    return None


def _status_response_from_health(
    *,
    settings,
    health: dict[str, Any],
) -> MonomerMdStatusResponse:
    unavailable_message = _worker_unavailable_message(health)
    available = unavailable_message is None
    worker_status = str(health.get("status") or "unknown")
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
        active_jobs=_optional_int(health.get("active_jobs")),
        protocols=health.get("protocols") if isinstance(health.get("protocols"), dict) else {},
        message=(
            "monomer MD worker is ready"
            if available
            else unavailable_message
        ),
    )


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
        health = _worker_client_for_app(request.app).get_health()
    except MonomerMdWorkerError as exc:
        return MonomerMdStatusResponse(
            enabled=True,
            available=False,
            default_steps=settings.monomer_md_default_steps,
            worker_base_url_configured=bool(settings.monomer_md_worker_base_url),
            worker_status="unreachable",
            message=str(exc),
        )

    return _status_response_from_health(settings=settings, health=health)


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
        health = _worker_client_for_app(request.app).get_health()
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

    details = _job_request_details(request_body)
    try:
        client = _worker_client_for_app(request.app)
        health = client.get_health()
    except MonomerMdWorkerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    unavailable_message = _worker_unavailable_message(health)
    if unavailable_message is not None:
        raise HTTPException(status_code=503, detail=unavailable_message)
    if details["run_mode"] == "formal":
        formal_unavailable_message = _formal_protocol_unavailable_message(health, details["protocol"])
        if formal_unavailable_message is not None:
            raise HTTPException(status_code=503, detail=formal_unavailable_message)
    _enforce_submit_rate_limit(request, settings)

    job_id = uuid4().hex
    requested_steps = details["requested_steps"] or settings.monomer_md_default_steps
    _create_pending_job_with_capacity_guard(
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
        submission = client.submit_job(
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
        with postgres_connection(settings.app_postgres_dsn) as connection:
            mark_monomer_md_job_failed_postgres(connection, job_id, error_message, "worker_submit_failed")
        raise HTTPException(status_code=503, detail=error_message) from exc

    with postgres_connection(settings.app_postgres_dsn) as connection:
        mark_monomer_md_job_submitted_postgres(
            connection,
            job_id=job_id,
            worker_id=submission.worker_id,
            worker_job_id=submission.worker_job_id,
            worker_version=submission.worker_version,
        )
        job = get_monomer_md_job_postgres(connection, job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return MonomerMdJobCreateResponse(job_id=job["job_id"], status=job["status"])


@router.get("/jobs/{job_id}", response_model=MonomerMdJobStatusResponse)
async def get_monomer_md_job(job_id: str, request: Request) -> MonomerMdJobStatusResponse:
    with postgres_connection(request.app.state.settings.app_postgres_dsn) as connection:
        job = get_monomer_md_job_postgres(connection, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return MonomerMdJobStatusResponse(**job)


@router.delete("/jobs/{job_id}/artifacts", response_model=MonomerMdJobStatusResponse)
async def delete_monomer_md_job_artifacts(job_id: str, request: Request) -> MonomerMdJobStatusResponse:
    settings = request.app.state.settings
    with postgres_connection(settings.app_postgres_dsn) as connection:
        job = get_monomer_md_job_postgres(connection, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] in {"pending", "submitted", "running"}:
        raise HTTPException(status_code=409, detail="cannot delete artifacts for an active monomer MD job")
    if job.get("artifact_deleted_at"):
        return MonomerMdJobStatusResponse(**job)

    try:
        deletion = _worker_client_for_app(request.app).delete_artifacts(job_id)
    except MonomerMdWorkerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    message = _optional_str(deletion.get("message")) or "artifacts deleted"
    with postgres_connection(settings.app_postgres_dsn) as connection:
        mark_monomer_md_artifacts_deleted_postgres(connection, job_id=job_id, message=message)
        updated = get_monomer_md_job_postgres(connection, job_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return MonomerMdJobStatusResponse(**updated)

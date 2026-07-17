from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.postgres_database import postgres_connection
from app.services.deployment_control import aggregate_active_jobs, get_drain_state
from app.services.deployment_monomer_md_canary import (
    DeploymentMonomerMdCanaryBusy,
    DeploymentMonomerMdCanaryError,
    cleanup_canary,
    submit_canary,
    validate_completed_canary,
)
from app.services.monomer_md_worker_client import MonomerMdWorkerClient


# Nginx only proxies /api and /health. This operational endpoint is intended for
# a loopback/container-network probe by the release controller, not public traffic.
router = APIRouter(prefix="/internal/deployment", tags=["deployment-internal"])


class DeploymentMonomerMdCanaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]{7,127}$",
    )
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_byteff2_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    capability: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class DeploymentMonomerMdCanaryContinuationRequest(
    DeploymentMonomerMdCanaryRequest
):
    capability: str = Field(pattern=r"^[0-9a-f]{64}$")


def _require_direct_loopback(request: Request) -> None:
    client_host = request.client.host if request.client is not None else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    forwarded_headers = (
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
    )
    if not is_loopback or any(
        name in request.headers for name in forwarded_headers
    ):
        raise HTTPException(
            status_code=403,
            detail="deployment canary control is loopback-only",
        )


def _require_running_source(source_sha: str) -> None:
    if os.getenv("BUILD_REVISION", "") != source_sha:
        raise HTTPException(
            status_code=409,
            detail="deployment canary source differs from the running Backend",
        )


def _canary_state_directory(request: Request) -> Path:
    override = getattr(
        request.app.state,
        "deployment_monomer_md_canary_state_dir",
        None,
    )
    if override is not None:
        return Path(override)
    raw_path = os.getenv("MONOMER_MD_CANARY_STATE_DIR", "").strip()
    if not raw_path:
        raise HTTPException(
            status_code=503,
            detail="deployment canary state directory is not configured",
        )
    return Path(raw_path)


def _canary_worker_client(request: Request) -> MonomerMdWorkerClient:
    override = getattr(request.app.state, "monomer_md_worker_client", None)
    if override is not None:
        return override
    settings = request.app.state.settings
    if not settings.monomer_md_worker_base_url:
        raise HTTPException(
            status_code=503,
            detail="monomer MD Worker is not configured",
        )
    return MonomerMdWorkerClient(
        base_url=settings.monomer_md_worker_base_url,
        timeout_seconds=settings.monomer_md_worker_timeout_seconds,
    )


async def _run_canary_operation(operation, *, busy_detail: str):
    try:
        return await run_in_threadpool(operation)
    except DeploymentMonomerMdCanaryBusy as exc:
        raise HTTPException(status_code=409, detail=busy_detail) from exc
    except DeploymentMonomerMdCanaryError as exc:
        raise HTTPException(
            status_code=409,
            detail="deployment canary ownership or recovery evidence is invalid",
        ) from exc


@router.get("/status", include_in_schema=False)
def deployment_status(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    connection_factory = getattr(
        request.app.state,
        "deployment_control_connection_factory",
        postgres_connection,
    )
    try:
        with connection_factory(settings.app_postgres_dsn) as connection:
            drain = get_drain_state(connection)
            jobs = aggregate_active_jobs(connection, request.app)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="deployment state is unavailable") from exc

    return {
        "active_jobs_schema_version": 1,
        "drain": {
            "enabled": drain.enabled,
            "reason": drain.reason,
            "release_sha": drain.release_sha,
            "activated_at": drain.activated_at.isoformat() if drain.activated_at else None,
            "activated_by": drain.activated_by,
            "updated_at": drain.updated_at.isoformat(),
        },
        "active_jobs": jobs.counts,
        "active_total": jobs.total,
    }


@router.post("/monomer-md-canary/submit", include_in_schema=False)
async def submit_deployment_monomer_md_canary(
    body: DeploymentMonomerMdCanaryRequest,
    request: Request,
) -> dict[str, Any]:
    _require_direct_loopback(request)
    _require_running_source(body.source_sha)
    settings = request.app.state.settings
    connection_factory = getattr(
        request.app.state,
        "postgres_connection_factory",
        postgres_connection,
    )
    worker_client = _canary_worker_client(request)
    return await _run_canary_operation(
        lambda: submit_canary(
            dsn=settings.app_postgres_dsn,
            state_directory=_canary_state_directory(request),
            operation_id=body.operation_id,
            source_sha=body.source_sha,
            expected_byteff2_commit=body.expected_byteff2_commit,
            max_active_jobs=settings.monomer_md_max_active_jobs,
            worker_client=worker_client,
            capability=body.capability,
            connection_factory=connection_factory,
        ),
        busy_detail="deployment canary is waiting for exact Monomer-MD capacity",
    )


@router.post("/monomer-md-canary/validated", include_in_schema=False)
async def validate_deployment_monomer_md_canary(
    body: DeploymentMonomerMdCanaryContinuationRequest,
    request: Request,
) -> dict[str, Any]:
    _require_direct_loopback(request)
    _require_running_source(body.source_sha)
    settings = request.app.state.settings
    connection_factory = getattr(
        request.app.state,
        "postgres_connection_factory",
        postgres_connection,
    )
    return await _run_canary_operation(
        lambda: validate_completed_canary(
            dsn=settings.app_postgres_dsn,
            state_directory=_canary_state_directory(request),
            operation_id=body.operation_id,
            source_sha=body.source_sha,
            expected_byteff2_commit=body.expected_byteff2_commit,
            capability=body.capability,
            connection_factory=connection_factory,
        ),
        busy_detail="deployment canary has not reached its reviewed terminal result",
    )


@router.post("/monomer-md-canary/cleanup", include_in_schema=False)
async def cleanup_deployment_monomer_md_canary(
    body: DeploymentMonomerMdCanaryContinuationRequest,
    request: Request,
) -> dict[str, Any]:
    _require_direct_loopback(request)
    _require_running_source(body.source_sha)
    settings = request.app.state.settings
    connection_factory = getattr(
        request.app.state,
        "postgres_connection_factory",
        postgres_connection,
    )
    worker_client = _canary_worker_client(request)
    return await _run_canary_operation(
        lambda: cleanup_canary(
            dsn=settings.app_postgres_dsn,
            state_directory=_canary_state_directory(request),
            operation_id=body.operation_id,
            source_sha=body.source_sha,
            expected_byteff2_commit=body.expected_byteff2_commit,
            capability=body.capability,
            worker_client=worker_client,
            connection_factory=connection_factory,
        ),
        busy_detail="deployment canary Worker artifacts are still active",
    )

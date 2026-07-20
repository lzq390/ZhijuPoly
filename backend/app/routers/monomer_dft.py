from __future__ import annotations

import asyncio
import logging
import re
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute

from app.postgres_database import PostgresUnavailableError

from app.services.monomer_dft_models import (
    MonomerDftArtifactDeleteResponse,
    MonomerDftCalculationType,
    MonomerDftCapabilitiesResponse,
    MonomerDftJobListResponse,
    MonomerDftJobResponse,
    MonomerDftJobStatus,
    MonomerDftRunRequest,
    MonomerDftStatusResponse,
)
from app.services.monomer_dft_protocol import (
    MAX_HEAVY_ATOMS,
    MAX_HESSIAN_ATOMS,
    MAX_TOTAL_ATOMS,
    MODEL_ELEMENTS,
    MonomerDftRequestError,
    calculation_request_sha256,
    prepare_monomer_dft_request,
)
from app.services.monomer_dft_download_proxy import (
    MonomerDftDownloadProxyError,
    VerifiedMonomerDftFileResponse,
)
from app.services.monomer_dft_repository import (
    MonomerDftArtifactNotFound,
    MonomerDftCapacityError,
    MonomerDftIdempotencyConflict,
    MonomerDftJobNotFound,
    MonomerDftJobStateConflict,
    MonomerDftRepository,
    MAX_PAGE,
    TERMINAL_STATUSES,
)
from app.services.monomer_dft_worker_client import MonomerDftWorkerClient, MonomerDftWorkerError


logger = logging.getLogger(__name__)

MONOMER_DFT_STABLE_ERROR_CODES = frozenset(
    {
        "submission_disabled",
        "schema_not_ready",
        "worker_socket_not_configured",
        "worker_unavailable",
        "gpu_capacity_unavailable",
        "gpu_lease_lost",
        "gpu_runtime_unhealthy",
        "charge_out_of_range",
        "unsupported_isotope",
        "artifact_integrity_mismatch",
        "artifact_bundle_invalid",
        "artifact_size_out_of_contract",
        "download_capacity_full",
        "artifact_deletion_pending",
        "journal_upgrade_missing_enqueue_sequence",
    }
)

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_JOB_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_ELEMENT_SYMBOLS = {
    1: "H",
    5: "B",
    6: "C",
    7: "N",
    8: "O",
    9: "F",
    14: "Si",
    15: "P",
    16: "S",
    17: "Cl",
    33: "As",
    34: "Se",
    35: "Br",
    46: "Pd",
    53: "I",
}
_MODEL_METADATA: dict[str, tuple[str, str]] = {
    "aimnet2": ("AIMNet2", "General-purpose AIMNet2 model for organic molecules."),
    "aimnet2-2025": (
        "AIMNet2 2025",
        "Recommended model for new calculations with a B97-3c reference.",
    ),
    "aimnet2-b973c": (
        "AIMNet2 B97-3c",
        "Legacy B97-3c model retained for reproducing earlier calculations.",
    ),
    "aimnet2-nse": (
        "AIMNet2 NSE",
        "Charge- and multiplicity-aware model for neutral, ionic, and open-shell molecules.",
    ),
    "aimnet2-pd": (
        "AIMNet2 Pd",
        "Palladium-capable model (including Pd, excluding As) referenced to B97-3c with CPCM(THF).",
    ),
    "aimnet2-rxn": (
        "AIMNet2 RXN",
        "Neutral H/C/N/O model for reaction paths and reactive structures.",
    ),
}


def _repository(request: Request) -> MonomerDftRepository:
    return request.app.state.monomer_dft_repository


def _worker(request: Request) -> MonomerDftWorkerClient:
    worker = request.app.state.monomer_dft_worker_client
    if worker is None:
        raise _public_error(
            503,
            code="worker_unavailable",
            message="monomer DFT worker is unavailable",
            retryable=True,
        )
    return worker


def _kick_reconciler(request: Request) -> None:
    reconciler = request.app.state.monomer_dft_reconciler
    if reconciler is not None:
        reconciler.kick()


def _persisted_artifact_count(job: dict[str, Any]) -> int:
    artifacts = job.get("artifacts")
    if not isinstance(artifacts, list):
        return 0
    return len(
        {
            str(artifact["artifact_id"])
            for artifact in artifacts
            if isinstance(artifact, dict)
            and isinstance(artifact.get("artifact_id"), str)
            and artifact["artifact_id"]
        }
    )


class MonomerDftPublicError(Exception):
    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers


class MonomerDftRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def guarded_route_handler(request: Request):
            try:
                return await route_handler(request)
            except (MonomerDftPublicError, HTTPException, RequestValidationError):
                raise
            except PostgresUnavailableError as exc:
                raise MonomerDftPublicError(
                    503,
                    code="database_unavailable",
                    message="monomer DFT database is unavailable",
                    retryable=True,
                ) from exc
            except Exception as exc:
                logger.exception("Unexpected monomer DFT backend error")
                raise MonomerDftPublicError(
                    500,
                    code="internal_error",
                    message="monomer DFT request failed",
                    retryable=False,
                ) from exc

        return guarded_route_handler


router = APIRouter(
    prefix="/api/v1/monomer-dft",
    tags=["monomer-dft"],
    route_class=MonomerDftRoute,
)


async def monomer_dft_public_error_handler(
    _request: Request,
    exc: MonomerDftPublicError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "details": exc.details,
        },
        headers=exc.headers,
    )


def _public_error(
    status_code: int,
    *,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> MonomerDftPublicError:
    return MonomerDftPublicError(
        status_code,
        code=code,
        message=message,
        retryable=retryable,
        details=details,
        headers=headers,
    )


def _validate_job_id(job_id: str) -> str:
    if _JOB_ID.fullmatch(job_id) is None:
        raise _public_error(404, code="job_not_found", message="DFT job not found")
    return job_id.lower()


async def _worker_health(request: Request) -> tuple[dict[str, Any], bool]:
    settings = request.app.state.settings
    if not settings.monomer_dft_submit_enabled or not settings.monomer_dft_worker_uds:
        return {}, False
    try:
        health = await _worker(request).health()
    except MonomerDftWorkerError:
        return {}, False
    available = (
        health.get("status") == "ok"
        and health.get("runtime_ready") is True
        and health.get("draining") is not True
        and health.get("accepting_jobs") is True
    )
    return health, available


async def _schema_ready(request: Request) -> bool:
    repository = _repository(request)
    checker = getattr(repository, "schema_ready", None)
    if checker is None:
        # Lightweight route test doubles predate the governed schema probe.
        # Production always installs ``MonomerDftRepository``.
        return True
    return bool(await asyncio.to_thread(checker))


async def _require_schema_ready(request: Request) -> None:
    if await _schema_ready(request):
        return
    raise _public_error(
        503,
        code="schema_not_ready",
        message="monomer DFT schema is not ready",
        retryable=True,
        headers={"Retry-After": "5"},
    )


def _public_job(repository: MonomerDftRepository, job: dict[str, Any], *, replay: bool = False) -> MonomerDftJobResponse:
    return MonomerDftJobResponse.model_validate(
        repository.public_job(job, idempotent_replay=replay)
    )


async def _find_idempotent_job(
    repository: MonomerDftRepository,
    *,
    idempotency_key: str,
    request_sha256: str,
) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(
            repository.find_idempotent_job,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
    except MonomerDftIdempotencyConflict as exc:
        raise _public_error(409, code="idempotency_conflict", message=str(exc)) from exc


@router.get("/status", response_model=MonomerDftStatusResponse)
async def get_status(request: Request) -> MonomerDftStatusResponse:
    settings = request.app.state.settings
    enabled = bool(settings.monomer_dft_submit_enabled)
    schema_ready = await _schema_ready(request)
    health: dict[str, Any] = {}
    worker_available = False
    active_jobs = 0
    if schema_ready:
        health, worker_available = await _worker_health(request)
        try:
            active_jobs = await asyncio.to_thread(_repository(request).count_active_jobs)
        except Exception:
            active_jobs = 0
            worker_available = False
    available = (
        schema_ready
        and enabled
        and worker_available
        and bool(settings.monomer_dft_worker_uds)
    )
    worker_status = str(health.get("status") or "unavailable")
    if not schema_ready:
        message = "monomer DFT schema is not ready"
    elif not enabled:
        message = "monomer DFT submission is disabled"
    elif not settings.monomer_dft_worker_uds:
        message = "monomer DFT Unix socket is not configured"
    elif available:
        message = "monomer DFT worker is ready"
    elif health.get("draining") is True:
        message = "monomer DFT worker is draining"
    elif health.get("runtime_ready") is not True:
        message = "monomer DFT runtime is not ready"
    else:
        message = "monomer DFT worker is unavailable"
    return MonomerDftStatusResponse(
        enabled=enabled,
        available=available,
        schema_ready=schema_ready,
        worker_status=worker_status,
        runtime_ready=health.get("runtime_ready") if isinstance(health.get("runtime_ready"), bool) else None,
        draining=health.get("draining") if isinstance(health.get("draining"), bool) else None,
        active_jobs=active_jobs,
        max_active_jobs=settings.monomer_dft_max_active_jobs,
        message=message,
    )


@router.get("/capabilities", response_model=MonomerDftCapabilitiesResponse)
async def get_capabilities(request: Request) -> MonomerDftCapabilitiesResponse:
    settings = request.app.state.settings
    schema_ready = await _schema_ready(request)
    health: dict[str, Any] = {}
    health_available = False
    worker_capabilities: dict[str, Any] = {}
    if schema_ready:
        health, health_available = await _worker_health(request)
    if (
        schema_ready
        and settings.monomer_dft_submit_enabled
        and settings.monomer_dft_worker_uds
    ):
        try:
            worker_capabilities = await _worker(request).capabilities()
        except MonomerDftWorkerError:
            pass
    enabled = bool(settings.monomer_dft_submit_enabled)
    available = (
        schema_ready
        and enabled
        and bool(settings.monomer_dft_worker_uds)
        and health_available
    )

    worker_models = {
        str(item.get("alias") or item.get("id")): item
        for item in worker_capabilities.get("models", [])
        if isinstance(item, dict) and isinstance(item.get("alias") or item.get("id"), str)
    }
    calculation_types = ["single_point", "optimization"]
    properties = ["energy", "charges", "forces", "hessian", "frequencies"]
    models: list[dict[str, Any]] = []
    for model_id, elements in MODEL_ELEMENTS.items():
        worker_model = worker_models.get(model_id, {})
        label, description = _MODEL_METADATA[model_id]
        deprecated = model_id == "aimnet2-b973c"
        model_available = available and worker_model.get("loaded") is True
        models.append(
            {
                "id": model_id,
                "label": str(worker_model.get("label") or label),
                "description": description,
                "available": model_available,
                "is_default": model_id == "aimnet2",
                "deprecated": deprecated,
                "deprecation_message": (
                    "Legacy compatibility alias; prefer aimnet2-2025 for new B97-3c work."
                    if deprecated
                    else None
                ),
                "supported_calculation_types": calculation_types,
                "supported_properties": properties,
                "supported_elements": [_ELEMENT_SYMBOLS[number] for number in sorted(elements)],
                "supports_spin": model_id == "aimnet2-nse",
                "charge_min": 0 if model_id == "aimnet2-rxn" else -5,
                "charge_max": 0 if model_id == "aimnet2-rxn" else 5,
            }
        )

    queue = worker_capabilities.get("queue")
    worker_proxy = {
        "schema_version": worker_capabilities.get("schema_version"),
        "calculation_types": worker_capabilities.get("calculation_types", []),
        "properties": worker_capabilities.get("properties", []),
        "input_limits": worker_capabilities.get("input_limits", {}),
        "queue": queue if isinstance(queue, dict) else {},
        "worker_status": health.get("status", "unavailable"),
        "runtime_ready": health.get("runtime_ready"),
        "draining": health.get("draining"),
    }
    return MonomerDftCapabilitiesResponse.model_validate(
        {
            "enabled": enabled,
            "available": available,
            "schema_ready": schema_ready,
            "calculation_types": calculation_types,
            "properties": properties,
            "default_model": "aimnet2",
            "models": models,
            "defaults": {
                "conformer": {"seed": 1, "max_iterations": 500},
                "single_point": {"properties": ["energy", "charges", "forces"]},
                "optimization": {
                    "fmax_eV_per_A": 0.01,
                    "max_steps": 50,
                    "post_optimization_properties": [],
                },
            },
            "limits": {
                "max_atoms": MAX_TOTAL_ATOMS,
                "max_heavy_atoms": MAX_HEAVY_ATOMS,
                "max_hessian_atoms": MAX_HESSIAN_ATOMS,
                "min_optimization_steps": 10,
                "max_optimization_steps": 50,
                "max_concurrent_jobs": 1,
                "max_queued_jobs": 8,
                "max_active_jobs": settings.monomer_dft_max_active_jobs,
            },
            "worker": worker_proxy,
            "message": (
                "monomer DFT worker is ready"
                if available
                else "monomer DFT schema is not ready"
                if not schema_ready
                else "monomer DFT worker is unavailable"
            ),
        }
    )


@router.get("/jobs", response_model=MonomerDftJobListResponse)
async def list_jobs(
    request: Request,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[MonomerDftJobStatus | None, Query(alias="status")] = None,
    calculation_type: Annotated[MonomerDftCalculationType | None, Query()] = None,
) -> MonomerDftJobListResponse:
    await _require_schema_ready(request)
    repository = _repository(request)
    result = await asyncio.to_thread(
        repository.list_jobs,
        page=page,
        page_size=page_size,
        status=status_filter,
        calculation_type=calculation_type,
    )
    return MonomerDftJobListResponse(
        items=[_public_job(repository, item) for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
    )


@router.post("/jobs", response_model=MonomerDftJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    request: Request,
    scientific_request: MonomerDftRunRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> MonomerDftJobResponse:
    await _require_schema_ready(request)
    settings = request.app.state.settings
    if _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise _public_error(
            422,
            code="invalid_idempotency_key",
            message="Idempotency-Key must be 8-128 URL-safe characters",
        )
    repository = _repository(request)
    normalized_request = scientific_request.model_dump(mode="json")
    request_sha256 = calculation_request_sha256(normalized_request)

    async def replay_if_present() -> MonomerDftJobResponse | None:
        job = await _find_idempotent_job(
            repository,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        return _public_job(repository, job, replay=True) if job is not None else None

    replay = await replay_if_present()
    if replay is not None:
        return replay
    try:
        prepared = await anyio.to_thread.run_sync(
            prepare_monomer_dft_request,
            scientific_request,
            limiter=request.app.state.monomer_dft_validation_limiter,
        )
    except MonomerDftRequestError as exc:
        replay = await replay_if_present()
        if replay is not None:
            return replay
        raise _public_error(422, code=exc.code, message=str(exc)) from exc
    except RuntimeError as exc:
        replay = await replay_if_present()
        if replay is not None:
            return replay
        raise _public_error(
            503,
            code="scientific_validation_unavailable",
            message="molecular request validation is unavailable",
            retryable=True,
        ) from exc
    except Exception:
        replay = await replay_if_present()
        if replay is not None:
            return replay
        raise
    if prepared.request_sha256 != request_sha256:
        replay = await replay_if_present()
        if replay is not None:
            return replay
        raise _public_error(
            500,
            code="request_normalization_mismatch",
            message="monomer DFT request normalization failed",
        )

    replay = await replay_if_present()
    if replay is not None:
        return replay

    if not settings.monomer_dft_submit_enabled:
        replay = await replay_if_present()
        if replay is not None:
            return replay
        raise _public_error(503, code="submission_disabled", message="monomer DFT submission is disabled")
    if not settings.monomer_dft_worker_uds:
        replay = await replay_if_present()
        if replay is not None:
            return replay
        raise _public_error(
            503,
            code="worker_socket_not_configured",
            message="monomer DFT Unix socket is not configured",
        )
    health, available = await _worker_health(request)
    if not available:
        replay = await replay_if_present()
        if replay is not None:
            return replay
        queued_jobs = health.get("queued_jobs")
        max_queued_jobs = health.get("max_queued_jobs")
        queue_is_full = (
            isinstance(queued_jobs, int)
            and not isinstance(queued_jobs, bool)
            and isinstance(max_queued_jobs, int)
            and not isinstance(max_queued_jobs, bool)
            and max_queued_jobs >= 0
            and queued_jobs >= max_queued_jobs
        )
        if (
            health.get("accepting_jobs") is False
            and health.get("draining") is not True
            and health.get("recovering") is not True
            and health.get("runtime_ready") is True
            and queue_is_full
        ):
            raise _public_error(
                429,
                code="worker_capacity_full",
                message="monomer DFT worker capacity is full",
                retryable=True,
                headers={"Retry-After": "5"},
            )
        raise _public_error(
            503,
            code="worker_unavailable",
            message="monomer DFT worker is unavailable",
            retryable=True,
        )

    try:
        created = await asyncio.to_thread(
            repository.create_job,
            prepared,
            idempotency_key=idempotency_key,
            max_active_jobs=settings.monomer_dft_max_active_jobs,
        )
    except MonomerDftIdempotencyConflict as exc:
        raise _public_error(409, code="idempotency_conflict", message=str(exc)) from exc
    except MonomerDftCapacityError as exc:
        raise _public_error(
            429,
            code="capacity_full",
            message=str(exc),
            retryable=True,
            headers={"Retry-After": "5"},
        ) from exc
    if not created.created:
        return _public_job(repository, created.job, replay=True)

    _kick_reconciler(request)
    return _public_job(repository, created.job)


@router.get("/jobs/{job_id}", response_model=MonomerDftJobResponse)
async def get_job(request: Request, job_id: str) -> MonomerDftJobResponse:
    job_id = _validate_job_id(job_id)
    await _require_schema_ready(request)
    repository = _repository(request)
    job = await asyncio.to_thread(repository.get_job, job_id)
    if job is None:
        raise _public_error(404, code="job_not_found", message="DFT job not found")
    if job["status"] not in TERMINAL_STATUSES:
        _kick_reconciler(request)
    return _public_job(repository, job)


@router.post("/jobs/{job_id}/cancel", response_model=MonomerDftJobResponse)
async def cancel_job(request: Request, job_id: str) -> MonomerDftJobResponse:
    job_id = _validate_job_id(job_id)
    await _require_schema_ready(request)
    repository = _repository(request)
    try:
        job = await asyncio.to_thread(repository.request_cancel, job_id)
    except MonomerDftJobNotFound as exc:
        raise _public_error(404, code="job_not_found", message="DFT job not found") from exc
    if job["status"] not in TERMINAL_STATUSES:
        _kick_reconciler(request)
    return _public_job(repository, job)


@router.get("/jobs/{job_id}/artifacts/{artifact_id}")
async def get_artifact(request: Request, job_id: str, artifact_id: str) -> FileResponse:
    job_id = _validate_job_id(job_id)
    await _require_schema_ready(request)
    repository = _repository(request)
    try:
        artifact = await asyncio.to_thread(
            repository.get_artifact,
            job_id=job_id,
            artifact_id=artifact_id,
        )
    except MonomerDftArtifactNotFound as exc:
        raise _public_error(404, code="artifact_not_found", message="DFT artifact not found") from exc
    proxy = request.app.state.monomer_dft_download_proxy
    try:
        verified = await proxy.verify_artifact(
            open_stream=lambda: _worker(request).stream_artifact(job_id, artifact_id),
            artifact=artifact,
        )
    except MonomerDftWorkerError as exc:
        raise _public_error(
            exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            details=exc.details,
        ) from exc
    except MonomerDftDownloadProxyError as exc:
        raise _public_error(
            exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            headers={"Retry-After": "5"} if exc.code == "download_capacity_full" else None,
        ) from exc
    try:
        return VerifiedMonomerDftFileResponse(
            verified=verified,
            media_type=artifact["media_type"],
            filename=str(artifact["name"]),
            headers={
                "Content-Length": str(verified.size_bytes),
                "ETag": f'"{verified.sha256}"',
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception:
        await verified.cleanup()
        raise


@router.get("/jobs/{job_id}/bundle")
async def get_bundle(request: Request, job_id: str) -> FileResponse:
    job_id = _validate_job_id(job_id)
    await _require_schema_ready(request)
    repository = _repository(request)
    job = await asyncio.to_thread(repository.get_job, job_id)
    if job is None:
        raise _public_error(404, code="job_not_found", message="DFT job not found")
    if job["status"] not in TERMINAL_STATUSES:
        raise _public_error(409, code="job_not_terminal", message="DFT artifact bundle is not ready")
    if job["artifacts_state"] == "deleted":
        raise _public_error(404, code="artifacts_deleted", message="DFT artifacts have been deleted")
    if job["artifacts_state"] == "delete_requested":
        raise _public_error(
            409,
            code="artifact_deletion_pending",
            message="DFT artifact deletion is pending",
            retryable=True,
        )
    if job["artifacts_state"] == "none":
        raise _public_error(404, code="artifact_not_found", message="DFT job has no available artifacts")
    proxy = request.app.state.monomer_dft_download_proxy
    try:
        verified = await proxy.verify_bundle(
            open_stream=lambda: _worker(request).stream_bundle(job_id),
            artifacts=job["artifacts"],
        )
    except MonomerDftWorkerError as exc:
        raise _public_error(
            exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            details=exc.details,
        ) from exc
    except MonomerDftDownloadProxyError as exc:
        raise _public_error(
            exc.status_code,
            code=exc.code,
            message=str(exc),
            retryable=exc.retryable,
            headers={"Retry-After": "5"} if exc.code == "download_capacity_full" else None,
        ) from exc
    try:
        return VerifiedMonomerDftFileResponse(
            verified=verified,
            media_type="application/zip",
            filename=f"monomer-dft-{job_id}.zip",
            headers={
                "Content-Length": str(verified.size_bytes),
                "ETag": f'"{verified.sha256}"',
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception:
        await verified.cleanup()
        raise


@router.delete("/jobs/{job_id}/artifacts", response_model=MonomerDftArtifactDeleteResponse)
async def delete_artifacts(
    request: Request,
    response: Response,
    job_id: str,
) -> MonomerDftArtifactDeleteResponse:
    job_id = _validate_job_id(job_id)
    await _require_schema_ready(request)
    repository = _repository(request)
    job = await asyncio.to_thread(repository.get_job, job_id)
    if job is None:
        raise _public_error(404, code="job_not_found", message="DFT job not found")
    if job["status"] not in TERMINAL_STATUSES:
        raise _public_error(
            409,
            code="job_not_terminal",
            message="artifacts can be deleted only after a DFT job reaches a terminal state",
        )
    if job["artifacts_state"] == "deleted":
        response.status_code = status.HTTP_200_OK
        return MonomerDftArtifactDeleteResponse(
            job_id=job_id,
            deleted=True,
            artifacts_state="deleted",
            deleted_artifacts=_persisted_artifact_count(job),
            message="DFT artifacts were already deleted",
        )
    try:
        requested = await asyncio.to_thread(repository.request_artifact_deletion, job_id)
    except MonomerDftJobStateConflict as exc:
        raise _public_error(409, code="job_not_terminal", message=str(exc)) from exc
    artifacts_state = requested["artifacts_state"]
    if artifacts_state == "none":
        response.status_code = status.HTTP_200_OK
        return MonomerDftArtifactDeleteResponse(
            job_id=job_id,
            deleted=False,
            artifacts_state="none",
            deleted_artifacts=0,
            message="DFT job has no available artifacts",
        )
    if artifacts_state == "deleted":
        response.status_code = status.HTTP_200_OK
        return MonomerDftArtifactDeleteResponse(
            job_id=job_id,
            deleted=True,
            artifacts_state="deleted",
            deleted_artifacts=_persisted_artifact_count(requested),
            message="DFT artifacts were already deleted",
        )
    response.status_code = status.HTTP_202_ACCEPTED
    response.headers["Retry-After"] = "5"
    _kick_reconciler(request)
    return MonomerDftArtifactDeleteResponse(
        job_id=job_id,
        deleted=False,
        artifacts_state="delete_requested",
        deleted_artifacts=0,
        message="DFT artifact deletion was requested",
    )

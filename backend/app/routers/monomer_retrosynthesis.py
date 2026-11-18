from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from uuid import UUID
from fastapi import Query
from app.auth.context import current_owner_id
from app.postgres_database import PostgresUnavailableError
from app.services.entitlement_service import request_idempotency_key
from app.services.retrosynthesis_models import ResolveTargetRequest, RetrosynthesisJobRequest
from app.services.retrosynthesis_repository import public_job, TERMINAL
from app.services.retrosynthesis_targets import canonical_target, resolve_target, validate_token_budget
from task_usage import native_entitlements

from app.services.entitlement_sync import entitled_sync

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
from app.usage_context import mark_current_usage_outcome


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
@entitled_sync("retrosynthesis")
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
        if isinstance(exc, GpuQueueTimeoutError):
            mark_current_usage_outcome("timed_out")
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

# Durable MCP-facing operations. The synchronous endpoint above remains stable.

def _async_repository(request):
    return request.app.state.retrosynthesis_repository


@router.get("/monomer-retrosynthesis/status")
def retrosynthesis_status(request: Request):
    settings = request.app.state.settings
    repository = _async_repository(request)
    schema_ready = names_ready = library_ready = False
    try:
        schema_ready = repository.ready()
        with repository.connection() as connection:
            row = connection.execute("SELECT to_regclass('pi.monomer_iupac') IS NOT NULL AS names,to_regclass('structure_library.records') IS NOT NULL AS library").fetchone()
            names_ready, library_ready = row["names"], row["library"]
    except Exception:
        pass
    registry = request.app.state.gpu_runtime_registry.model_snapshots().get("retrosynthesis",{})
    model_root = Path(settings.retro_model_id)
    model_available = bool(registry.get("loaded") or (model_root.is_dir() and (model_root/"config.json").is_file() and any((model_root/name).is_file() for name in ("model.safetensors","pytorch_model.bin","model.safetensors.index.json","pytorch_model.bin.index.json"))))
    enabled = settings.retro_model_enabled and settings.retro_jobs_enabled
    accepting = request.app.state.retrosynthesis_job_manager.accepting
    return {"enabled": enabled,"available":enabled and schema_ready and model_available and accepting,
            "schema_ready":schema_ready,"accepting":accepting,"model_available":model_available,"model_loaded":bool(registry.get("loaded")),
            "name_resolution_available":names_ready,"structure_library_available":settings.structure_library_enabled and library_ready,
            "parameters":{"target_role":{"default":"auto","values":["auto","diamine","dianhydride","other"],"affects_generation":False},
                "num_beams":{"default":5,"min":1,"max":20},"num_return_sequences":{"default":5,"min":1,"max":10},
                "max_new_tokens":{"default":128,"min":16,"max":256},"max_input_tokens":512,"max_smiles_characters":8000},
            "message":"" if enabled and schema_ready and model_available and accepting else "Retrosynthesis submission is unavailable; existing jobs remain queryable."}


@router.post("/monomer-retrosynthesis/targets/resolve")
def resolve_retrosynthesis_target(body: ResolveTargetRequest, request: Request):
    try:
        # Direct SMILES resolution requires no database or model assets.
        if body.source == "smiles":
            return resolve_target(None,current_owner_id(),body,structure_library_enabled=False)
        with _async_repository(request).connection() as connection:
            return resolve_target(connection,current_owner_id(),body,structure_library_enabled=request.app.state.settings.structure_library_enabled)
    except PostgresUnavailableError as exc:
        raise HTTPException(503,detail={"code":"retrosynthesis_target_source_unavailable"}) from exc


def _confirmed_request(body,request):
    canonical,_ = canonical_target(body.smiles)
    source = {"type":"smiles"}
    if body.source.type != "smiles":
        values = {"source":body.source.type}
        if body.source.type == "name":
            values["name"] = body.source.label
        else:
            if not body.source.record_id:
                raise HTTPException(422,detail={"code":"retrosynthesis_source_required"})
            values["record_id"] = body.source.record_id
        try:
            parsed = ResolveTargetRequest.model_validate(values)
        except ValueError as exc:
            raise HTTPException(422,detail={"code":"retrosynthesis_source_required"}) from exc
        with _async_repository(request).connection() as connection:
            resolved = resolve_target(connection,current_owner_id(),parsed,structure_library_enabled=request.app.state.settings.structure_library_enabled)
        choices = ([resolved["target"]] if resolved.get("target") else []) + resolved["candidates"]
        selected = next((item for item in choices if item["canonical_smiles"] == canonical),None)
        if selected is None:
            raise HTTPException(409,detail={"code":"retrosynthesis_source_changed","message":"Resolve the target again before confirming."})
        source = selected["source"]
    normalized = body.model_dump(mode="json")
    normalized["smiles"],normalized["source"] = canonical,source
    return normalized


@router.post("/monomer-retrosynthesis/jobs",status_code=202)
def create_retrosynthesis_job(body: RetrosynthesisJobRequest, request: Request):
    settings = request.app.state.settings
    key,owner = request_idempotency_key(request),current_owner_id()
    try:
        # Resolve identity from the immutable stored source on replay; deleting or
        # renaming a saved record must not invalidate a durable submission receipt.
        canonical,_ = canonical_target(body.smiles)
        confirmed = body.model_dump(mode="json")
        confirmed["smiles"] = canonical
        confirmed["source"] = body.source.model_dump(mode="json",exclude_none=True)
        if body.source.type == "smiles":
            confirmed["source"] = {"type":"smiles"}
        previous = _async_repository(request).get(owner,key=key)
        if previous is not None:
            old_source = previous["request_json"]["source"]
            if old_source.get("type") == body.source.type:
                from app.services.smiles_to_iupac import normalize_iupac_name
                same_record = body.source.type == "saved_structure" and str(body.source.record_id) == old_source.get("record_id")
                same_name = body.source.type == "name" and normalize_iupac_name(body.source.label or "") == normalize_iupac_name(old_source.get("label") or "")
                if same_record or same_name:
                    confirmed["source"] = old_source
            if previous["request_sha256"] != native_entitlements.request_fingerprint(confirmed):
                raise HTTPException(409,detail={"code":"retrosynthesis_idempotency_conflict"})
            native_entitlements.attach_native_operation(request,owner_user_id=owner,module="retrosynthesis",native_task_id=previous["job_id"])
            return public_job(previous)
        if not settings.retro_model_enabled or not settings.retro_jobs_enabled:
            raise HTTPException(503,detail={"code":"retrosynthesis_service_disabled"})
        confirmed = _confirmed_request(body,request)
        validate_token_budget(confirmed["smiles"],settings.retro_model_id)
        result = request.app.state.retrosynthesis_job_manager.create(owner,confirmed,key)
        native_entitlements.attach_native_operation(request,owner_user_id=owner,module="retrosynthesis",native_task_id=result["job_id"])
        return result
    except ModelArtifactError as exc:
        raise HTTPException(503,detail={"code":"retrosynthesis_model_unavailable"}) from exc
    except PostgresUnavailableError as exc:
        raise HTTPException(503,detail={"code":"retrosynthesis_storage_unavailable"}) from exc


@router.get("/monomer-retrosynthesis/jobs/lookup")
def lookup_retrosynthesis_job(request: Request,idempotency_key: str = Query(min_length=8,max_length=128)):
    row = _async_repository(request).get(current_owner_id(),key=idempotency_key)
    if row is not None:
        native_entitlements.attach_native_operation(request,owner_user_id=current_owner_id(),module="retrosynthesis",native_task_id=row["job_id"])
    return public_job(row)


@router.get("/monomer-retrosynthesis/jobs/{job_id}")
async def get_retrosynthesis_job(job_id: UUID, request: Request,wait_seconds: float = Query(0,ge=0,le=20)):
    owner,deadline = current_owner_id(),monotonic()+wait_seconds
    while True:
        row = await run_in_threadpool(_async_repository(request).get,owner,job_id=job_id)
        snapshot = public_job(row)
        if snapshot["status"] in TERMINAL or snapshot["status"] == "unknown" or monotonic() >= deadline:
            native_entitlements.attach_native_operation(request,owner_user_id=owner,module="retrosynthesis",native_task_id=job_id)
            return snapshot
        await asyncio.sleep(min(0.25,max(0,deadline-monotonic())))

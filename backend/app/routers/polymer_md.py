"""Development-only, authenticated polymer MD API. No scientific engine imports."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from uuid import UUID
from typing import Literal, get_args

import anyio
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from app.auth.context import current_owner_id
from task_usage import entitlements
from app.postgres_database import PostgresUnavailableError
from app.services.polymer_md_repository import PolymerMdError, public_job, submit_request
from app.services.polymer_md_acceptance_registry import acceptance_summary
from app.services.polymer_md_analysis_client import ANALYSIS_TIMEOUT_SECONDS
from app.services.polymer_md_logs import read_stage_logs
from polymer_md.capabilities import get_capability_catalog, get_user_configuration_spec, get_model_configuration_spec, get_post_equilibrium_protocols
from polymer_md.contracts import Artifact, Forcefield, NativeConfiguration, ScientificRequest, StartAuthorizationRequest, StartAuthorizationResponse, DistanceRequest


class DomainRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                if request.method in {"POST","PUT","PATCH","DELETE"}:
                    body=[]
                    size=0
                    async for chunk in request.stream():
                        size+=len(chunk)
                        if size>32768:
                            raise PolymerMdError("request_too_large", "计算请求超过允许大小。", 413)
                        body.append(chunk)
                    request._body=b"".join(body)
                response = await handler(request)
                response.headers["Cache-Control"] = "private, no-store"
                return response
            except PolymerMdError as exc:
                return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": str(exc), "retryable": exc.status >= 500 or exc.code=="analysis_busy"}, headers={"Cache-Control":"private, no-store"})
            except PostgresUnavailableError:
                return JSONResponse(status_code=503, content={"code":"database_unavailable", "message":"任务数据库暂不可用。", "retryable":True}, headers={"Cache-Control":"private, no-store"})
        return wrapped


router = APIRouter(prefix="/api/v1/polymer-md", tags=["polymer-md"], route_class=DomainRoute)
internal_router = APIRouter(prefix="/internal/polymer-md", tags=["internal-polymer-md"], route_class=DomainRoute)


def _job_id(value):
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise PolymerMdError("job_not_found", "任务不存在。", 404) from exc


def _repository(request):
    return request.app.state.polymer_md_repository


def _worker(request):
    value = request.app.state.polymer_md_worker_client
    if value is None:
        raise PolymerMdError("worker_unavailable", "开发计算服务尚未配置。", 503)
    return value


async def _ready(request):
    if not request.app.state.settings.polymer_md_dev_enabled:
        raise PolymerMdError("feature_disabled", "真实聚合物 MD 尚未在开发环境启用。", 503)
    if not await asyncio.to_thread(_repository(request).schema_ready):
        raise PolymerMdError("schema_not_ready", "需显式准备开发环境 0023 数据结构。", 503)


async def _owned(request, job_id):
    await _ready(request)
    return await asyncio.to_thread(_repository(request).get_job, _job_id(job_id), owner_user_id=current_owner_id())


def _kick(request):
    reconciler = request.app.state.polymer_md_reconciler
    if reconciler:
        reconciler.kick()


def _public_runtime(health):
    raw=health.get("runtime",{}) if isinstance(health,dict) else {}
    if not isinstance(raw,dict):
        raw={}
    result={"source_sha":None,"patch_version":None,"runtime_fingerprint":None,
            "scientific_acceptance":"pending","adapter_scientific_validation":"pending","accepted_protocols":[]}
    for key,pattern in (("source_sha",r"[0-9a-f]{40}"),("runtime_fingerprint",r"[0-9a-f]{64}"),
                        ("patch_version",r"[A-Za-z0-9._-]{1,128}"),("scientific_acceptance",r"[A-Za-z0-9._-]{1,128}"),
                        ("adapter_scientific_validation",r"[A-Za-z0-9._-]{1,128}")):
        value=raw.get(key)
        if isinstance(value,str) and re.fullmatch(pattern,value):
            result[key]=value
    protocols=raw.get("accepted_protocols",[])
    if isinstance(protocols,list):
        result["accepted_protocols"]=[item for item in protocols[:50] if isinstance(item,str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",item)]
    return result


WORKER_ADMISSION_REASONS = frozenset({"maintenance", "runtime_not_ready", "cleanup_unconfirmed",
    "recovering", "scope_not_ready", "authorization_unavailable", "monitor_unavailable", "stopping"})
RESOURCE_FIELDS = ("cpu_count", "memory_bytes", "gpu_count", "stage_timeout_seconds",
    "job_timeout_seconds", "max_job_bytes", "max_total_atoms", "max_replicated_atoms")


def _effective_resource_limits(health):
    """Only the running Worker can state its actual execution limits."""
    values = health.get("resource_limits") if isinstance(health, dict) else None
    if not isinstance(values, dict):
        return None
    for key in RESOURCE_FIELDS:
        value = values.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or value < (0 if key == "gpu_count" else 1) or value > 2**63 - 1 or not math.isfinite(value)
                or not float(value).is_integer()):
            return None
    return {key: int(values[key]) for key in RESOURCE_FIELDS}


def _maintenance(health):
    value = health.get("maintenance") if isinstance(health, dict) else None
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool or type(value.get("persisted")) is not bool:
        return None
    updated = value.get("updated_at")
    if not isinstance(updated, str) or re.fullmatch(r"[0-9]{4}-[0-9T:.+Z-]{10,35}", updated) is None:
        updated = None
    return {"enabled": value["enabled"], "updated_at": updated, "persisted": value["persisted"]}


def _admission_reason(*, enabled, schema_ready, submit_enabled, worker_ready, health):
    if not enabled:
        return "feature_disabled"
    if not schema_ready:
        return "schema_not_ready"
    if entitlements.enabled():
        return "entitlements_unsupported"
    if isinstance(health, dict):
        reason = health.get("admission_reason")
        if isinstance(reason, str) and reason in WORKER_ADMISSION_REASONS:
            return reason
        # Older Workers have no typed reason. Preserve their maintenance state.
        if health.get("draining") is True:
            return "maintenance"
    if not submit_enabled:
        return "submission_disabled"
    if health is None:
        return "worker_unavailable"
    return None if worker_ready else "worker_not_ready"


def _scientific_protocol():
    return {"version":1,"polymer_type":"neutral_linear_polymers_and_blends",
            "model_configuration_source":"scientific_request.model_configuration; absent uses legacy homopolymer",
            "forcefield":"scientific_request.forcefield",
            "forcefield_default":"GAFF2_mod",
            "cpu_count":16,"max_mpi_processes":16,"gpu_count":0,
            "resource_limits_source":"capabilities.resource_limits; measured Worker health, null when unavailable",
            "mpi_selection":{"authority":"server","formula":"min(16, max(1, ceil(actual_atom_count / 500)))",
                             "monomer_mm_processes":1,"quantum_optimization":"serial"},
            "initial_density_g_cm3":0.05,
            "packing":{"target_density_g_cm3":0.8,"max_temperature_k":700},
            "equilibration":{"scheme":"21step","time_step_fs":1.0,"max_temperature_k":600,"max_pressure_atm":50000},
            "equilibrium_analysis":{"burn_in_ps":2000,"minimum_production_samples":500,"uncertainty_blocks":5},
            "rnemd":{"method":"Muller_Plathe","time_step_fs":0.2,"burn_in_ps":800,"preheat_ps":2,
                     "replication_other_axes":1,"decomposition":False,"requires_equilibrium_and_orientation":True},
            "qm":{"conformer_search_count":1000,"dft_conformer_count":4,
                  "configuration_source":"scientific_request.native_configuration",
                  "optimization":"native_configuration.opt_method/native_configuration.opt_basis",
                  "charges":"native_configuration.charge; native_configuration.charge_method/native_configuration.charge_basis",
                  "single_point":"native_configuration.sp_method/native_configuration.sp_basis",
                  "static_polarizability":"native_configuration.polar_method/native_configuration.polar_basis",
                  "legacy_defaults":{"optimization":"wb97m-d3bj/6-31G(d,p)","charges":"RESP HF/6-31G(d)",
                                     "single_point":"wb97m-d3bj/6-311G(d,p)",
                                     "static_polarizability":"wb97m-d3bj/6-311+G(2d,p)"},
                  "legacy_defaults_apply":"only_when_native_configuration_is_absent",
                  "molecular_only":"selected QM targets skip polymer construction and MD"},
            "retention":{"artifact_days":7,"metadata_days":30,"clock":"server_terminal_finished_at","requires_confirmed_cleanup":True}}


@router.get("/capabilities")
@router.get("/status")
async def capabilities(request: Request):
    settings = request.app.state.settings
    enabled = settings.polymer_md_dev_enabled
    schema_ready = enabled and await asyncio.to_thread(_repository(request).schema_ready)
    async def execution_health():
        if not enabled or not request.app.state.polymer_md_worker_client:
            return None
        try:
            return await asyncio.wait_for(_worker(request).health(), timeout=6)
        except (PolymerMdError, asyncio.TimeoutError):
            return None

    async def analysis_health():
        client = getattr(request.app.state, "polymer_md_analysis_client", None)
        if not enabled or client is None:
            return None
        try:
            return await asyncio.wait_for(client.health(), timeout=1.5)
        except (PolymerMdError, asyncio.TimeoutError):
            return None

    health, analysis_health_value = await asyncio.gather(execution_health(), analysis_health())
    worker_ready = bool(health and (health.get("ready") is True or health.get("status") == "ready"))
    # Analysis has its own process and health. Its failure cannot close MD
    # execution admission, and an unavailable reader never falls back to MD.
    analysis_ready = bool(analysis_health_value and analysis_health_value.get("ready") is True)
    analysis = {"status": "unavailable", "timeout_seconds": ANALYSIS_TIMEOUT_SECONDS}
    if analysis_ready:
        analysis["status"] = "busy" if analysis_health_value.get("active", 0) else "ready"
    runtime = _public_runtime(health)
    protocol = _scientific_protocol()
    acceptance = await asyncio.to_thread(acceptance_summary,
        getattr(settings, "polymer_md_acceptance_root", ""), runtime, protocol)
    admission_reason = _admission_reason(enabled=enabled, schema_ready=schema_ready,
        submit_enabled=settings.polymer_md_submit_enabled, worker_ready=worker_ready, health=health)
    return {"enabled": enabled, "submit_enabled": bool(settings.polymer_md_submit_enabled and schema_ready and worker_ready and not entitlements.enabled()),
            "admission_reason":admission_reason, "maintenance":_maintenance(health),
            "resource_limits":_effective_resource_limits(health),
            "acceptance_summary":acceptance,
            "schema_ready": bool(schema_ready), "worker_ready": worker_ready, "protocol_version":1,
            "execution_backend":"local_cpu_mpi", "supported_forcefields":list(get_args(Forcefield)),
            "supported_terminals":["CH3","H","OH"], "supported_calculations":["basic","basic_tc"],
            "supported_tacticities":["atactic","isotactic","syndiotactic"],
            "parameter_schema":ScientificRequest.model_json_schema(),
            "native_configuration_schema":NativeConfiguration.model_json_schema(),
            "model_configuration_spec":get_model_configuration_spec(),
            "post_equilibrium_protocols":get_post_equilibrium_protocols(),
            "supported_model_modes":["homopolymer","alternating","random","block","blend"],
            "user_configuration_spec":get_user_configuration_spec(),
            "parameter_defaults":{name:field.get_default(call_default_factory=True) for name,field in ScientificRequest.model_fields.items() if not field.is_required()},
            "scientific_protocol":protocol,"runtime":runtime,
            "capability_catalog":get_capability_catalog(),
            "limits":{"per_owner_active":1,"global_active":3,"execution":1,"max_total_atoms":20000,"max_rnemd_atoms":60000},
            "trajectory_available":analysis_ready, "distance_available":analysis_ready,
            "analysis":analysis,
            "worker": {"ready":worker_ready,"protocol_version":1} if health else None}


@router.post("/jobs", status_code=202)
async def create_job(request: Request, payload: ScientificRequest, idempotency_key: str = Header(alias="Idempotency-Key")):
    await _ready(request)
    if not request.app.state.settings.polymer_md_submit_enabled:
        raise PolymerMdError("submission_disabled", "开发计算提交开关未启用。", 503)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", idempotency_key) is None:
        raise PolymerMdError("invalid_idempotency_key", "请提供有效的幂等请求标识。", 422)
    existing=await asyncio.to_thread(_repository(request).find_idempotent_job,payload,owner_user_id=current_owner_id(),idempotency_key=idempotency_key)
    if existing is not None:
        return {**public_job(existing),"created":False}
    if entitlements.enabled():
        raise PolymerMdError("entitlements_unsupported", "当前额度策略下暂不能提交聚合物 MD 计算。", 503)
    health=await _worker(request).health()
    if health.get("ready") is not True and health.get("status") != "ready":
        raise PolymerMdError("worker_not_ready", "开发计算环境尚未就绪。", 503)
    job, created = await asyncio.to_thread(_repository(request).create_job, payload, owner_user_id=current_owner_id(), idempotency_key=idempotency_key)
    _kick(request)
    return {**public_job(job), "created": created}


@router.get("/jobs")
async def list_jobs(request: Request, page: int = Query(1, ge=1, le=10000), page_size: int = Query(20, ge=1, le=100)):
    await _ready(request)
    return await asyncio.to_thread(_repository(request).list_jobs, owner_user_id=current_owner_id(), page=page, page_size=page_size)


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    return public_job(await _owned(request, job_id))


@router.get("/jobs/{job_id}/results")
async def get_results(request: Request, job_id: str):
    job=await _owned(request,job_id)
    return {"job_id":job["job_id"],"snapshot_revision":job["snapshot_revision"],"status":job["status"],
            "quality_status":job["quality_status"],"results":job["results"]}


@router.get("/jobs/{job_id}/stages")
async def get_stages(request: Request, job_id: str):
    job=await _owned(request,job_id)
    return {"job_id":job["job_id"],"snapshot_revision":job["snapshot_revision"],"stage_runs":job["stage_runs"]}


@router.get("/jobs/{job_id}/stages/{stage_run_id}/logs")
async def stage_logs(request: Request, job_id: str, stage_run_id: str, tail_lines: int = Query(200,ge=1,le=1000),
                     source: Literal["auto", "engine", "stdout", "runner"] = Query("auto")):
    job=await _owned(request,job_id)
    if job["artifact_state"]!="available" or not any(stage["stage_run_id"]==stage_run_id for stage in job["stage_runs"]):
        raise PolymerMdError("stage_not_found", "该任务的阶段日志不存在。", 404)
    root = getattr(request.app.state.settings, "polymer_md_artifact_root", "")
    if not root:
        raise PolymerMdError("stage_logs_unavailable", "阶段日志只读存储尚未配置。", 503)
    value = await asyncio.to_thread(read_stage_logs, root, job, stage_run_id, tail_lines=tail_lines, source=source)
    return {**value, "job_id": job["job_id"]}


@router.post("/jobs/{job_id}/cancel", status_code=202)
async def cancel_job(request: Request, job_id: str):
    await _owned(request, job_id)
    job = await asyncio.to_thread(_repository(request).request_cancel, _job_id(job_id), owner_user_id=current_owner_id())
    _kick(request)
    return public_job(job)


@router.get("/jobs/{job_id}/artifacts")
async def artifacts(request: Request, job_id: str):
    job = await _owned(request, job_id)
    return {"job_id": job["job_id"], "artifact_state":job["artifact_state"], "artifacts": public_job(job)["artifacts"]}


async def _delete(request, job_id, entire_job=False):
    await _owned(request, job_id)
    job = await asyncio.to_thread(_repository(request).request_deletion, _job_id(job_id), owner_user_id=current_owner_id(),delete_job=entire_job)
    _kick(request)
    await _worker(request).delete(submit_request(job), entire_job=entire_job)
    await asyncio.to_thread(_repository(request).complete_deletion, job["job_id"], job["attempt_token"], delete_job=entire_job)
    return {"job_id":job["job_id"], "deleted":True, "artifact_state":"deleted"}


@router.delete("/jobs/{job_id}/artifacts")
async def delete_artifacts(request: Request, job_id: str):
    return await _delete(request, job_id)


@router.delete("/jobs/{job_id}")
async def delete_job(request: Request, job_id: str):
    return await _delete(request, job_id, True)


class VerifiedFileResponse(FileResponse):
    def __init__(self, *, limiter, temporary, **kwargs):
        self.limiter, self.temporary = limiter, temporary
        super().__init__(**kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.temporary.unlink(missing_ok=True)
            self.limiter.release()


class VerifiedMountedResponse(StreamingResponse):
    """Stream the verified descriptor, so a path replacement cannot redirect reads."""
    def __init__(self, *, handle, limiter, artifact):
        self.handle, self.limiter = handle, limiter
        def chunks():
            while chunk := handle.read(1024*1024):
                yield chunk
        super().__init__(chunks(), media_type=artifact["media_type"], headers={
            "Content-Length":str(artifact["size_bytes"]),
            "Content-Disposition":f'attachment; filename="{artifact["filename"]}"',
            "Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.handle.close()
            self.limiter.release()


def _open_verified_artifact(root, job, artifact):
    ref = artifact.get("storage_ref")
    if (not ref or not ref.startswith(f'{job["job_id"]}/{job["attempt_token"]}/')
        or artifact.get("attempt_token") != job["attempt_token"]
        or not any(s["stage_run_id"] == artifact["stage_run_id"] for s in job["stage_runs"])):
        raise PolymerMdError("artifact_reference_invalid", "产物执行身份不匹配。", 502)
    parts=ref.split("/")
    if any(p in {".",".."} or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*",p) is None for p in parts):
        raise PolymerMdError("artifact_reference_invalid", "产物存储引用无效。", 502)
    directory_fd = None
    handle = None
    try:
        # Resolve every component relative to an already-open directory. Reject
        # symlinks at every level rather than relying on a check-then-open path.
        directory_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        for component in parts[:-1]:
            next_fd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd=next_fd
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
        handle=os.fdopen(fd,"rb")
        before=os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size!=artifact["size_bytes"]:
            raise PolymerMdError("artifact_integrity_mismatch", "产物大小校验失败。", 502)
        digest=hashlib.sha256()
        while chunk := handle.read(1024*1024):
            digest.update(chunk)
        after=os.fstat(handle.fileno())
        if (digest.hexdigest()!=artifact["sha256"] or before.st_size!=after.st_size
            or before.st_mtime_ns!=after.st_mtime_ns):
            raise PolymerMdError("artifact_integrity_mismatch", "产物摘要校验失败。", 502)
        handle.seek(0)
        result,handle=handle,None
        return result
    except OSError as exc:
        raise PolymerMdError("artifact_storage_unavailable", "只读产物存储暂不可用。", 503) from exc
    finally:
        if handle is not None:
            handle.close()
        if directory_fd is not None:
            os.close(directory_fd)


@router.get("/jobs/{job_id}/artifacts/{artifact_id}")
async def download_artifact(request: Request, job_id: str, artifact_id: str):
    job = await _owned(request, job_id)
    artifact = next((a for a in job["artifacts"] if a["artifact_id"] == artifact_id), None)
    if artifact is None or job["artifact_state"] != "available":
        raise PolymerMdError("artifact_not_found", "产物不存在或正在清理。", 404)
    limiter = request.app.state.polymer_md_download_limiter
    try:
        limiter.acquire_nowait()
    except anyio.WouldBlock as exc:
        raise PolymerMdError("download_capacity_full", "下载并发已满。", 429) from exc
    path = None
    try:
        mounted_root=getattr(request.app.state.settings,"polymer_md_artifact_root","")
        if mounted_root:
            handle=await anyio.to_thread.run_sync(_open_verified_artifact,mounted_root,job,artifact)
            return VerifiedMountedResponse(handle=handle,limiter=limiter,artifact=artifact)
        root = Path(request.app.state.settings.polymer_md_download_spool_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix="polymer-md-", dir=root)
        path = Path(filename)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as handle:
            async with _worker(request).artifact_stream(submit_request(job), artifact_id) as response:
                async for chunk in response.aiter_bytes(1024*1024):
                    size += len(chunk)
                    if size > artifact["size_bytes"]:
                        raise PolymerMdError("artifact_integrity_mismatch", "产物大小校验失败。", 502)
                    digest.update(chunk)
                    handle.write(chunk)
        if size != artifact["size_bytes"] or digest.hexdigest() != artifact["sha256"]:
            raise PolymerMdError("artifact_integrity_mismatch", "产物摘要校验失败。", 502)
        return VerifiedFileResponse(path=path, temporary=path, limiter=limiter, filename=artifact["filename"], media_type=artifact["media_type"], headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})
    except BaseException:
        if path:
            path.unlink(missing_ok=True)
        limiter.release()
        raise


async def _stage_job(request, job_id, stage_run_id):
    job = await _owned(request, job_id)
    if job["artifact_state"] != "available" or not any(s["stage_run_id"] == stage_run_id and s["status"] == "completed" for s in job["stage_runs"]) or not any(a["stage_run_id"] == stage_run_id for a in job["artifacts"]):
        raise PolymerMdError("stage_not_found", "该任务的阶段轨迹不存在。", 404)
    return job


def _analysis_artifacts(job, stage_run_id):
    values = []
    for item in job["artifacts"]:
        if item["stage_run_id"] != stage_run_id:
            continue
        if item.get("attempt_token", job["attempt_token"]) != job["attempt_token"]:
            raise PolymerMdError("artifact_integrity_mismatch", "轨迹产物执行身份不匹配。", 409)
        try:
            descriptor = Artifact.model_validate({key: item[key] for key in Artifact.model_fields if key in item})
        except ValueError as exc:
            raise PolymerMdError("artifact_integrity_mismatch", "轨迹产物清单无效。", 409) from exc
        if not descriptor.storage_ref or not descriptor.storage_ref.startswith(f'{job["job_id"]}/{job["attempt_token"]}/'):
            raise PolymerMdError("artifact_integrity_mismatch", "轨迹产物存储身份不匹配。", 409)
        values.append(descriptor.model_dump(mode="json"))
    return sorted(values, key=lambda item: item["artifact_id"])


async def _analyze_stage(request, job, stage_run_id, operation, **options):
    client = getattr(request.app.state, "polymer_md_analysis_client", None)
    if client is None:
        raise PolymerMdError("analysis_service_unavailable", "轨迹分析服务暂不可用，已有模拟任务不受影响。", 503)
    stage = next(item for item in job["stage_runs"] if item["stage_run_id"] == stage_run_id)
    artifacts = _analysis_artifacts(job, stage_run_id)
    payload = {"job_id": job["job_id"], "attempt_token": job["attempt_token"],
               "stage_run_id": stage_run_id, "stage": stage["stage"], "artifacts": artifacts, **options}
    async def wait_disconnected():
        # Body validation has completed. Wait for the ASGI disconnect directly;
        # polling Request.is_disconnected can swallow task cancellation inside
        # its AnyIO CancelScope and leave a successful response waiting forever.
        while (await request.receive())["type"] != "http.disconnect":
            pass
    value = await client.analyze(operation, payload, wait_disconnected=wait_disconnected)
    if value.get("job_id") != job["job_id"] or value.get("stage_run_id") != stage_run_id:
        raise PolymerMdError("invalid_stage_response", "轨迹分析返回了其他任务或阶段。", 502)
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise PolymerMdError("invalid_stage_response", "轨迹分析返回了无效数值。", 502) from exc
    if operation == "distance":
        expected = options["distance"]
        if any(value.get(key) != expected[key] for key in ("atom_id_1", "atom_id_2", "use_pbc")) or value.get("unit") != "A":
            raise PolymerMdError("invalid_stage_response", "距离结果与当前原子选择不匹配。", 502)
    elif value.get("frame_index") != options["frame_index"]:
        raise PolymerMdError("invalid_stage_response", "轨迹结果与当前帧不匹配。", 502)
    # The service uses private immutable copies. Recheck current access and
    # deletion/attempt fencing before publishing a completed calculation.
    current = await _stage_job(request, job["job_id"], stage_run_id)
    if current["attempt_token"] != job["attempt_token"] or _analysis_artifacts(current, stage_run_id) != artifacts:
        raise PolymerMdError("artifact_integrity_mismatch", "轨迹执行身份或产物已变化，请刷新任务。", 409)
    return value


@router.get("/jobs/{job_id}/stages/{stage_run_id}/trajectory")
async def trajectory(request: Request, job_id: str, stage_run_id: str, frame_index: int = Query(0, ge=0), offset: int | None = Query(None, ge=0), limit: int = Query(10000, ge=1, le=10000)):
    job = await _stage_job(request, job_id, stage_run_id)
    return await _analyze_stage(request, job, stage_run_id, "trajectory", frame_index=frame_index if offset is None else offset, limit=limit)


@router.post("/jobs/{job_id}/stages/{stage_run_id}/distance")
async def distance(request: Request, job_id: str, stage_run_id: str, payload: DistanceRequest):
    job = await _stage_job(request, job_id, stage_run_id)
    return await _analyze_stage(request, job, stage_run_id, "distance", distance=payload.model_dump(mode="json"))


@internal_router.post("/jobs/{job_id}/authorize-start", response_model=StartAuthorizationResponse)
async def authorize_start(request: Request, job_id: str, payload: StartAuthorizationRequest):
    await _ready(request)
    expected = request.app.state.settings.polymer_md_start_authorization_token
    provided = request.headers.get("authorization", "")
    if not expected or not secrets.compare_digest(provided.encode(), ("Bearer "+expected).encode()):
        raise PolymerMdError("service_identity_required", "内部服务身份未获授权。", 403)
    if _job_id(job_id) != payload.job_id:
        raise PolymerMdError("start_rejected", "计算启动授权拒绝。", 403)
    authorized_at = await asyncio.to_thread(_repository(request).authorize_start, payload)
    if authorized_at is None:
        raise PolymerMdError("start_rejected", "账号状态不允许开始计算。", 403)
    return StartAuthorizationResponse(**{k:getattr(payload,k) for k in ("job_id","attempt_token","request_sha256","enqueue_sequence","worker_instance_id")}, start_authorized_at=authorized_at)

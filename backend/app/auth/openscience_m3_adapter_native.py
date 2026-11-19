"""Native CPU model jobs and owned receipts on the isolated dev-m1 authority."""

from contextlib import asynccontextmanager
from functools import partial
import hashlib
import json
from pathlib import Path

import anyio
from fastapi import HTTPException, Query, Request

from app.auth.context import current_owner_id, service_context
from app.postgres_database import postgres_connection
from app.routers import monomer_retrosynthesis as retro
from app.routers import polytao, usage
from app.services.entitlement_memory import get_memory_job
from app.services.gpu_runtime_registry import GpuRuntimeRegistry
from app.services.in_memory_jobs import BoundedInMemoryJobStore
from app.services.monomer_retrosynthesis import load_retrosynthesis_runtime
from app.services.polytao_jobs import PolytaoJobManager
from app.services.polytao_runtime import BackendPolytaoRuntime
from app.services.retrosynthesis_jobs import RetrosynthesisJobManager
from app.services.retrosynthesis_repository import RetrosynthesisRepository

MODEL_ROOT = Path("/m3adapter-models")
RELEASE = "ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2"
MODELS = ("polytao", "reactiont5-retrosynthesis")


def check_assets():
    manifest = (MODEL_ROOT / "ASSET-MANIFEST.json").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != RELEASE:
        raise ValueError("Unexpected development model release")
    files = json.loads(manifest)["assets"]["model"]
    for model in MODELS:
        directory = MODEL_ROOT / model
        expected = {entry["path"]: entry for entry in files if entry["path"].startswith(model + "/")}
        actual = {f.relative_to(MODEL_ROOT).as_posix() for f in directory.rglob("*") if f.is_file()}
        if not expected or set(expected) != actual:
            raise ValueError(f"Unexpected files in the development {model} model")
        for name in sorted(actual):
            path = MODEL_ROOT / name
            with path.open("rb") as stream:
                if (
                    path.is_symlink()
                    or hashlib.file_digest(stream, "sha256").hexdigest() != expected[name]["sha256"]
                ):
                    raise ValueError(f"Development model digest mismatch: {name}")


def lookup_polytao_receipt(request: Request, idempotency_key: str = Query(min_length=8, max_length=128)):
    owner = current_owner_id()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    with postgres_connection(request.app.state.auth.settings.application_dsn) as connection:
        row = connection.execute(
            """SELECT native_task_id,operation_id FROM usage_entitlements.operations
               WHERE owner_user_id=%s AND module='polytao' AND key_hash=%s""",
            (owner, key_hash),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found")
    if not row["native_task_id"]:
        raise HTTPException(409, "The original job receipt is pending")
    # get_memory_job restores a completed task from its durable quota record after eviction or restart.
    job = get_memory_job(
        request, "polytao", row["native_task_id"], request.app.state.polytao_job_manager.get_job
    )
    request.state.usage_operation_id = str(row["operation_id"])
    return {"job_id": job.job_id, "status": job.status}


def install_adapter_native(app):
    check_assets()
    settings = app.state.settings
    settings.app_postgres_dsn = app.state.auth.settings.application_dsn
    settings.gpu_async_queue_timeout_seconds = 120
    settings.polytao_enabled = True
    settings.polytao_model_id = "dev-m1-cpu-polytao"
    settings.polytao_model_revision = RELEASE
    # The public IP guard is a coarse ingress ceiling on this UDS service. The adapter
    # applies its per-owner admission before any new native submission.
    settings.polytao_rate_limit_per_ip_per_minute = 120
    settings.polytao_rate_limit_window_seconds = 60
    settings.retro_model_enabled = settings.retro_jobs_enabled = True
    settings.retro_model_id = str(MODEL_ROOT / "reactiont5-retrosynthesis")
    settings.retro_device = "cpu"
    settings.structure_library_enabled = False

    registry = app.state.gpu_runtime_registry = GpuRuntimeRegistry(max_concurrent_inferences=1)
    poly_runtime = app.state.polytao_runtime = BackendPolytaoRuntime(
        model_dir=MODEL_ROOT / "polytao",
        device="cpu",
        model_id=settings.polytao_model_id,
        model_revision=RELEASE,
    )
    registry.register("polytao", enabled=True, loader=poly_runtime.ensure_loaded)
    registry.register(
        "retrosynthesis",
        enabled=True,
        loader=lambda: load_retrosynthesis_runtime(settings.retro_model_id, "cpu"),
    )
    poly_manager = app.state.polytao_job_manager = PolytaoJobManager(
        max_workers=1, max_active_jobs=2, store=BoundedInMemoryJobStore()
    )
    repository = app.state.retrosynthesis_repository = RetrosynthesisRepository(settings.app_postgres_dsn)
    retro_manager = app.state.retrosynthesis_job_manager = RetrosynthesisJobManager(app, repository)
    base = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with base(application):
            try:
                with service_context(app.state.auth.settings):
                    if not await anyio.to_thread.run_sync(retro_manager.start):
                        raise RuntimeError("Private retrosynthesis manager could not acquire its executor")
                yield
            finally:
                for manager in (poly_manager, retro_manager):
                    manager.stop_accepting()
                    await anyio.to_thread.run_sync(partial(manager.close_executor, wait=True))
                registry.stop_accepting()

    app.router.lifespan_context = lifespan
    app.add_api_route(
        "/api/v1/conditional-generation/polytao/jobs/lookup", lookup_polytao_receipt, methods=["GET"]
    )
    app.include_router(polytao.router)
    routes = {
        retro.retrosynthesis_status,
        retro.resolve_retrosynthesis_target,
        retro.create_retrosynthesis_job,
        retro.lookup_retrosynthesis_job,
        retro.get_retrosynthesis_job,
        usage.activity,
    }
    for router in (retro.router, usage.router):
        app.router.routes.extend(route for route in router.routes if route.endpoint in routes)

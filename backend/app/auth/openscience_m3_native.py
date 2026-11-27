"""Native NexPoly routes for the isolated OpenScience six-tool development pilot."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
from types import SimpleNamespace

import anyio
from fastapi import FastAPI

from app.entitlement_middleware import EntitlementAccessMiddleware
from app.routers import monomer_dft, monomer_polymerization, predict
from app.services.entitlement_service import entitlement_exception_handler
from app.services.monomer_dft_download_proxy import MonomerDftDownloadProxy
from app.services.monomer_dft_reconciler import MonomerDftReconciler
from app.services.monomer_dft_repository import MonomerDftRepository
from app.usage_context import UsageCollector
from app.usage_middleware import UsageMeteringMiddleware
from task_usage.entitlement_config import configured_policy
from task_usage.entitlements import EntitlementError
from task_usage.ledger import metering_enabled

from .openscience_m3_worker import create_fixture_worker


def install_native(app: FastAPI) -> None:
    policy = configured_policy()
    if policy is None or not metering_enabled():
        raise RuntimeError("The M3 development pilot requires native quota and usage accounting")
    auth = app.state.auth.settings
    app.state.settings = SimpleNamespace(
        model_enabled=True,
        model_dir_path=Path("/m3model"),
        smipoly_enabled=True,
        monomer_dft_submit_enabled=True,
        monomer_dft_worker_uds="in-process-development-only",
        monomer_dft_max_active_jobs=2,
    )
    app.state.entitlement_policy = policy
    app.state.usage_collector = UsageCollector(auth.application_dsn, settings=auth)
    app.state.smipoly_limiter = anyio.CapacityLimiter(1)
    app.state.monomer_dft_validation_limiter = anyio.CapacityLimiter(2)
    app.state.monomer_dft_repository = repository = MonomerDftRepository(auth.application_dsn)
    worker, worker_lifespan = create_fixture_worker(repository, app.state.monomer_dft_validation_limiter)
    app.state.monomer_dft_worker_client = worker
    app.state.monomer_dft_download_proxy = MonomerDftDownloadProxy(
        spool_root=Path("/tmp/m3-downloads"), max_concurrent=2
    )
    app.state.monomer_dft_reconciler = reconciler = MonomerDftReconciler(
        repository=repository,
        worker=worker,
        interval_seconds=0.5,
        artifact_retention_enabled=False,
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application), worker_lifespan():
            try:
                reconciler.start()
                yield
            finally:
                await reconciler.stop()

    app.router.lifespan_context = lifespan
    app.add_exception_handler(EntitlementError, entitlement_exception_handler)
    app.add_exception_handler(
        monomer_dft.MonomerDftPublicError, monomer_dft.monomer_dft_public_error_handler
    )
    app.add_middleware(UsageMeteringMiddleware, collector=app.state.usage_collector)
    app.add_middleware(
        EntitlementAccessMiddleware, repository=app.state.entitlement_repository, policy=policy
    )

    # Reuse the original APIRoute objects, including their entitlement wrappers,
    # validation, response models, and public errors. Keep the surface to six tools.
    app.include_router(predict.router)
    allowed = {
        monomer_polymerization.monomer_polymerization,
        monomer_dft.create_job,
        monomer_dft.list_jobs,
        monomer_dft.get_job,
        monomer_dft.get_artifact,
        monomer_dft.get_bundle,
    }
    for router in (monomer_polymerization.router, monomer_dft.router):
        app.router.routes.extend(route for route in router.routes if route.endpoint in allowed)

    if os.environ.get("OPENSCIENCE_M3_ADAPTERS_ENABLED") == "1":
        from .openscience_m3_adapter_native import install_adapter_native

        install_adapter_native(app)

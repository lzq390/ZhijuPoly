"""CPU acceptance fixture behind the native DFT worker contract.

The production worker's durable queue, start authorization, scientific input/output
format and artifact writer are exercised. Numerical values from this test potential
are synthetic and identified as such in each artifact's provenance.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.services.monomer_dft_worker_client import MonomerDftWorkerClient
from workers.monomer_dft_worker.app.engine import (
    ComputationCancelled,
    ScientificComputationError,
    ScientificEngine,
)
from workers.monomer_dft_worker.app.job_manager import JobManager, JobManagerError
from workers.monomer_dft_worker.app.schemas import JobSubmitRequest

FIXTURE = "m3-cpu-harmonic-fixture"


class CpuTestRuntime(dict):
    ready = True
    settings = SimpleNamespace(physical_gpu="none")

    def probe(self):
        return self


class HarmonicSinglePoint:
    def evaluate(self, *, atomic_numbers, coordinates_angstrom, net_charge, forces, hessian, **_):
        coordinates = np.asarray(coordinates_angstrom)
        values = {
            "energy": np.array([0.5 * np.square(coordinates).sum()]),
            "charges": np.full(len(atomic_numbers), net_charge / len(atomic_numbers)),
        }
        if forces:
            values["forces"] = -coordinates
        if hessian:
            values["hessian"] = np.eye(coordinates.size)
        return values

    def optimize(self, **_):
        raise ScientificComputationError(
            "fixture_scope", "The development calculation fixture accepts single points"
        )


class FixtureEngine(ScientificEngine):
    def execute(self, request, directory, *, provenance, **kwargs):
        # The version-2 wire format requires a fenced execution identity. Mark
        # every field as part of the CPU fixture and use the actual job sequence.
        provenance.update(
            execution_path="primary",
            gpu_uuid="fixture-cpu-no-gpu",
            gpu_budget_mib=1,
            broker_instance_id=FIXTURE,
            lease_id="fixture-" + request.job_id,
            fencing_token=request.enqueue_sequence,
        )
        return super().execute(request, directory, provenance=provenance, **kwargs)


def create_fixture_worker(repository, limiter):
    def authorize(snapshot):
        if not repository.authorize_start(
            job_id=snapshot.job_id,
            attempt_token=snapshot.attempt_token,
            request_sha256=snapshot.request_sha256,
            enqueue_sequence=snapshot.enqueue_sequence,
        ):
            raise ComputationCancelled("Native execution start authorization was denied")

    # The existing wire contract only accepts one CUDA logical device. The
    # worker version, broker id and name label that field as a fixture.
    runtime = CpuTestRuntime(
        gpu_name="M3 CPU test fixture (no GPU)", visible_gpu_count=1, logical_device="cuda:0"
    )
    manager = JobManager(
        job_root=Path("/m3artifacts/jobs"),
        engine=FixtureEngine(HarmonicSinglePoint()),
        runtime=runtime,
        worker_version=FIXTURE,
        start_authorizer=authorize,
    )
    # No public worker listener. The native client still validates the fenced
    # request, complete scientific snapshot, response headers and byte stream.
    worker = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @worker.exception_handler(JobManagerError)
    async def worker_error(_request, exc):
        return JSONResponse(
            status_code=exc.status_code, content={"error": {"code": exc.code, "message": str(exc)}}
        )

    @worker.get("/health")
    def health():
        return {
            **manager.health_state(),
            "status": "ok",
            "runtime_ready": True,
            "start_authorization_version": 1,
        }

    @worker.post("/jobs")
    async def submit(request: JobSubmitRequest, response: Response):
        replay = manager.replay_submission(request)
        if replay is not None:
            return replay
        await asyncio.to_thread(manager.validate_submission, request)
        # Like the native UDS worker, mutate its queue and wake its event on the
        # owning loop. Offload only the CPU-bound prevalidation.
        snapshot, created = manager.submit(request, chemistry_validated=True)
        response.status_code = 202 if created else 200
        return snapshot

    @worker.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return manager.get(job_id)

    def file_response(stream, size, digest, media):
        return StreamingResponse(
            iter(lambda: stream.read(65536), b""),
            media_type=media,
            headers={"Content-Length": str(size), "ETag": f'"{digest}"'},
            background=BackgroundTask(stream.close),
        )

    @worker.get("/jobs/{job_id}/artifacts/{artifact_id}")
    async def artifact(job_id: str, artifact_id: str):
        access = await asyncio.to_thread(manager.artifact, job_id, artifact_id)
        return file_response(
            access.stream,
            access.descriptor.size_bytes,
            access.descriptor.sha256,
            access.descriptor.media_type,
        )

    @worker.get("/jobs/{job_id}/bundle")
    async def bundle(job_id: str):
        access = await asyncio.to_thread(manager.bundle, job_id)
        return file_response(access.stream, access.size_bytes, access.sha256, "application/zip")

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker), base_url="http://m3-worker.local"
    )
    native = MonomerDftWorkerClient(
        base_url="http://m3-worker.local",
        uds_path="in-process-development-only",
        validation_limiter=limiter,
        client=client,
    )

    @asynccontextmanager
    async def lifespan():
        async with client:
            await manager.start()
            try:
                yield
            finally:
                await manager.stop()

    return native, lifespan

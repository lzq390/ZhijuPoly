from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from polymer_md.contracts import DistanceRequest, SubmitRequest
from .config import WorkerSettings, load_settings
from .manager import JobManager, WorkerError


class PrivateFileResponse(FileResponse):
    def __init__(self, *, manager, job_id, **kwargs):
        super().__init__(**kwargs)
        self.manager, self.job_id = manager, job_id

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(self.manager.release_read(self.job_id))


def create_app(settings: WorkerSettings | None = None, *, manager: JobManager | None = None) -> FastAPI:
    active = manager or JobManager(settings or load_settings())

    @asynccontextmanager
    async def lifespan(app):
        await active.start()
        try:
            yield
        finally:
            await active.stop()

    app = FastAPI(title="Private polymer MD CPU worker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = active

    @app.exception_handler(WorkerError)
    async def worker_error(_request: Request, error: WorkerError):
        return JSONResponse(status_code=error.status_code,
                            content={"error": {"code": error.code, "message": str(error)}})

    @app.exception_handler(ValueError)
    async def invalid_analysis(_request: Request, _error: ValueError):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_analysis_request", "message": "analysis input or measured artifact is invalid"}})

    @app.get("/health")
    async def health():
        return await active.health()

    @app.get("/capabilities")
    async def capabilities():
        return await active.capabilities()

    @app.post("/drain")
    async def drain():
        await active.set_draining(True)
        return await active.health()

    @app.post("/resume")
    async def resume():
        await active.set_draining(False)
        return await active.health()

    @app.post("/jobs")
    async def submit(payload: SubmitRequest, response: Response):
        snapshot, created = await active.submit(payload)
        response.status_code = 202 if created else 200
        return snapshot

    @app.get("/jobs")
    async def list_jobs():
        values = [active.get(job_id) for job_id in active.records]
        return {"jobs": sorted(values, key=lambda item: item["enqueue_sequence"]), "total": len(values)}

    @app.get("/jobs/{job_id}")
    async def status(job_id: str):
        return active.get(job_id)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel(job_id: str, payload: SubmitRequest):
        return await active.cancel(job_id, payload)

    @app.get("/jobs/{job_id}/artifacts/{artifact_id}")
    async def artifact(job_id: str, artifact_id: str, attempt_token: str = Query(pattern=r"^[0-9a-f]{64}$")):
        active.assert_attempt(job_id, attempt_token)
        await active.acquire_read(job_id)
        try:
            path, descriptor = await asyncio.to_thread(active.artifact, job_id, artifact_id)
            return PrivateFileResponse(manager=active, job_id=job_id, path=path, filename=descriptor["filename"],
                media_type=descriptor["media_type"], headers={"ETag": '"' + descriptor["sha256"] + '"', "Cache-Control": "no-store"})
        except BaseException:
            await active.release_read(job_id)
            raise

    @app.delete("/jobs/{job_id}/artifacts")
    async def delete_artifacts(job_id: str, payload: SubmitRequest):
        return await active.delete(job_id, payload)

    @app.delete("/jobs/{job_id}")
    async def delete_job(job_id: str, payload: SubmitRequest):
        return await active.delete(job_id, payload, purge=True)

    @app.get("/jobs/{job_id}/stages/{stage_run_id}/trajectory")
    async def trajectory(job_id: str, stage_run_id: str, attempt_token: str = Query(pattern=r"^[0-9a-f]{64}$"),
                         offset: int = Query(0, ge=0), frame_index: int | None = Query(None, ge=0), limit: int = Query(10000, ge=1, le=10000)):
        active.assert_attempt(job_id, attempt_token)
        return await active.analyze(job_id, stage_run_id, "trajectory", offset if frame_index is None else frame_index, limit)

    @app.get("/jobs/{job_id}/stages/{stage_run_id}/logs")
    async def logs(job_id: str, stage_run_id: str, attempt_token: str = Query(pattern=r"^[0-9a-f]{64}$"),
                   tail_lines: int = Query(100, ge=1, le=1000)):
        active.assert_attempt(job_id, attempt_token)
        await active.acquire_read(job_id)
        try:
            return await asyncio.to_thread(active.logs, job_id, stage_run_id, tail_lines)
        finally:
            await active.release_read(job_id)

    @app.post("/jobs/{job_id}/stages/{stage_run_id}/distance")
    async def distance(job_id: str, stage_run_id: str, payload: DistanceRequest,
                       attempt_token: str = Query(pattern=r"^[0-9a-f]{64}$")):
        active.assert_attempt(job_id, attempt_token)
        return await active.analyze(job_id, stage_run_id, "distance", payload.model_dump(mode="json"))

    return app


app = create_app()

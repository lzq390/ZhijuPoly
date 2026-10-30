from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from .config import AnalysisSettings
from .contracts import AnalysisError, DistanceAnalysisRequest, TrajectoryAnalysisRequest
from .service import AnalysisService


class BoundedRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def bounded(request):
            if request.method == "POST":
                size, chunks = 0, []
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 2 * 1024 ** 2:
                        raise AnalysisError("analysis_request_too_large", "分析请求超过允许大小。", 413)
                    chunks.append(chunk)
                request._body = b"".join(chunks)
            response = await handler(request)
            response.headers["Cache-Control"] = "private, no-store"
            return response
        return bounded


def create_app(settings: AnalysisSettings | None = None, *, service: AnalysisService | None = None):
    active = service or AnalysisService(settings or AnalysisSettings.from_env())

    @asynccontextmanager
    async def lifespan(_app):
        await active.start()
        try:
            yield
        finally:
            await active.stop()

    app = FastAPI(title="Private read-only polymer MD analysis", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.router.route_class = BoundedRoute
    app.state.analysis_service = active

    @app.exception_handler(AnalysisError)
    async def analysis_error(_request, error):
        return JSONResponse(status_code=error.status, content=error.body(), headers={"Retry-After": "2"} if error.status == 429 else None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse(status_code=422, content={"code": "invalid_analysis_request", "message": "分析请求或来源身份无效。", "retryable": False})

    @app.get("/health")
    async def health():
        return active.health()

    async def perform(request, operation, payload):
        analysis = asyncio.create_task(active.analyze(operation, payload))
        async def disconnected():
            while True:
                # The complete JSON body was consumed by BoundedRoute. A raw
                # receive now waits only for disconnect. is_disconnected() uses
                # a cancel scope that can swallow our monitor's cancellation.
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return
        monitor = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({analysis, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if analysis in done:
                return await analysis
            analysis.cancel()
            await asyncio.gather(analysis, return_exceptions=True)
            return JSONResponse(status_code=499, content={"code": "analysis_cancelled", "message": "分析已取消。", "retryable": False})
        finally:
            monitor.cancel()
            if not analysis.done():
                analysis.cancel()
            await asyncio.gather(monitor, analysis, return_exceptions=True)

    @app.post("/distance")
    async def distance(request: Request, payload: DistanceAnalysisRequest):
        return await perform(request, "distance", payload)

    @app.post("/trajectory")
    async def trajectory(request: Request, payload: TrajectoryAnalysisRequest):
        return await perform(request, "trajectory", payload)

    return app


app = create_app()

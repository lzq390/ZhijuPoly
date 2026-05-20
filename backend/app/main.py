from __future__ import annotations

import math
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.database import sqlite_connection
from app.middleware import BrowserCrossSiteProtectionMiddleware
from app.postgres_database import postgres_connection
from app.routers.conditional_generation import router as conditional_generation_router
from app.routers.database_browser import router as database_browser_router
from app.routers.dft import router as dft_router
from app.routers.knowledge import router as knowledge_router
from app.routers.lab_data import router as lab_data_router
from app.routers.online_knowledge import router as online_knowledge_router
from app.routers.predict import router as predict_router
from app.routers.query import router as query_router
from app.routers.reverse_design import router as reverse_design_router
from app.services.conditional_generation_jobs import ConditionalGenerationJobManager
from app.services.reverse_design_jobs import ReverseDesignJobManager


async def health() -> dict[str, str]:
    return {"status": "ok"}


def _json_safe_validation_error(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {key: _json_safe_validation_error(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_validation_error(item) for item in value]
    if isinstance(value, BaseException):
        return str(value)
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(api_app: FastAPI):
        try:
            yield
        finally:
            api_app.state.reverse_design_job_manager.shutdown(wait=False)
            api_app.state.conditional_generation_job_manager.shutdown(wait=False)

    app = FastAPI(title="PolyProp API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe_validation_error(exc.errors())},
        )

    app.state.settings = app_settings
    app.state.sqlite_connection_factory = sqlite_connection
    app.state.postgres_connection_factory = postgres_connection
    app.state.reverse_design_job_manager = ReverseDesignJobManager(max_workers=app_settings.pi_reverse_job_workers)
    app.state.conditional_generation_job_manager = ConditionalGenerationJobManager(
        max_workers=app_settings.gen_job_workers,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(BrowserCrossSiteProtectionMiddleware)

    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(query_router)
    app.include_router(predict_router)
    app.include_router(conditional_generation_router)
    app.include_router(knowledge_router)
    app.include_router(lab_data_router)
    app.include_router(online_knowledge_router)
    app.include_router(dft_router)
    app.include_router(database_browser_router)
    app.include_router(reverse_design_router)

    return app


app = create_app()

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.database import sqlite_connection
from app.routers.dft import router as dft_router
from app.routers.knowledge import router as knowledge_router
from app.routers.online_knowledge import router as online_knowledge_router
from app.routers.predict import router as predict_router
from app.routers.query import router as query_router
from app.routers.reverse_design import router as reverse_design_router


async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    app = FastAPI(title="PolyProp API", version="0.1.0")
    app.state.settings = app_settings
    app.state.sqlite_connection_factory = sqlite_connection

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(query_router)
    app.include_router(predict_router)
    app.include_router(knowledge_router)
    app.include_router(online_knowledge_router)
    app.include_router(dft_router)
    app.include_router(reverse_design_router)

    return app


app = create_app()

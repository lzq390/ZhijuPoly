"""Lightweight development entrypoint using the formal browsing-recording routes."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings
from app.knowledge_poc_trace import KnowledgeTraceMiddleware
from app.middleware import BrowserCrossSiteProtectionMiddleware
from app.models import PropertyFilterOptionsResponse, PropertyFilterHistogramResponse
from app.recording_models import RecordedFilterSearchResponse
from app.postgres_database import postgres_connection
from app.routers.database_browser import get_property_filter_options, get_property_filter_histogram, search_property_filter
from app.routers.knowledge import router as knowledge_router
from app.routers.browsing_recording import router as browsing_recording_router
from app.services.browsing_recording import BrowsingRecordingStore, MAX_RECENT_SEARCHES


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    app = FastAPI(title="Knowledge Summary POC", version="0.1.0")
    app.state.settings = app_settings
    app.state.postgres_connection_factory = postgres_connection
    app.state.browsing_recording = BrowsingRecordingStore(app_settings)
    app.include_router(knowledge_router)
    app.include_router(browsing_recording_router)
    # The lightweight entrypoint exposes only SQL-based property filtering.
    for path, endpoint, method, response_model in (
        ("options", get_property_filter_options, "GET", PropertyFilterOptionsResponse),
        ("histogram", get_property_filter_histogram, "GET", PropertyFilterHistogramResponse),
        ("search", search_property_filter, "POST", RecordedFilterSearchResponse),
    ):
        app.add_api_route(f"/api/v1/database-browser/property-filter/{path}", endpoint,
                          methods=[method], response_model=response_model)
    app.add_middleware(
        CORSMiddleware, allow_origins=app_settings.allowed_origins_list,
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
        expose_headers=["ETag", "Server-Timing"],
    )
    app.add_middleware(BrowserCrossSiteProtectionMiddleware)
    app.add_middleware(KnowledgeTraceMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app

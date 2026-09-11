"""Exercise the real business routers without scientific runtimes or a database."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import PropertyFilterSearchResponse
from app.routers import database_browser, knowledge
from test_knowledge_poc_recording import sample_search


def test_existing_python_callers_can_use_the_original_search_request(monkeypatch):
    import asyncio
    from fastapi import Request
    from app.models import KnowledgeSearchRequest
    from app.services.browsing_recording import BrowsingRecordingStore

    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)
    app = FastAPI()
    app.state.settings = Settings()
    app.state.browsing_recording = BrowsingRecordingStore(app.state.settings)
    response = asyncio.run(knowledge.search_knowledge(
        KnowledgeSearchRequest(query="legacy"), Request({"type": "http", "app": app})))
    assert response.query == "legacy"
    assert response.search_id


def test_formal_search_routes_preserve_payload_and_add_recording_snapshots(monkeypatch):
    from app.services.browsing_recording import BrowsingRecordingStore
    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)
    app = FastAPI()
    app.state.settings = Settings()
    app.state.browsing_recording = BrowsingRecordingStore(app.state.settings)
    app.include_router(knowledge.router)
    app.include_router(database_browser.router)
    with TestClient(app) as client:
        response = client.post("/api/v1/knowledge/search", json={"query": "polyimide"})
        assert response.status_code == 200
        assert response.json()["results"][0]["title_en"] == "polyimide"
        assert response.json()["search_id"]


def test_formal_routers_share_recording_and_freeze_both_modules(monkeypatch):
    from app.routers.browsing_recording import router
    from app.services.browsing_recording import BrowsingRecordingStore

    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)

    def filter_search(body, request, response):
        response.headers["Cache-Control"] = "no-store"
        return PropertyFilterSearchResponse(query=body.q, page=body.page, page_size=body.page_size,
            query_time_ms=1, total_records=0, matched_records=0, results=[])

    monkeypatch.setattr(database_browser, "_search_property_filter_sync", filter_search)
    app = FastAPI()
    app.state.settings = Settings()
    app.state.browsing_recording = BrowsingRecordingStore(app.state.settings)
    app.include_router(knowledge.router)
    app.include_router(database_browser.router)
    app.include_router(router)
    with TestClient(app) as client:
        assert client.post("/api/v1/knowledge/recordings", json={"recording_id": "formal"}).status_code == 200
        search = client.post("/api/v1/knowledge/search", json={"query": "polyimide", "recording_id": "formal"})
        assert search.status_code == 200
        assert client.post("/api/v1/knowledge/observations", json={"search_id": search.json()["search_id"],
            "recording_id": "formal", "knowledge_id": 1, "source": "result_card"}).status_code == 200
        filtered = client.post("/api/v1/database-browser/property-filter/search", json={
            "recording_id": "formal", "filters": [{"filter_type": "standardized", "property_key": "tg", "min_value": 100}]})
        assert filtered.status_code == 200
        assert filtered.headers["cache-control"] == "no-store"
        frozen = client.post("/api/v1/knowledge/recordings/formal/stop").json()
        assert [event["event"] for event in frozen["events"]] == [
            "search.completed", "article.opened", "property_filter.search_completed"]
        assert client.post("/api/v1/knowledge/search", json={"query": "later", "recording_id": "formal"}).status_code == 409
        assert client.post("/api/v1/knowledge/recordings/formal/stop").json() == frozen
        assert client.post("/api/v1/knowledge/search", json={"query": "unrecorded"}).status_code == 200
        assert client.post("/api/v1/knowledge/recordings/formal/stop").json() == frozen

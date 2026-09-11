"""Formal entrypoint regression; uses full backend dependencies, no live services."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import KnowledgeDocumentResult, KnowledgeSearchResponse, PropertyFilterSearchResponse
from app.routers import database_browser, knowledge
from app.services import browsing_recording


def test_main_records_both_modules_and_reuses_frozen_summary(monkeypatch):
    def search(body, app):
        return KnowledgeSearchResponse(query=body.query, query_time_ms=1, total=1, results=[
            KnowledgeDocumentResult(knowledge_id=1, source_file="sample.pdf", source_row_number=1,
                title_en="Sample article", abstract="Sample abstract", abstract_snippet="Sample")])

    def filter_search(body, request, response):
        return PropertyFilterSearchResponse(query=body.q, page=body.page, page_size=body.page_size,
            query_time_ms=1, total_records=0, matched_records=0, results=[])

    calls = []

    async def summarize(events, settings):
        calls.append(events)
        return {"summary": "正式入口的浏览回顾", "generated": True}

    monkeypatch.setattr(knowledge, "_search_knowledge_sync", search)
    monkeypatch.setattr(database_browser, "_search_property_filter_sync", filter_search)
    monkeypatch.setattr(browsing_recording, "generate_knowledge_summary", summarize)
    app = create_app(Settings(app_postgres_dsn="postgresql://unused:unused@127.0.0.1:1/unused",
        deployment_drain_enabled=False, model_enabled=False))
    # Test route registration and middleware; do not run scientific startup or background database jobs.
    client = TestClient(app)
    try:
        base = "/api/v1/knowledge"
        assert client.post(base + "/recordings", json={"recording_id": "formal"}).status_code == 200
        found = client.post(base + "/search", json={"query": "sample", "recording_id": "formal"})
        assert found.status_code == 200
        assert client.post(base + "/observations", json={"recording_id": "formal", "search_id": found.json()["search_id"],
            "knowledge_id": 1, "source": "reaction_tab"}).status_code == 200
        assert client.post("/api/v1/database-browser/property-filter/search", json={"recording_id": "formal",
            "filters": [{"filter_type": "standardized", "property_key": "tg", "min_value": 100}]}).status_code == 200
        frozen = client.post(base + "/recordings/formal/stop")
        assert frozen.status_code == 200
        assert [event["event"] for event in frozen.json()["events"]] == [
            "search.completed", "article.reaction_viewed", "property_filter.search_completed"]
        summary = client.post(base + "/recordings/formal/summary")
        assert summary.status_code == 200
        assert summary.json()["summary"] == "正式入口的浏览回顾"
        assert client.post(base + "/recordings/formal/summary").json() == summary.json()
        assert len(calls) == 1
        assert client.post(base + "/search", json={"query": "later", "recording_id": "formal"}).status_code == 409
        assert client.post(base + "/search", json={"query": "ordinary"}).status_code == 200
        assert client.post(base + "/recordings/formal/stop").json() == frozen.json()
        routes = [route.path for route in app.routes if "POST" in getattr(route, "methods", set())]
        for path in (base + "/search", base + "/recordings", base + "/observations",
                     "/api/v1/database-browser/property-filter/search"):
            assert routes.count(path) == 1
    finally:
        client.close()

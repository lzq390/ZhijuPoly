import json
import logging

import pytest

from app.models import KnowledgeDocumentResult, KnowledgeSearchResponse
from app.routers import knowledge
from test_knowledge_poc_app import make_client


@pytest.fixture
def client(monkeypatch):
    def search(body, app):
        article_id = 1 if body.query == "polyimide" else 2
        return KnowledgeSearchResponse(query=body.query, query_time_ms=1, total=1, results=[
            KnowledgeDocumentResult(knowledge_id=article_id, source_file="sample.pdf",
                                    source_row_number=1, title_en=f"Paper {article_id}",
                                    abstract="Original abstract", abstract_snippet="Original")
        ])
    monkeypatch.setattr(knowledge, "_search_knowledge_sync", search)
    with make_client() as current:
        yield current


def test_opening_an_earlier_result_keeps_its_original_search_and_content(client, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    first = client.post("/api/v1/knowledge/search", json={"query": "polyimide"}).json()
    second = client.post("/api/v1/knowledge/search", json={"query": "absorbance"}).json()
    assert first["search_id"] != second["search_id"]
    payload = {"search_id": first["search_id"], "knowledge_id": 1, "source": "result_card"}
    response = client.post("/api/v1/knowledge/observations", json=payload)
    assert response.status_code == 200
    assert response.json()["event"] == "article.opened"
    assert response.json()["query"] == "polyimide"
    assert response.json()["title_en"] == "Paper 1"
    assert response.json()["abstract_characters"] == len("Original abstract")
    traces = [json.loads(row.args[0]) for row in caplog.records if row.msg == "KNOWLEDGE_TRACE %s"]
    assert traces[-1]["request"] == payload
    assert traces[-1]["response"]["search_id"] == first["search_id"]
    assert traces[-1]["response"]["knowledge_id"] == 1


def test_unknown_search_and_articles_outside_its_results_are_rejected(client):
    payload = {"search_id": "missing", "knowledge_id": 1, "source": "result_card"}
    assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 410
    result = client.post("/api/v1/knowledge/search", json={"query": "polyimide"}).json()
    payload.update(search_id=result["search_id"], knowledge_id=2)
    assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 404
    payload.update(knowledge_id=1, source="automatic")
    assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 422


def test_old_searches_expire_when_the_poc_snapshot_limit_is_reached(client):
    from app.knowledge_poc import MAX_RECENT_SEARCHES
    first = client.post("/api/v1/knowledge/search", json={"query": "polyimide"}).json()
    for _ in range(MAX_RECENT_SEARCHES):
        latest = client.post("/api/v1/knowledge/search", json={"query": "polyimide"}).json()
    payload = {"search_id": first["search_id"], "knowledge_id": 1, "source": "drawer_reopen"}
    assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 410
    payload["search_id"] = latest["search_id"]
    assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 200


@pytest.mark.parametrize("has_content", [True, False])
def test_reaction_view_records_original_server_fields_including_missing_values(monkeypatch, caplog, has_content):
    fields = {name: None for name in (
        "polymer_iupac", "formulation", "catalyst", "solvent", "temperature",
        "reaction_time", "analysis", "judgement_reason",
    )}
    if has_content:
        fields.update(formulation="dianhydride + diamine", solvent="NMP", temperature="80 °C")

    def search(body, app):
        return KnowledgeSearchResponse(query=body.query, query_time_ms=1, total=1, results=[
            KnowledgeDocumentResult(knowledge_id=1, source_file="sample.pdf", source_row_number=1,
                                    title_en=body.query, abstract="Original abstract",
                                    abstract_snippet="Original", **fields)
        ])

    monkeypatch.setattr(knowledge, "_search_knowledge_sync", search)
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    with make_client() as client:
        first = client.post("/api/v1/knowledge/search", json={"query": "first"}).json()
        client.post("/api/v1/knowledge/search", json={"query": "second"})
        payload = {"search_id": first["search_id"], "knowledge_id": 1, "source": "reaction_tab"}
        response = client.post("/api/v1/knowledge/observations", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["event"] == "article.reaction_viewed"
        assert result["query"] == result["title_en"] == "first"
        assert result["reaction_info"] == fields
        traces = [json.loads(row.args[0]) for row in caplog.records if row.msg == "KNOWLEDGE_TRACE %s"]
        assert traces[-1]["request"] == payload
        assert traces[-1]["response"]["reaction_info"] == fields
        assert traces[-1]["response"]["event"] == "article.reaction_viewed"
        assert client.post("/api/v1/knowledge/observations", json={**payload, "knowledge_id": 2}).status_code == 404
        assert client.post("/api/v1/knowledge/observations", json={**payload, "search_id": "missing"}).status_code == 410
        assert client.post("/api/v1/knowledge/observations", json={**payload, "reaction_info": fields}).status_code == 422

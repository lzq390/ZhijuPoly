import json
import logging

import pytest
from fastapi import Request, Response

from app.models import PropertyFilterRecord, PropertyFilterSearchRequest, PropertyFilterSearchResponse, PropertyFilterSearchResult
from test_knowledge_poc_app import make_client

PATH = "/api/v1/database-browser/property-filter"
FILTER = {"filter_type": "standardized", "property_key": "tg", "min_value": 100, "canonical_unit": "C"}


@pytest.fixture
def client(monkeypatch):
    def search(body: PropertyFilterSearchRequest, request: Request, response: Response):
        return PropertyFilterSearchResponse(query=body.q, page=body.page, page_size=body.page_size,
            query_time_ms=1, total_records=2, matched_records=1, results=[
                PropertyFilterSearchResult(polymer_name=body.q or "Material A", smiles="*CC*",
                    canonical_smiles="*CC*", matched_filters=1, records=[
                        PropertyFilterRecord(filter_record_id=i, source_row_number=i, property_category="Thermal",
                            property_name="Tg", property_value=str(value), canonical_value=value,
                            canonical_unit="C", filter_index=0)
                        for i, value in [(1, 180), (2, 190)]])])
    monkeypatch.setattr("app.knowledge_poc.search_property_filter", search)
    with make_client() as current:
        yield current


def search(client, query="first"):
    response = client.post(PATH + "/search", json={"filters": [FILTER], "q": query})
    assert response.status_code == 200
    return response.json()["search_id"]


def test_measurement_open_uses_original_search_and_logs_all_measurements(client, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    first = search(client)
    assert search(client, "second") != first
    payload = {"search_id": first, "result_index": 0, "source": "measurement_details", "filter_index": 0}
    response = client.post(PATH + "/observations", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["event"] == "property_filter.measurements_viewed"
    assert data["query"] == data["polymer_name"] == "first"
    assert data["filters"][0]["min_value"] == 100
    assert [row["canonical_value"] for row in data["records"]] == [180, 190]
    assert "smiles_value" not in data
    traces = [json.loads(row.args[0]) for row in caplog.records if row.msg == "KNOWLEDGE_TRACE %s"]
    assert traces[-1]["request"] == payload
    assert traces[-1]["response"]["records"] == data["records"]


@pytest.mark.parametrize("field", ["smiles", "canonical_smiles"])
def test_smiles_open_returns_only_the_requested_structure(client, field):
    payload = {"search_id": search(client), "result_index": 0, "source": "smiles", "smiles_field": field}
    response = client.post(PATH + "/observations", json=payload)
    assert response.status_code == 200
    assert response.json()["event"] == "property_filter.smiles_viewed"
    assert response.json()["smiles_field"] == field
    assert response.json()["smiles_value"] == "*CC*"
    assert "records" not in response.json()


def test_unknown_results_and_fabricated_observation_fields_are_rejected(client):
    payload = {"search_id": search(client), "result_index": 0, "source": "measurement_details", "filter_index": 0}
    for changes, status in [({"search_id": "missing"}, 410), ({"result_index": 1}, 404),
                            ({"filter_index": 1}, 404), ({"filter_index": None}, 422),
                            ({"source": "automatic"}, 422), ({"records": []}, 422),
                            ({"smiles_field": "smiles"}, 422)]:
        assert client.post(PATH + "/observations", json={**payload, **changes}).status_code == status


def test_evicted_filter_search_requires_a_new_search(client):
    from app.knowledge_poc import MAX_RECENT_SEARCHES
    first = search(client)
    for _ in range(MAX_RECENT_SEARCHES):
        latest = search(client)
    payload = {"search_id": first, "result_index": 0, "source": "smiles", "smiles_field": "smiles"}
    assert client.post(PATH + "/observations", json=payload).status_code == 410
    assert client.post(PATH + "/observations", json={**payload, "search_id": latest}).status_code == 200


def test_mixed_recording_freezes_both_modules_and_summarizes_only_viewed_content(client, monkeypatch):
    from app.routers import knowledge
    from app.services.knowledge_poc_summary import build_summary_evidence
    from test_knowledge_poc_recording import sample_search, start, stop
    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)
    evidence = []

    async def summarize(events, settings):
        evidence.append(build_summary_evidence(events))
        return {"summary": "跨模块总结", "generated": True}

    monkeypatch.setattr("app.knowledge_poc.generate_knowledge_summary", summarize)
    search(client, "before")
    start(client, "mixed")
    article = client.post("/api/v1/knowledge/search", json={"query": "polyimide", "recording_id": "mixed"}).json()
    client.post("/api/v1/knowledge/observations", json={"recording_id": "mixed", "search_id": article["search_id"],
        "knowledge_id": 1, "source": "result_card"})
    result = client.post(PATH + "/search", json={"filters": [FILTER], "q": "viewed", "recording_id": "mixed"})
    assert result.status_code == 200
    target = {"recording_id": "mixed", "search_id": result.json()["search_id"], "result_index": 0}
    for source in [{"source": "measurement_details", "filter_index": 0}, {"source": "smiles", "smiles_field": "smiles"}]:
        assert client.post(PATH + "/observations", json={**target, **source}).status_code == 200
    client.post(PATH + "/search", json={"filters": [FILTER], "q": "unviewed", "recording_id": "mixed"})
    frozen = stop(client, "mixed").json()
    assert [event["event"] for event in frozen["events"]] == ["search.completed", "article.opened",
        "property_filter.search_completed", "property_filter.measurements_viewed", "property_filter.smiles_viewed",
        "property_filter.search_completed"]
    assert client.post("/api/v1/knowledge/recordings/mixed/summary").status_code == 200
    assert len(evidence[0]["articles"]) == len(evidence[0]["materials"]) == 1
    material = evidence[0]["materials"][0]
    assert material["polymer_name"] == "viewed"
    assert len(material["measurements"]) == 2 and material["structures"] == {"smiles": "*CC*"}
    assert "smiles" not in material["measurements"][0]
    assert client.post(PATH + "/observations", json={**target, "source": "smiles", "smiles_field": "smiles"}).status_code == 409
    assert client.post(PATH + "/search", json={"filters": [FILTER], "recording_id": "mixed"}).status_code == 409
    search(client, "after")
    assert stop(client, "mixed").json() == frozen


def test_recorded_filter_survives_cache_eviction_and_cannot_join_another_record(client):
    from app.knowledge_poc import MAX_RECENT_SEARCHES
    from test_knowledge_poc_recording import start, stop
    start(client, "one")
    start(client, "two")
    result = client.post(PATH + "/search", json={"filters": [FILTER], "recording_id": "one"})
    assert result.status_code == 200
    target = {"search_id": result.json()["search_id"], "result_index": 0, "source": "smiles", "smiles_field": "smiles"}
    assert client.post(PATH + "/observations", json={**target, "recording_id": "two"}).status_code == 409
    for _ in range(MAX_RECENT_SEARCHES):
        search(client)
    assert client.post(PATH + "/observations", json={**target, "recording_id": "one"}).status_code == 200
    assert stop(client, "two").json()["events"] == []


def test_stop_waits_for_recorded_filter_and_retains_failure(client, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    from test_knowledge_poc_recording import start, stop
    entered, release = threading.Event(), threading.Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        raise HTTPException(503, "unavailable")

    monkeypatch.setattr("app.knowledge_poc.search_property_filter", slow)
    start(client, "one")
    with ThreadPoolExecutor() as pool:
        task = pool.submit(client.post, PATH + "/search", json={"filters": [FILTER], "recording_id": "one"})
        try:
            assert entered.wait(5)
            assert stop(client, "one").status_code == 409
        finally:
            release.set()
        assert task.result().status_code == 503
    events = stop(client, "one").json()["events"]
    assert len(events) == 1 and events[0]["event"] == "property_filter.search_failed"

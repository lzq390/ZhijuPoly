import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.knowledge_poc_trace import KnowledgeTraceMiddleware


def test_search_observation_preserves_payload_and_response(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    app = FastAPI()
    app.add_middleware(KnowledgeTraceMiddleware)
    payload = {"query": "polyimide", "page": 1, "top_k": 20}
    result = {"total": 1, "results": [{"knowledge_id": 1, "title_en": "Paper A", "abstract": "Sample abstract"}]}

    @app.post("/api/v1/knowledge/search")
    def search(body: dict):
        assert body == payload
        return result

    with TestClient(app) as client:
        response = client.post("/api/v1/knowledge/search", json=payload,
                               headers={"Authorization": "Bearer do-not-log"})
    assert response.status_code == 200
    assert response.json() == result
    record = next(record for record in caplog.records if record.msg == "KNOWLEDGE_TRACE %s")
    event = json.loads(record.args[0])
    assert event["request"] == payload
    assert event["response"]["total"] == 1
    assert event["response"]["results"][0]["knowledge_id"] == 1
    assert event["response"]["results"][0]["abstract_preview"] == "Sample abstract"
    assert "do-not-log" not in record.getMessage()


@pytest.mark.parametrize("path", ["/api/v1/knowledge/search", "/api/v1/database-browser/property-filter/search"])
def test_observation_marks_large_bodies_without_changing_response(caplog, path):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    app = FastAPI()
    app.add_middleware(KnowledgeTraceMiddleware, max_body_bytes=64)

    @app.post(path)
    def search(body: dict):
        return body

    payload = {"query": "a" * 200}
    with TestClient(app) as client:
        response = client.post(path, json=payload)
    assert response.json() == payload
    record = next(record for record in caplog.records if record.msg == "KNOWLEDGE_TRACE %s")
    event = json.loads(record.args[0])
    assert event["truncated"] == {"request": True, "response": True}


def test_observation_includes_unknown_api_requests_but_skips_health(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    app = FastAPI()
    app.add_middleware(KnowledgeTraceMiddleware)
    with TestClient(app) as client:
        client.get("/health")
        client.get("/api/v1/unknown")
    records = [record for record in caplog.records if record.msg == "KNOWLEDGE_TRACE %s"]
    assert len(records) == 1
    event = json.loads(records[0].args[0])
    assert event["path"] == "/api/v1/unknown"
    assert event["status"] == 404


def test_filter_observation_links_submitted_conditions_to_returned_measurements(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    app = FastAPI()
    app.add_middleware(KnowledgeTraceMiddleware)
    condition = {"filter_type": "standardized", "property_key": "tg", "canonical_unit": "C", "min_value": 200}
    payload = {"filters": [condition], "q": "POC-C", "page": 1, "page_size": 25, "private": "omit-input"}
    measurement = {"filter_record_id": 9000007, "filter_index": 0, "property_key": "tg",
                   "canonical_value": 300, "canonical_unit": "C", "value_origin": "synthetic_poc"}
    result = {"query": "POC-C", "matched_records": 1, "total_records": 13, "page": 1, "page_size": 25,
              "results": [{"polymer_name": "POC-C 演示材料", "smiles": "*CC(c1ccccc1)*", "matched_filters": 1,
                           "records": [{**measurement, "private": "omit-measurement"}], "private": "omit-material"}]}

    @app.post("/api/v1/database-browser/property-filter/search")
    def search(body: dict):
        assert body == payload
        return result

    with TestClient(app) as client:
        response = client.post("/api/v1/database-browser/property-filter/search", json=payload,
                               headers={"Authorization": "Bearer omit-header"})
    assert response.json() == result
    record = next(record for record in caplog.records if record.msg == "KNOWLEDGE_TRACE %s")
    event = json.loads(record.args[0])
    assert event["request"] == {key: payload[key] for key in ("filters", "q", "page", "page_size")}
    assert event["response"]["matched_records"] == 1
    assert event["response"]["results"][0]["polymer_name"] == "POC-C 演示材料"
    assert event["response"]["results"][0]["records"] == [measurement]
    assert event["status"] == 200 and event["completed"] is True
    assert "omit-" not in record.getMessage()

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.knowledge_poc import create_app
from app.routers import knowledge
from test_knowledge_poc_recording import sample_search, search, start, stop


@pytest.fixture
def setup(monkeypatch):
    calls = []
    responses = [httpx.Response(200, json={"choices": [{"message": {"content": "本次查看了文献 #1。"}, "finish_reason": "stop"}]})]

    def respond(request):
        calls.append(json.loads(request.content))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)
    settings = Settings(app_postgres_dsn="postgresql://unused:unused@127.0.0.1:1/unused",
                        assistant_api_key="test-secret", assistant_base_url="https://model.example/v1",
                        assistant_model="test-model", ai_proxy_url="", online_knowledge_proxy_url="")
    with TestClient(create_app(settings)) as client:
        yield client, calls, responses


def observe(client, search_id, source="result_card"):
    return client.post("/api/v1/knowledge/observations", json={
        "recording_id": "one", "search_id": search_id, "knowledge_id": 1, "source": source})


def summarize(client, name="one"):
    return client.post(f"/api/v1/knowledge/recordings/{name}/summary")


def test_summary_requires_frozen_record_and_empty_record_does_not_call_model(setup):
    client, calls, _ = setup
    assert summarize(client, "missing").status_code == 410
    start(client, "one")
    assert summarize(client).status_code == 409
    stop(client, "one")
    result = summarize(client)
    assert result.status_code == 200
    assert result.json()["generated"] is False
    assert "没有记录" in result.json()["summary"]
    assert calls == []


def test_summary_uses_frozen_evidence_and_repeated_requests_reuse_result(setup):
    client, calls, _ = setup
    start(client, "one")
    first = search(client, "first", "one").json()
    observe(client, first["search_id"])
    observe(client, first["search_id"])
    observe(client, first["search_id"], "reaction_tab")
    search(client, "unopened", "one")
    stop(client, "one")
    result = summarize(client)
    assert result.status_code == 200
    assert result.json()["generated"] is True
    assert summarize(client).json() == result.json()
    assert len(calls) == 1
    evidence = json.loads(calls[0]["messages"][1]["content"])
    assert len(evidence["events"]) == 5
    assert len(evidence["articles"]) == 1
    assert evidence["articles"][0]["abstract"] == "Abstract: first"
    assert evidence["articles"][0]["citation"] == "[文献 #1]"
    assert evidence["articles"][0]["article_ref"] == first["search_id"] + ":1"
    assert evidence["articles"][0]["reaction_info"]["formulation"] == "Original formula"
    assert "Abstract: unopened" not in calls[0]["messages"][1]["content"]
    assert search(client, "after", "one").status_code == 409


@pytest.mark.parametrize("failure", [
    httpx.Response(401, text="test-secret provider response"),
    httpx.ReadTimeout("test-secret internal endpoint"),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
    httpx.Response(200, json={"choices": [{"message": {"content": "cut off"}, "finish_reason": "length"}]}),
])
def test_failure_is_safe_and_retry_uses_same_frozen_record(setup, failure):
    client, calls, responses = setup
    responses.insert(0, failure)
    start(client, "one")
    result = search(client, "first", "one").json()
    observe(client, result["search_id"])
    frozen = stop(client, "one").json()
    failed = summarize(client)
    assert failed.status_code in (502, 504)
    assert "test-secret" not in failed.text
    assert stop(client, "one").json() == frozen
    assert summarize(client).status_code == 200
    assert calls[0] == calls[1]


def test_search_without_opening_does_not_send_article_content(setup):
    client, calls, _ = setup
    start(client, "one")
    search(client, "query only", "one")
    stop(client, "one")
    assert summarize(client).status_code == 200
    evidence = json.loads(calls[0]["messages"][1]["content"])
    assert evidence["articles"] == []
    assert evidence["events"][0]["total"] == 1

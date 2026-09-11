import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from app.models import KnowledgeDocumentResult, KnowledgeSearchResponse
from app.routers import knowledge
from test_knowledge_poc_app import make_client


def sample_search(body, app):
    return KnowledgeSearchResponse(query=body.query, query_time_ms=1, total=1, results=[
        KnowledgeDocumentResult(knowledge_id=1, source_file="sample.pdf", source_row_number=1,
                                title_en=body.query, abstract="Abstract: " + body.query,
                                abstract_snippet="Abstract", formulation="Original formula")
    ])


def start(client, name):
    response = client.post("/api/v1/knowledge/recordings", json={"recording_id": name})
    assert response.status_code == 200
    return response.json()


def search(client, query, recording_id=None):
    payload = {"query": query}
    if recording_id:
        payload["recording_id"] = recording_id
    return client.post("/api/v1/knowledge/search", json=payload)


def stop(client, name):
    return client.post(f"/api/v1/knowledge/recordings/{name}/stop")


def test_recording_keeps_two_searches_and_original_articles_then_freezes(monkeypatch):
    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)
    with make_client() as client:
        search(client, "before")
        start(client, "one")
        first = search(client, "first", "one").json()
        payload = {"search_id": first["search_id"], "knowledge_id": 1,
                   "source": "result_card", "recording_id": "one"}
        assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 200
        second = search(client, "second", "one").json()
        payload.update(search_id=second["search_id"], source="reaction_tab")
        assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 200
        assert start(client, "one")["recording_id"] == "one"  # retry does not reset
        result = stop(client, "one").json()
        assert result["status"] == "stopped"
        assert [event["event"] for event in result["events"]] == [
            "search.completed", "article.opened", "search.completed", "article.reaction_viewed"]
        assert result["events"][1]["article"]["abstract"] == "Abstract: first"
        assert result["events"][3]["article"]["formulation"] == "Original formula"
        assert [event["sequence"] for event in result["events"]] == [1, 2, 3, 4]
        assert search(client, "after", "one").status_code == 409
        assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 409
        search(client, "unrecorded after")
        assert stop(client, "one").json() == result  # retry returns frozen content


def test_recordings_do_not_mix_and_empty_recording_is_valid(monkeypatch):
    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)
    with make_client() as client:
        start(client, "one")
        start(client, "two")
        result = search(client, "first", "one").json()
        payload = {"search_id": result["search_id"], "knowledge_id": 1,
                   "source": "reaction_tab", "recording_id": "two"}
        assert client.post("/api/v1/knowledge/observations", json=payload).status_code == 409
        assert stop(client, "two").json()["events"] == []
        assert len(stop(client, "one").json()["events"]) == 1
        assert search(client, "missing", "unknown").status_code == 410
        assert stop(client, "unknown").status_code == 410


def test_recorded_article_survives_unrecorded_search_cache_eviction(monkeypatch):
    from app.knowledge_poc import MAX_RECENT_SEARCHES
    monkeypatch.setattr(knowledge, "_search_knowledge_sync", sample_search)
    with make_client() as client:
        start(client, "one")
        first = search(client, "first", "one").json()
        for _ in range(MAX_RECENT_SEARCHES):
            search(client, "later")
        response = client.post("/api/v1/knowledge/observations", json={
            "search_id": first["search_id"], "knowledge_id": 1, "source": "result_card", "recording_id": "one"})
        assert response.status_code == 200
        assert stop(client, "one").json()["events"][1]["article"]["abstract"] == "Abstract: first"


def test_stop_waits_for_inflight_search_and_failed_search_is_not_success(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def slow_search(body, app):
        entered.set()
        assert release.wait(5)
        raise HTTPException(503, "test unavailable")

    monkeypatch.setattr(knowledge, "_search_knowledge_sync", slow_search)
    with make_client() as client, ThreadPoolExecutor() as pool:
        start(client, "one")
        task = pool.submit(search, client, "failure", "one")
        try:
            assert entered.wait(5)
            assert stop(client, "one").status_code == 409
        finally:
            release.set()
        assert task.result().status_code == 503
        events = stop(client, "one").json()["events"]
        assert len(events) == 1
        assert events[0]["event"] == "search.failed"
        assert events[0]["status_code"] == 503

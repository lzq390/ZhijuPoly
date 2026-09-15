import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.knowledge_poc import create_app
from app.services import browsing_recording
from app.services.browsing_recording import BrowsingRecordingStore, Recording
from app.services.knowledge_poc_summary import stream_knowledge_summary


SETTINGS = dict(assistant_api_key="test-secret", assistant_base_url="https://model.example/v1",
                assistant_model="test-model", ai_proxy_url="", online_knowledge_proxy_url="")
EVENTS = [{"sequence": 1, "event": "search.completed", "query": "polyimide", "total": 1}]


def provider_stream(parts=("聚合物", "总结"), *, finish="stop", done=True):
    frames = ["data: " + json.dumps({"choices": [{"delta": {"content": part}, "finish_reason": None}]}, ensure_ascii=False)
              + "\r\n\r\n" for part in parts]
    frames.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}) + "\r\n\r\n")
    if done:
        frames.append("data: [DONE]\r\n\r\n")
    return httpx.Response(200, content="".join(frames).encode(), headers={"Content-Type": "text/event-stream"})


def mock_provider(monkeypatch, responses):
    calls = []
    original = httpx.AsyncClient

    def respond(request):
        calls.append(json.loads(request.content))
        return responses.pop(0)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    return calls


def test_upstream_deltas_are_streamed_and_credentials_redacted_across_boundaries(monkeypatch):
    calls = mock_provider(monkeypatch, [provider_stream(("内容 test-", "sec", "ret 结束"))])

    async def collect():
        return [part async for part in stream_knowledge_summary(EVENTS, Settings(**SETTINGS))]

    parts = asyncio.run(collect())
    assert len(parts) > 1
    assert "".join(parts) == "内容 [已隐藏] 结束"
    assert all("test-secret" not in part for part in parts)
    assert calls[0]["stream"] is True
    assert calls[0]["model"] == "test-model"
    assert calls[0]["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("response", [provider_stream(done=False), provider_stream(finish="length"),
                                    provider_stream(finish=None), provider_stream(parts=("",)),
                                    httpx.Response(200, content="data: invalid\n\n")])
def test_incomplete_or_malformed_stream_never_succeeds(monkeypatch, response):
    mock_provider(monkeypatch, [response])

    async def collect():
        return [part async for part in stream_knowledge_summary(EVENTS, Settings(**SETTINGS))]

    with pytest.raises(HTTPException) as error:
        asyncio.run(collect())
    assert error.value.status_code == 502


def test_terminal_usage_frame_with_no_choices_is_accepted(monkeypatch):
    body = provider_stream(done=False).text + 'data: {"choices": [], "usage": {"completion_tokens": 4}}\n\n'
    body += "data: [DONE]\n\n"
    mock_provider(monkeypatch, [httpx.Response(200, content=body)])

    async def collect():
        return "".join([part async for part in stream_knowledge_summary(EVENTS, Settings(**SETTINGS))])

    assert asyncio.run(collect()) == "聚合物总结"


def test_sse_route_errors_retries_and_json_share_only_completed_cache(monkeypatch):
    calls = mock_provider(monkeypatch, [provider_stream(done=False), provider_stream()])
    app = create_app(Settings(**SETTINGS))
    store = app.state.browsing_recording
    store.recordings["one"] = Recording(ended_at="stopped", events=EVENTS)
    headers = {"Accept": "text/event-stream"}
    with TestClient(app) as client:
        assert client.post("/api/v1/knowledge/recordings/missing/summary", headers=headers).status_code == 410
        first = client.post("/api/v1/knowledge/recordings/one/summary", headers=headers)
        assert first.headers["content-type"].startswith("text/event-stream")
        assert first.headers["x-accel-buffering"] == "no"
        assert "event: delta" in first.text and "event: error" in first.text and "event: done" not in first.text
        assert store.recordings["one"].summary is None
        retried = client.post("/api/v1/knowledge/recordings/one/summary", headers=headers)
        assert "event: done" in retried.text
        saved = client.post("/api/v1/knowledge/recordings/one/summary").json()
        assert saved == {"recording_id": "one", "summary": "聚合物总结", "generated": True}
        cached = client.post("/api/v1/knowledge/recordings/one/summary", headers=headers)
        assert "event: done" in cached.text and "event: delta" not in cached.text
        assert len(calls) == 2
        client.post("/api/v1/knowledge/recordings", json={"recording_id": "empty"})
        assert client.post("/api/v1/knowledge/recordings/empty/summary", headers=headers).status_code == 409
        client.post("/api/v1/knowledge/recordings/empty/stop")
        empty = client.post("/api/v1/knowledge/recordings/empty/summary", headers=headers)
        assert '"generated": false' in empty.text and len(calls) == 2


def test_disconnect_releases_lock_without_caching_partial_and_json_waits_for_stream(monkeypatch):
    async def exercise():
        release = asyncio.Event()
        calls = []

        async def stream(events, settings):
            calls.append(events)
            yield "第一段。"
            await release.wait()
            yield "第二段。"

        monkeypatch.setattr(browsing_recording, "stream_knowledge_summary", stream)
        store = BrowsingRecordingStore(Settings(**SETTINGS))
        record = Recording(ended_at="stopped", events=EVENTS)
        store.recordings["one"] = record
        disconnected = store.stream_summary("one")
        assert (await anext(disconnected))[0] == "status"
        assert (await anext(disconnected))[0] == "delta"
        await disconnected.aclose()
        assert not record.summary_lock.locked() and record.summary is None
        retried = store.stream_summary("one")
        await anext(retried)
        await anext(retried)
        waiting = asyncio.create_task(store.summarize_recording("one"))
        await asyncio.sleep(0)
        assert not waiting.done()
        release.set()
        frames = [event async for event in retried]
        assert frames[-1][0] == "done"
        assert (await waiting)["summary"] == "第一段。第二段。"
        assert len(calls) == 2

    asyncio.run(exercise())

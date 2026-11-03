"""Explicit model proxy and transport wiring, with all inference/network mocked."""
import asyncio
import socket
from types import SimpleNamespace as NS

import pytest

from app import config
from app.config import Settings
from app.auth.context import Identity, user_context
from app.models import AssistantChatStreamRequest, OnlineKnowledgeSearchRequest, TgAssistantChatStreamRequest
from app.routers import assistant, online_knowledge as online
from app.services import ai_client
from app.services.online_knowledge import search_service


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEFAULT_ENV_FILE", tmp_path / "absent.env")
    for key in ("AI_PROXY_URL", "AI_MODEL_PROXY_URL", "ONLINE_KNOWLEDGE_PROXY_URL", "ASSISTANT_TRANSPORT", "ONLINE_KNOWLEDGE_TRANSPORT"):
        monkeypatch.delenv(key, raising=False)
    def deny(*_, **__):
        raise AssertionError("No network permitted")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr("app.task_control._start_checker", lambda _: True)
    with user_context(Identity("11111111-1111-1111-1111-111111111111")):
        yield


def test_model_only_override_preserves_legacy_discovery_and_defaults():
    settings = Settings(ai_proxy_url="http://discovery.invalid:8888", ai_model_proxy_url="http://provider.invalid:9999")
    assert settings.ai_model_proxy_url == "http://provider.invalid:9999"
    assert settings.ai_proxy_url == settings.online_knowledge_proxy_url == "http://discovery.invalid:8888"
    assert settings.assistant_transport == settings.online_knowledge_transport == "chat_completions"


def test_unset_model_proxy_inherits_legacy_but_explicit_empty_means_direct(monkeypatch):
    assert Settings(ai_proxy_url="http://legacy.invalid:8888").ai_model_proxy_url == "http://legacy.invalid:8888"
    monkeypatch.setenv("AI_MODEL_PROXY_URL", "")
    assert Settings(ai_proxy_url="http://legacy.invalid:8888").ai_model_proxy_url == ""


def test_model_only_env_leaves_discovery_direct(monkeypatch):
    monkeypatch.setenv("AI_MODEL_PROXY_URL", "http://provider.invalid:8888")
    monkeypatch.setenv("ASSISTANT_TRANSPORT", "responses")
    monkeypatch.setenv("ONLINE_KNOWLEDGE_TRANSPORT", "responses")
    settings = Settings()
    assert settings.ai_model_proxy_url == "http://provider.invalid:8888"
    assert settings.online_knowledge_proxy_url == settings.ai_proxy_url == ""
    assert settings.assistant_transport == settings.online_knowledge_transport == "responses"


@pytest.mark.parametrize("value", ["socks5://invalid:5", "http://user:secret@invalid:9", "http://invalid:9/path", "http://invalid:9?secret=1"])
def test_model_proxy_rejects_credentials_and_non_proxy_urls(value):
    with pytest.raises(ValueError, match="AI_MODEL_PROXY_URL"):
        Settings(ai_model_proxy_url=value)


@pytest.mark.parametrize("field", ["assistant_transport", "online_knowledge_transport"])
def test_invalid_transport_is_rejected(field):
    with pytest.raises(ValueError, match=field.upper()):
        Settings(**{field: "auto-unknown"})


@pytest.mark.parametrize("transport,cap", [("responses", 2400), ("chat_completions", 1400)])
def test_entitlement_output_budget_matches_selected_transport(monkeypatch, transport, cap):
    from app.services import assistant_entitlements as adapter
    monkeypatch.setattr(adapter.core, "enabled", lambda: True)
    captured = []
    monkeypatch.setattr(adapter, "prepare_operation", lambda *args: captured.append(args))
    request = NS(app=NS(state=NS(settings=NS(assistant_transport=transport))))
    adapter.prepare_chat(request, AssistantChatStreamRequest(messages=[{"role": "user", "content": "hi"}]))
    assert captured[0][3]["max_output_tokens"] == cap


def test_shared_model_client_uses_explicit_proxy_and_never_ambient(monkeypatch):
    seen = []
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:1")
    monkeypatch.setattr(ai_client, "DefaultHttpxClient", lambda **kw: seen.append(kw))
    monkeypatch.setattr(ai_client, "OpenAI", lambda **kw: kw)
    settings = Settings(ai_model_proxy_url="http://provider.invalid:2")
    client = ai_client.create_openai_client(api_key="fake", base_url="https://unused.invalid/v1", proxy_url=ai_client.model_proxy_url(settings))
    assert seen == [{"trust_env": False, "proxy": "http://provider.invalid:2"}]
    assert client["max_retries"] == 0


def test_online_sync_and_job_access_freezes_two_separate_channels(monkeypatch):
    settings = Settings(online_knowledge_api_key="fake", ai_proxy_url="http://discovery.invalid:1", ai_model_proxy_url="http://provider.invalid:2", online_knowledge_transport="responses")
    body = OnlineKnowledgeSearchRequest(material="polymer", max_papers=1)
    access = online.resolve_online_model_access(body, settings)
    assert access.proxy_url == "http://discovery.invalid:1"
    assert access.model_proxy_url == "http://provider.invalid:2"
    assert access.transport == "responses"
    captured = []
    monkeypatch.setattr(online, "admission", lambda _: __import__("contextlib").nullcontext())
    monkeypatch.setattr(online, "run_online_knowledge_search", lambda **kw: captured.append(kw))
    online._run_search_from_request(body, access)
    assert captured[0]["proxy_url"] == access.proxy_url
    assert captured[0]["model_proxy_url"] == access.model_proxy_url
    assert captured[0]["transport"] == "responses"


def test_literature_sources_never_receive_model_proxy_or_model_key(monkeypatch):
    captured = {}
    class Searcher:
        DEFAULT_SOURCES = ["fixture"]
        def __init__(self, **kw): captured["source"] = kw
        def search_all(self, *_, **__): return [{"title": "T", "abstract": "A"}]
        def deduplicate(self, rows): return rows
        def enrich_batch_with_crossref(self, rows): return rows
    class Extractor:
        def __init__(self, **kw): captured["model"] = kw
        def process_papers(self, *_, **__): return [{}]
        def convert_to_rows(self, **_): return []
        def close(self): captured["closed"] = True
    monkeypatch.setattr(search_service, "SimpleLiteratureSearcher", Searcher)
    monkeypatch.setattr(search_service, "PolymerExtractor", Extractor)
    result = search_service.run_online_knowledge_search(material="polymer", mode="synthesis", api_key="model-only-key", base_url="https://provider.invalid/v1", model="m", max_papers=1, extraction_delay_seconds=0, proxy_url="", model_proxy_url="http://provider-only.invalid:9", transport="responses")
    assert captured["source"] == {"max_workers": 4, "proxy_url": ""}
    assert captured["model"]["proxy_url"] == "http://provider-only.invalid:9"
    assert captured["model"]["transport"] == "responses"
    assert "model-only-key" not in repr(captured["source"])
    assert result["exampleUsed"] is False and captured["closed"]


@pytest.mark.parametrize("kind", ["general", "tg", "image"])
def test_every_assistant_route_passes_model_only_proxy(monkeypatch, kind):
    settings = Settings(assistant_api_key="fake", assistant_base_url="https://unused.invalid", assistant_transport="responses", tg_assistant_enabled=True, tg_assistant_api_key="fake", tg_assistant_base_url="https://unused.invalid", ai_proxy_url="http://discovery.invalid:1", ai_model_proxy_url="http://provider.invalid:2")
    request = NS(app=NS(state=NS(settings=settings)))
    captured = []
    monkeypatch.setattr(assistant, "prepare_chat", lambda *_, **__: None)
    monkeypatch.setattr(assistant, "wrap_chat", lambda source, *_, **__: source)
    monkeypatch.setattr(assistant, "PrivateStreamingResponse", lambda source, **_: source)
    def generate(**kw):
        captured.append(kw)
        return iter([NS(event="done", payload={"message": "OK"})])
    body = {"messages": [{"role": "user", "content": "hello"}]}
    if kind == "general":
        monkeypatch.setattr(assistant, "stream_assistant_events", generate)
        list(assistant.stream_chat(AssistantChatStreamRequest(**body), request))
    elif kind == "tg":
        monkeypatch.setattr(assistant, "stream_tg_assistant_events", generate)
        list(assistant.stream_tg_chat(TgAssistantChatStreamRequest(**body), request))
    else:
        async def read(*_): return b"png"
        async def close(): pass
        monkeypatch.setattr(assistant, "validate_structure_image", lambda *_, **__: ("image/png", ".png"))
        monkeypatch.setattr(assistant, "stream_assistant_image_chat", lambda **kw: (captured.append(kw) or iter(["OK"])))
        values = asyncio.run(assistant.stream_image_chat(request, __import__("json").dumps(body), NS(read=read, close=close, content_type="image/png")))
        list(values() if callable(values) else values)
    assert captured[0]["proxy_url"] == "http://provider.invalid:2"
    if kind != "tg":
        assert captured[0]["transport"] == "responses"

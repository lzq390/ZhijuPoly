from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AssistantChatMessage, AssistantModuleContext
from app.routers import assistant as assistant_router
from app.services import assistant_chat as assistant_chat_service
from app.services.assistant_chat import build_assistant_system_prompt


def make_assistant_app(tmp_path: Path, *, api_key: str = "test-key"):
    return create_app(
        Settings(
            sqlite_db_path=str(tmp_path / "assistant.db"),
            csv_source_path=str(tmp_path / "sample.csv"),
            allowed_origins="http://localhost:5173",
            model_enabled=False,
            online_knowledge_api_key="",
            assistant_api_key=api_key,
            assistant_base_url="https://example.test/v1",
            assistant_model="test-model",
        )
    )


def test_assistant_settings_reuses_online_knowledge_config_for_blank_assistant_values(tmp_path: Path, monkeypatch) -> None:
    for key in ("ASSISTANT_API_KEY", "ASSISTANT_BASE_URL", "ASSISTANT_MODEL"):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(
        sqlite_db_path=str(tmp_path / "assistant.db"),
        csv_source_path=str(tmp_path / "sample.csv"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
        online_knowledge_api_key="knowledge-key",
        online_knowledge_base_url="https://knowledge.example/v1",
        online_knowledge_model="knowledge-model",
        assistant_api_key="",
        assistant_base_url="",
        assistant_model="",
    )

    assert settings.assistant_api_key == "knowledge-key"
    assert settings.assistant_base_url == "https://knowledge.example/v1"
    assert settings.assistant_model == "gpt-5.5"

def test_assistant_prompt_uses_clickable_module_markers_without_routes() -> None:
    prompt = build_assistant_system_prompt(
        [
            AssistantModuleContext(
                id="labData",
                title="实验数据采集",
                route="/lab-data/collect",
                group="数据与知识",
                description="录入实验样品、测试项目和测量结果。",
            )
        ],
        "home",
    )

    assert "/lab-data/collect" not in prompt
    assert "[[module:labData|实验数据采集]]" in prompt
    assert "Never expose internal route paths" in prompt

def test_assistant_chat_streams_token_and_done_events(tmp_path: Path, monkeypatch) -> None:
    def fake_stream_assistant_chat(
        *,
        messages: list[AssistantChatMessage],
        modules: list[AssistantModuleContext],
        active_module: str | None,
        api_key: str,
        base_url: str,
        model: str,
    ) -> Iterable[str]:
        assert messages[-1].content == "hello"
        assert modules[0].title == "Knowledge Search"
        assert active_module == "home"
        assert api_key == "test-key"
        assert base_url == "https://example.test/v1"
        assert model == "test-model"
        yield "Hi"
        yield " there"

    monkeypatch.setattr(assistant_router, "stream_assistant_chat", fake_stream_assistant_chat)
    client = TestClient(make_assistant_app(tmp_path))

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "context": {
                "active_module": "home",
                "modules": [
                    {
                        "id": "knowledge",
                        "title": "Knowledge Search",
                        "route": "/knowledge",
                        "group": "Data & Knowledge",
                        "description": "Search polymer literature.",
                    }
                ],
            },
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: token\ndata: {\"content\": \"Hi\"}" in response.text
    assert "event: token\ndata: {\"content\": \" there\"}" in response.text
    assert "event: done\ndata: {\"message\": \"Hi there\"}" in response.text


def test_assistant_chat_stream_emits_config_error_event(tmp_path: Path) -> None:
    client = TestClient(make_assistant_app(tmp_path, api_key=""))

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"messages": [{"role": "user", "content": "hello"}], "context": {"modules": []}},
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Assistant API Key is required" in response.text


def test_assistant_chat_requires_latest_user_message(tmp_path: Path) -> None:
    client = TestClient(make_assistant_app(tmp_path))

    response = client.post(
        "/api/v1/assistant/chat/stream",
        json={"messages": [{"role": "assistant", "content": "hello"}], "context": {"modules": []}},
    )

    assert response.status_code == 422
    assert "latest assistant chat message must be from the user" in response.text

def test_assistant_chat_ignores_stream_chunks_without_delta_content(monkeypatch) -> None:
    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["model"] == "test-model"
            return iter(
                [
                    SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="OK"))]),
                    SimpleNamespace(choices=[SimpleNamespace(delta=None)]),
                    SimpleNamespace(choices=[]),
                ]
            )

    class FakeOpenAI:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            assert api_key == "test-key"
            assert base_url == "https://example.test/v1"
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(assistant_chat_service, "OpenAI", FakeOpenAI)

    tokens = list(
        assistant_chat_service.stream_assistant_chat(
            messages=[AssistantChatMessage(role="user", content="hello")],
            modules=[],
            active_module=None,
            api_key="test-key",
            base_url="https://example.test/v1",
            model="test-model",
        )
    )

    assert tokens == ["OK"]

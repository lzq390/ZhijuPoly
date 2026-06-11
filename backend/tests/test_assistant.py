from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AssistantChatMessage, AssistantModuleContext
from app.pi_database import ensure_pi_schema
from app.routers import assistant as assistant_router
from app.routers import query as query_router
from app.services import image_recognition
from app.services import assistant_chat as assistant_chat_service
from app.services import assistant_orchestrator
from app.services.assistant_chat import build_assistant_system_prompt
from app.services.assistant_orchestrator import AssistantStreamEvent
from app.services.assistant_skills import predict_properties
from app.services.image_recognition import RecognizedStructure


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00"


def make_assistant_app(
    tmp_path: Path,
    *,
    api_key: str = "test-key",
    model_enabled: bool = False,
    pi_reverse_db_path: Path | None = None,
):
    return create_app(
        Settings(
            sqlite_db_path=str(tmp_path / "assistant.db"),
            csv_source_path=str(tmp_path / "sample.csv"),
            pi_reverse_db_path=str(pi_reverse_db_path or (tmp_path / "pi_reverse.db")),
            allowed_origins="http://localhost:5173",
            model_enabled=model_enabled,
            online_knowledge_api_key="",
            assistant_api_key=api_key,
            assistant_base_url="https://example.test/v1",
            assistant_model="test-model",
        )
    )


def make_ocsr_app(
    tmp_path: Path,
    *,
    ocsr_enabled: bool = True,
    ocsr_model_dir: Path | None = None,
    ocsr_max_image_bytes: int = 1024,
):
    return create_app(
        Settings(
            sqlite_db_path=str(tmp_path / "assistant.db"),
            csv_source_path=str(tmp_path / "sample.csv"),
            allowed_origins="http://localhost:5173",
            model_enabled=False,
            online_knowledge_api_key="",
            assistant_api_key="test-key",
            assistant_base_url="https://example.test/v1",
            assistant_model="test-model",
            ocsr_enabled=ocsr_enabled,
            ocsr_model_dir=str(ocsr_model_dir or (tmp_path / "missing-ocsr")),
            ocsr_max_image_bytes=ocsr_max_image_bytes,
        )
    )


async def async_post_structure_image(app, image_bytes: bytes, *, content_type: str = "image/png"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/api/v1/structure/recognize-image",
            files={"image": ("structure.png", image_bytes, content_type)},
        )


def post_structure_image(app, image_bytes: bytes, *, content_type: str = "image/png"):
    return asyncio.run(async_post_structure_image(app, image_bytes, content_type=content_type))


def write_iupac_cache(db_path: Path, entries: list[tuple[str, str]]) -> None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        ensure_pi_schema(connection)
        connection.executemany(
            "INSERT INTO smiles_iupac_cache (smiles, iupac_name) VALUES (?, ?)",
            entries,
        )
        connection.commit()
    finally:
        connection.close()


def post_assistant_message(client: TestClient, content: str = "hello"):
    return client.post(
        "/api/v1/assistant/chat/stream",
        json={
            "messages": [{"role": "user", "content": content}],
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


def test_structure_image_recognition_returns_molscribe_result(tmp_path: Path, monkeypatch) -> None:
    def fake_recognize_structure_image_from_bytes(
        image_bytes: bytes,
        *,
        content_type: str | None,
        model_path: Path,
        device: str,
        max_bytes: int,
    ) -> RecognizedStructure:
        assert image_bytes == PNG_BYTES
        assert content_type == "image/png"
        assert model_path == tmp_path / "missing-ocsr"
        assert device == "auto"
        assert max_bytes == 1024
        return RecognizedStructure(
            smiles="CCO",
            molfile="mol block",
            confidence=0.87,
            warnings=["low contrast"],
        )

    monkeypatch.setattr(query_router, "recognize_structure_image_from_bytes", fake_recognize_structure_image_from_bytes)
    response = post_structure_image(make_ocsr_app(tmp_path), PNG_BYTES)

    assert response.status_code == 200
    assert response.json()["smiles"] == "CCO"
    assert response.json()["molfile"] == "mol block"
    assert response.json()["confidence"] == 0.87
    assert response.json()["warnings"] == ["low contrast"]
    assert response.json()["query_time_ms"] >= 0


def test_structure_image_recognition_rejects_disabled_service(tmp_path: Path) -> None:
    response = post_structure_image(make_ocsr_app(tmp_path, ocsr_enabled=False), PNG_BYTES)

    assert response.status_code == 503
    assert response.json()["detail"] == "image recognition service is disabled"


def test_structure_image_recognition_rejects_unsupported_image_type(tmp_path: Path) -> None:
    response = post_structure_image(
        make_ocsr_app(tmp_path),
        b"not an image",
        content_type="text/plain",
    )

    assert response.status_code == 415
    assert response.json()["detail"] == "unsupported image type"


def test_structure_image_recognition_rejects_large_image(tmp_path: Path) -> None:
    response = post_structure_image(
        make_ocsr_app(tmp_path, ocsr_max_image_bytes=8),
        PNG_BYTES,
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "image file is too large"


def test_structure_image_recognition_reports_missing_model(tmp_path: Path) -> None:
    response = post_structure_image(make_ocsr_app(tmp_path), PNG_BYTES)

    assert response.status_code == 503
    assert "OCSR model path not found" in response.json()["detail"]


def test_ocsr_auto_device_rejects_cuda_without_conv_engine() -> None:
    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def synchronize() -> None:
            return None

    class FakeFunctional:
        @staticmethod
        def conv2d(*_args, **_kwargs):
            raise RuntimeError("GET was unable to find an engine to execute this computation")

    fake_torch = SimpleNamespace(
        cuda=FakeCuda(),
        nn=SimpleNamespace(functional=FakeFunctional()),
        empty=lambda *_args, **_kwargs: object(),
        ones=lambda *_args, **_kwargs: object(),
    )

    assert image_recognition._cuda_is_usable(fake_torch) is False


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
    assert "registered skill result" in prompt


def test_assistant_chat_streams_token_and_done_events(tmp_path: Path, monkeypatch) -> None:
    def fake_stream_assistant_events(**kwargs) -> Iterable[AssistantStreamEvent]:
        assert kwargs["messages"][-1].content == "hello"
        assert kwargs["modules"][0].title == "Knowledge Search"
        assert kwargs["active_module"] == "home"
        assert kwargs["api_key"] == "test-key"
        assert kwargs["base_url"] == "https://example.test/v1"
        assert kwargs["model"] == "test-model"
        yield AssistantStreamEvent("token", {"content": "Hi"})
        yield AssistantStreamEvent("token", {"content": " there"})
        yield AssistantStreamEvent("done", {"message": "Hi there"})

    monkeypatch.setattr(assistant_router, "stream_assistant_events", fake_stream_assistant_events)
    client = TestClient(make_assistant_app(tmp_path))

    response = post_assistant_message(client)

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


def test_assistant_plain_chat_does_not_trigger_skill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {"type": "chat", "message": "普通问答。"},
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_chat",
        lambda **kwargs: iter(["普通", "回答"]),
    )
    client = TestClient(make_assistant_app(tmp_path))

    response = post_assistant_message(client, "怎么分析聚合物数据？")

    assert response.status_code == 200
    assert "skill_start" not in response.text
    assert "event: done" in response.text
    assert "普通回答" in response.text


def test_assistant_resolves_cached_iupac_before_intent_routing(tmp_path: Path, monkeypatch) -> None:
    iupac_name = "2-(2,4-diamino-6-methyl-phenyl)acrylonitrile"
    resolved_smiles = "C=C(C#N)c1c(C)cc(N)cc1N"
    pi_db_path = tmp_path / "pi_reverse.db"
    write_iupac_cache(pi_db_path, [(resolved_smiles, iupac_name)])

    def fake_complete_assistant_intent(**kwargs):
        latest_content = kwargs["messages"][-1].content
        assert iupac_name in latest_content
        assert f"Original IUPAC name: {iupac_name}" in latest_content
        assert f"Resolved SMILES: {resolved_smiles}" in latest_content
        assert "Resolved SMILES source: smiles_iupac_cache" in latest_content
        return {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": resolved_smiles, "all_properties": False, "properties": ["Tg"]},
        }

    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        fake_complete_assistant_intent,
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("prediction summary should be deterministic")),
    )

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert smiles == resolved_smiles
        assert properties == ["Glass transition temperature"]
        return {"Glass transition temperature": 42.0}

    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(
        make_assistant_app(tmp_path, model_enabled=True, pi_reverse_db_path=pi_db_path)
    )

    response = post_assistant_message(client, f"预测 {iupac_name} 的 Tg")

    assert response.status_code == 200
    assert "event: skill_result" in response.text
    assert resolved_smiles in response.text
    assert "iupac_name" not in response.text
    assert f"对 **{iupac_name}** 的解析 SMILES" in response.text
    assert "预测结果如下" in response.text


def test_assistant_uses_image_resolved_smiles_before_intent_routing(tmp_path: Path, monkeypatch) -> None:
    resolved_smiles = "CCO"
    user_message = (
        "预测这张图的 Tg\n\n"
        "[Resolved structure input]\n"
        "Original image file: phenyl-sample.png\n"
        f"Resolved SMILES: {resolved_smiles}\n"
        "Resolved SMILES source: molscribe_image_recognition\n"
        "Recognition confidence: 0.8700\n"
        "Use the resolved SMILES as the structure input for any downstream task."
    )

    def fake_complete_assistant_intent(**kwargs):
        latest_content = kwargs["messages"][-1].content
        assert "Original image file: phenyl-sample.png" in latest_content
        assert f"Resolved SMILES: {resolved_smiles}" in latest_content
        assert "Resolved SMILES source: molscribe_image_recognition" in latest_content
        return {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": resolved_smiles, "all_properties": False, "properties": ["Tg"]},
        }

    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        fake_complete_assistant_intent,
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("prediction summary should be deterministic")),
    )

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert smiles == resolved_smiles
        assert properties == ["Glass transition temperature"]
        return {"Glass transition temperature": 42.0}

    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, user_message)

    assert response.status_code == 200
    assert "event: skill_result" in response.text
    assert resolved_smiles in response.text
    assert "当前 IUPAC 缓存中没有找到该名称" not in response.text
    assert "对图片 **phenyl-sample.png** 识别得到的 SMILES" in response.text
    assert "预测结果如下" in response.text


def test_assistant_unknown_iupac_clarifies_before_model_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("model router should not run")),
    )
    monkeypatch.setattr(
        predict_properties,
        "predict",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("predict should not run")),
    )
    client = TestClient(make_assistant_app(tmp_path, api_key="", model_enabled=True))

    response = post_assistant_message(
        client,
        "预测 2-(2,4-diamino-6-methyl-phenyl)unknown 的 Tg",
    )

    assert response.status_code == 200
    assert "event: error" not in response.text
    assert "skill_start" not in response.text
    assert "当前 IUPAC 缓存中没有找到该名称" in response.text


def test_assistant_multiple_cached_iupac_names_clarifies_before_model_call(tmp_path: Path, monkeypatch) -> None:
    first_iupac = "2-(2,4-diamino-6-methyl-phenyl)acrylonitrile"
    second_iupac = "2-(2,4-diamino-6-methyl-pyrimidin-5-yl)acrylonitrile"
    pi_db_path = tmp_path / "pi_reverse.db"
    write_iupac_cache(
        pi_db_path,
        [
            ("C=C(C#N)c1c(C)cc(N)cc1N", first_iupac),
            ("C=C(C#N)c1c(C)nc(N)nc1N", second_iupac),
        ],
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("model router should not run")),
    )
    client = TestClient(
        make_assistant_app(tmp_path, api_key="", model_enabled=True, pi_reverse_db_path=pi_db_path)
    )

    response = post_assistant_message(client, f"预测 {first_iupac} 和 {second_iupac} 的 Tg")

    assert response.status_code == 200
    assert "event: error" not in response.text
    assert "skill_start" not in response.text
    assert "识别到多个 IUPAC 名称" in response.text
    assert first_iupac in response.text
    assert second_iupac in response.text


def test_assistant_prediction_skill_streams_skill_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": True},
        },
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("prediction summary should be deterministic")),
    )

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert smiles == "CCO"
        assert len(properties) == 9
        return {property_name: float(index + 1) for index, property_name in enumerate(properties)}

    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的 9 个性质")

    assert response.status_code == 200
    skill_start_index = response.text.index("event: skill_start")
    skill_result_index = response.text.index("event: skill_result")
    token_index = response.text.index("event: token")
    done_index = response.text.index("event: done")
    assert skill_start_index < skill_result_index < token_index < done_index
    assert "Glass transition temperature" in response.text
    assert "O2 Permeability Barrer" in response.text
    assert "已解析 SMILES" in response.text
    assert "`CCO`" in response.text
    assert "预测结果如下" in response.text


def test_assistant_prediction_skill_maps_tg_subset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": False, "properties": ["Tg"]},
        },
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: iter(["Tg 预测完成"]),
    )
    monkeypatch.setattr(
        assistant_chat_service,
        "complete_prediction_property_resolution",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Tg should resolve deterministically")),
    )

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert properties == ["Glass transition temperature"]
        return {"Glass transition temperature": 42.0}

    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的 Tg")

    assert response.status_code == 200
    assert "Glass transition temperature" in response.text
    assert "42.0" in response.text
    assert "Melting temperature" not in response.text


def test_assistant_prediction_skill_prefers_properties_when_all_properties_omitted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "properties": ["Tg"]},
        },
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: iter(["Tg 预测完成"]),
    )
    monkeypatch.setattr(
        assistant_chat_service,
        "complete_prediction_property_resolution",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Tg should resolve deterministically")),
    )

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert properties == ["Glass transition temperature"]
        return {"Glass transition temperature": 42.0}

    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的 Tg")

    assert response.status_code == 200
    assert "Glass transition temperature" in response.text
    assert "42.0" in response.text
    assert "Melting temperature" not in response.text


def test_assistant_prediction_skill_maps_thermal_property_group(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": False, "properties": ["热学性质"]},
        },
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: iter(["热学性质预测完成"]),
    )
    monkeypatch.setattr(
        assistant_chat_service,
        "complete_prediction_property_resolution",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("thermal group should resolve deterministically")),
    )

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert properties == [
            "Glass transition temperature",
            "Melting temperature",
            "Thermal decomposition temperature",
            "Thermal decomposition weight loss",
        ]
        return {property_name: float(index + 1) for index, property_name in enumerate(properties)}

    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的热学性质")

    assert response.status_code == 200
    assert "event: error" not in response.text
    assert "event: skill_result" in response.text
    assert "Glass transition temperature" in response.text
    assert "Thermal decomposition weight loss" in response.text
    assert "O2 Permeability Barrer" not in response.text


def test_assistant_prediction_skill_resolves_heat_stability_with_llm_catalog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": False, "properties": ["热稳定性"]},
        },
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: iter(["热稳定性预测完成"]),
    )

    def fake_resolver(**kwargs):
        assert kwargs["requested_properties"] == ["热稳定性"]
        assert any(item["name"] == "Thermal decomposition temperature" for item in kwargs["property_catalog"])
        return {
            "type": "resolved",
            "properties": ["Thermal decomposition temperature", "Thermal decomposition weight loss"],
        }

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert properties == ["Thermal decomposition temperature", "Thermal decomposition weight loss"]
        return {property_name: float(index + 1) for index, property_name in enumerate(properties)}

    monkeypatch.setattr(assistant_chat_service, "complete_prediction_property_resolution", fake_resolver)
    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的热稳定性")

    assert response.status_code == 200
    assert "event: skill_start" in response.text
    assert "event: skill_result" in response.text
    assert "Thermal decomposition temperature" in response.text
    assert "Glass transition temperature" not in response.text


def test_assistant_prediction_skill_resolves_gas_barrier_with_llm_catalog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": False, "properties": ["气体阻隔性"]},
        },
    )
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "stream_assistant_skill_summary",
        lambda **kwargs: iter(["气体阻隔性预测完成"]),
    )

    def fake_resolver(**kwargs):
        assert kwargs["requested_properties"] == ["气体阻隔性"]
        return {
            "type": "resolved",
            "properties": [
                "O2 Permeability Barrer",
                "Co2 Permeability Barrer",
                "H2 Permeability Barrer",
            ],
        }

    def fake_predict(smiles: str, properties: list[str], model_dir=None):
        assert properties == [
            "O2 Permeability Barrer",
            "Co2 Permeability Barrer",
            "H2 Permeability Barrer",
        ]
        return {property_name: float(index + 1) for index, property_name in enumerate(properties)}

    monkeypatch.setattr(assistant_chat_service, "complete_prediction_property_resolution", fake_resolver)
    monkeypatch.setattr(predict_properties, "predict", fake_predict)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的气体阻隔性")

    assert response.status_code == 200
    assert "event: skill_result" in response.text
    assert "O2 Permeability Barrer" in response.text
    assert "H2 Permeability Barrer" in response.text


def test_assistant_prediction_property_resolver_rejects_catalog_outside_result(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": False, "properties": ["耐磨性"]},
        },
    )
    monkeypatch.setattr(
        assistant_chat_service,
        "complete_prediction_property_resolution",
        lambda **kwargs: {"type": "resolved", "properties": ["Young's modulus"]},
    )
    monkeypatch.setattr(
        predict_properties,
        "predict",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("predict should not run")),
    )
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的耐磨性")

    assert response.status_code == 200
    assert "event: skill_start" not in response.text
    assert "event: skill_result" not in response.text
    assert "event: skill_error" in response.text
    assert "catalog 外" in response.text


def test_assistant_prediction_property_resolver_failure_emits_skill_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": False, "properties": ["耐热表现"]},
        },
    )

    def fail_resolver(**kwargs):
        raise assistant_chat_service.AssistantChatModelError("prediction property resolver response was not valid JSON")

    monkeypatch.setattr(assistant_chat_service, "complete_prediction_property_resolution", fail_resolver)
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的耐热表现")

    assert response.status_code == 200
    assert "event: skill_start" not in response.text
    assert "event: skill_result" not in response.text
    assert "event: skill_error" in response.text
    assert "event: error" not in response.text
    assert "预测性质语义解析失败" in response.text


def test_assistant_prediction_skill_unknown_property_emits_skill_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "CCO", "all_properties": False, "properties": ["杨氏模量"]},
        },
    )
    monkeypatch.setattr(
        assistant_chat_service,
        "complete_prediction_property_resolution",
        lambda **kwargs: {
            "type": "unsupported",
            "requested": ["杨氏模量"],
            "message": "当前预测接口暂不支持杨氏模量。",
        },
    )
    monkeypatch.setattr(
        predict_properties,
        "predict",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("predict should not run")),
    )
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 CCO 的杨氏模量")

    assert response.status_code == 200
    assert "event: skill_start" not in response.text
    assert "event: skill_result" not in response.text
    assert "event: skill_error" in response.text
    assert "event: error" not in response.text
    assert "当前预测接口暂不支持杨氏模量" in response.text


def test_assistant_prediction_capability_query_uses_current_property_metadata(tmp_path: Path, monkeypatch) -> None:
    def fail_intent_call(**kwargs):
        raise AssertionError("capability queries should not call the model router")

    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        fail_intent_call,
    )
    monkeypatch.setattr(
        predict_properties,
        "get_available_properties",
        lambda model_dir=None: list(predict_properties.ALL_PREDICTABLE_PROPERTIES),
    )
    client = TestClient(make_assistant_app(tmp_path, api_key="", model_enabled=True))

    response = post_assistant_message(client, "我能预测聚合物哪些性质？")

    assert response.status_code == 200
    assert "event: error" not in response.text
    assert "skill_start" not in response.text
    assert "当前预测接口可预测 9 个性质" in response.text
    assert "玻璃化转变温度" in response.text
    assert "O₂ 渗透性" in response.text
    assert "H₂ 渗透性" in response.text
    assert "杨氏模量" not in response.text


def test_assistant_prediction_skill_missing_smiles_clarifies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"all_properties": True},
        },
    )
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "帮我预测性质")

    assert response.status_code == 200
    assert "skill_start" not in response.text
    assert "请提供需要预测的 SMILES" in response.text


def test_assistant_prediction_skill_invalid_smiles_emits_skill_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "predict_polymer_properties",
            "arguments": {"smiles": "not-a-smiles", "all_properties": True},
        },
    )
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "预测 not-a-smiles 的 9 个性质")

    assert response.status_code == 200
    assert "event: skill_start" in response.text
    assert "event: skill_error" in response.text
    assert "invalid smiles" in response.text


def test_assistant_unknown_skill_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        assistant_orchestrator.assistant_chat,
        "complete_assistant_intent",
        lambda **kwargs: {
            "type": "skill_call",
            "skill_name": "unknown_skill",
            "arguments": {},
        },
    )
    client = TestClient(make_assistant_app(tmp_path, model_enabled=True))

    response = post_assistant_message(client, "执行未知技能")

    assert response.status_code == 200
    assert "event: skill_error" in response.text
    assert "未注册的助手技能" in response.text

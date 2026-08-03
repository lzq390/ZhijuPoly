from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import app.config as config_module
from app.config import Settings, _normalize_http_proxy_url
from app.models import OnlineKnowledgeSearchRequest
from app.routers import online_knowledge as online_routes
from app.services.online_knowledge import extractor as extractor_module
from app.services.online_knowledge import search_service


def _request_with_settings(settings: object) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    return Request({"type": "http", "app": app})


def _search_request() -> OnlineKnowledgeSearchRequest:
    return OnlineKnowledgeSearchRequest(
        material="polyethylene",
        base_url="https://api.example.test/v1",
        model="test-model",
        max_papers=1,
        extraction_delay_seconds=0,
        use_server_default=True,
    )


def test_settings_reads_and_normalizes_scoped_online_knowledge_proxy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config_module, "DEFAULT_ENV_FILE", tmp_path / "missing.env")
    monkeypatch.setenv(
        "ONLINE_KNOWLEDGE_PROXY_URL",
        "  http://proxy.example.test:17892  ",
    )

    settings = Settings()

    assert settings.online_knowledge_proxy_url == "http://proxy.example.test:17892"


@pytest.mark.parametrize(
    "value",
    [
        "socks5://proxy.example.test:1080",
        "http://user:secret@proxy.example.test:8080",
        "http://proxy.example.test:8080/path",
        "http://proxy.example.test:8080?mode=test",
        "http://proxy.example.test:8080#fragment",
        "http://proxy.example.test:invalid",
    ],
)
def test_scoped_proxy_rejects_unsafe_or_malformed_urls(value: str) -> None:
    with pytest.raises(ValueError, match="ONLINE_KNOWLEDGE_PROXY_URL"):
        _normalize_http_proxy_url("ONLINE_KNOWLEDGE_PROXY_URL", value)


def test_polymer_extractor_uses_only_explicit_proxy_and_ignores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_http_options: list[dict[str, object]] = []
    captured_openai_options: list[dict[str, object]] = []
    closed: list[bool] = []

    def fake_http_client(**options):
        captured_http_options.append(options)
        return object()

    class FakeOpenAI:
        def __init__(self, **options) -> None:
            captured_openai_options.append(options)

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(extractor_module, "DefaultHttpxClient", fake_http_client)
    monkeypatch.setattr(extractor_module, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-be-inherited.example.test:9999")

    configured = extractor_module.PolymerExtractor(
        api_key="test-key",
        base_url="https://api.example.test/v1/",
        model_name="test-model",
        proxy_url="http://proxy.example.test:17892",
    )
    configured.close()
    direct = extractor_module.PolymerExtractor(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        model_name="test-model",
    )
    direct.close()

    assert captured_http_options == [
        {
            "trust_env": False,
            "proxy": "http://proxy.example.test:17892",
        },
        {"trust_env": False},
    ]
    assert captured_openai_options[0]["base_url"] == "https://api.example.test/v1"
    assert len(closed) == 2


def test_search_service_passes_proxy_and_closes_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeSearcher:
        DEFAULT_SOURCES = ("fixture",)

        def __init__(self, max_workers: int) -> None:
            captured["max_workers"] = max_workers

        def search_all(self, material: str, max_papers: int):
            return [
                {
                    "doi": "fixture-doi",
                    "title": material,
                    "abstract": "A polymer abstract.",
                }
            ]

        def deduplicate(self, papers):
            return papers

        def enrich_batch_with_crossref(self, papers):
            return papers

    class FakeExtractor:
        def __init__(self, **options) -> None:
            captured["extractor_options"] = options

        def process_papers(self, papers, **options):
            return [{"has_polymerization": False, "reactions": []}]

        def convert_to_rows(self, mode: str):
            return []

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(search_service, "SimpleLiteratureSearcher", FakeSearcher)
    monkeypatch.setattr(search_service, "PolymerExtractor", FakeExtractor)

    result = search_service.run_online_knowledge_search(
        material="polyethylene",
        mode="synthesis",
        api_key="test-key",
        base_url="https://api.example.test/v1",
        model="test-model",
        max_papers=1,
        extraction_delay_seconds=0,
        proxy_url="http://proxy.example.test:17892",
    )

    assert captured["extractor_options"] == {
        "api_key": "test-key",
        "base_url": "https://api.example.test/v1",
        "model_name": "test-model",
        "proxy_url": "http://proxy.example.test:17892",
    }
    assert captured["closed"] is True
    assert result["totalPapers"] == 1


def test_sync_and_async_routes_propagate_scoped_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_body = _search_request()
    settings = SimpleNamespace(
        online_knowledge_api_key="server-key",
        online_knowledge_proxy_url="http://proxy.example.test:17892",
    )
    access = online_routes.resolve_online_model_access(request_body, settings)
    captured: list[str] = []

    def fake_run_online_knowledge_search(**options):
        captured.append(options["proxy_url"])
        raise RuntimeError("stop after proxy propagation")

    @contextmanager
    def fake_postgres_connection(_dsn):
        yield object()

    monkeypatch.setattr(
        online_routes,
        "run_online_knowledge_search",
        fake_run_online_knowledge_search,
    )
    monkeypatch.setattr(online_routes, "postgres_connection", fake_postgres_connection)
    monkeypatch.setattr(online_routes, "mark_online_job_running_postgres", lambda *_: None)
    monkeypatch.setattr(online_routes, "mark_online_job_failed_postgres", lambda *_: None)

    with pytest.raises(RuntimeError, match="stop after proxy propagation"):
        online_routes._run_search_from_request(request_body, access)
    online_routes._run_online_knowledge_job(
        "test-job",
        "postgresql://example.test/db",
        request_body,
        access,
    )

    assert captured == [
        "http://proxy.example.test:17892",
        "http://proxy.example.test:17892",
    ]


def test_default_config_response_does_not_expose_proxy() -> None:
    settings = SimpleNamespace(
        online_knowledge_api_key="server-key",
        online_knowledge_base_url="https://api.example.test/v1",
        online_knowledge_model="test-model",
        online_knowledge_max_papers=20,
        online_knowledge_proxy_url="http://proxy.example.test:17892",
    )

    response = online_routes.get_online_knowledge_default_config(
        _request_with_settings(settings)
    )

    assert response.model_dump() == {
        "base_url": "https://api.example.test/v1",
        "model": "test-model",
        "max_papers": 20,
        "has_server_api_key": True,
    }

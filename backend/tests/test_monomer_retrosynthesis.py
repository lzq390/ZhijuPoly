from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from time import perf_counter
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import MonomerRetrosynthesisResponse
from app.services.gpu_runtime_registry import GpuRuntimeRegistry
from app.services.monomer_retrosynthesis import _get_runtime, _resolve_device
from app.utils.exceptions import ModelArtifactError


def _settings(tmp_path: Path, *, retro_model_enabled: bool = True) -> Settings:
    return Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
        retro_model_enabled=retro_model_enabled,
    )


def _app_with_fake_retro_runtime(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    registry = GpuRuntimeRegistry()
    registry.register("retrosynthesis", enabled=True, loader=object)
    app.state.gpu_runtime_registry = registry
    return app


def test_monomer_retrosynthesis_route_reports_disabled_service(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, retro_model_enabled=False)))

    response = client.post(
        "/api/v1/monomer-retrosynthesis",
        json={
            "smiles": "Nc1ccc(N)cc1",
            "target_role": "auto",
            "num_beams": 2,
            "num_return_sequences": 1,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "retrosynthesis service is disabled"


def test_monomer_retrosynthesis_route_returns_model_unavailable_as_503(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_model(*args, **kwargs):
        raise ModelArtifactError("retrosynthesis model is unavailable")

    monkeypatch.setattr("app.routers.monomer_retrosynthesis.predict_monomer_precursors", fail_model)
    client = TestClient(_app_with_fake_retro_runtime(tmp_path))

    response = client.post(
        "/api/v1/monomer-retrosynthesis",
        json={
            "smiles": "Nc1ccc(N)cc1",
            "target_role": "auto",
            "num_beams": 2,
            "num_return_sequences": 1,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "retrosynthesis service is unavailable"


def test_monomer_retrosynthesis_route_returns_candidates(tmp_path: Path, monkeypatch) -> None:
    def fake_model(*args, **kwargs) -> MonomerRetrosynthesisResponse:
        return MonomerRetrosynthesisResponse(
            input_smiles="Nc1ccc(N)cc1",
            canonical_smiles="Nc1ccc(N)cc1",
            target_role="auto",
            inferred_target_role="diamine",
            model_id="fixture-model",
            device="cpu",
            query_time_ms=1.0,
            total=0,
            candidates=[],
        )

    monkeypatch.setattr("app.routers.monomer_retrosynthesis.predict_monomer_precursors", fake_model)
    client = TestClient(_app_with_fake_retro_runtime(tmp_path))

    response = client.post(
        "/api/v1/monomer-retrosynthesis",
        json={
            "smiles": "Nc1ccc(N)cc1",
            "target_role": "auto",
            "num_beams": 2,
            "num_return_sequences": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["inferred_target_role"] == "diamine"


def test_retrosynthesis_inference_does_not_block_health_endpoint(tmp_path: Path, monkeypatch) -> None:
    started = Event()
    release = Event()

    def blocking_model(*args, **kwargs) -> MonomerRetrosynthesisResponse:
        started.set()
        assert release.wait(timeout=2)
        return MonomerRetrosynthesisResponse(
            input_smiles="Nc1ccc(N)cc1",
            canonical_smiles="Nc1ccc(N)cc1",
            target_role="auto",
            inferred_target_role="diamine",
            model_id="fixture-model",
            device="cpu",
            query_time_ms=1.0,
            total=0,
            candidates=[],
        )

    monkeypatch.setattr("app.routers.monomer_retrosynthesis.predict_monomer_precursors", blocking_model)
    app = _app_with_fake_retro_runtime(tmp_path)
    response_holder = []

    with TestClient(app) as client:
        inference_thread = Thread(
            target=lambda: response_holder.append(
                client.post(
                    "/api/v1/monomer-retrosynthesis",
                    json={
                        "smiles": "Nc1ccc(N)cc1",
                        "target_role": "auto",
                        "num_beams": 2,
                        "num_return_sequences": 1,
                    },
                )
            )
        )
        inference_thread.start()
        assert started.wait(timeout=2)
        health_started = perf_counter()
        health_response = client.get("/health")
        health_elapsed = perf_counter() - health_started
        release.set()
        inference_thread.join(timeout=2)

    assert health_response.status_code == 200
    assert health_elapsed < 0.5
    assert response_holder[0].status_code == 200


def test_invalid_retrosynthesis_input_does_not_trigger_lazy_model_load(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    load_calls = 0

    def load_runtime():
        nonlocal load_calls
        load_calls += 1
        return object()

    registry = GpuRuntimeRegistry()
    registry.register("retrosynthesis", enabled=True, loader=load_runtime)
    app.state.gpu_runtime_registry = registry
    client = TestClient(app)

    response = client.post(
        "/api/v1/monomer-retrosynthesis",
        json={
            "smiles": "not-a-smiles",
            "target_role": "auto",
            "num_beams": 2,
            "num_return_sequences": 1,
        },
    )

    assert response.status_code == 422
    assert load_calls == 0


def test_retrosynthesis_queue_full_returns_429_with_retry_after(tmp_path: Path) -> None:
    app = _app_with_fake_retro_runtime(tmp_path)
    registry = GpuRuntimeRegistry(max_concurrent_inferences=1, max_waiting_inferences=0)
    registry.register("retrosynthesis", enabled=True, loader=object)
    app.state.gpu_runtime_registry = registry
    holder_started = Event()
    release_holder = Event()

    def hold_gpu() -> None:
        with registry.inference_session("retrosynthesis", timeout_seconds=2):
            holder_started.set()
            assert release_holder.wait(timeout=2)

    with TestClient(app) as client:
        holder = Thread(target=hold_gpu)
        holder.start()
        assert holder_started.wait(timeout=2)
        response = client.post(
            "/api/v1/monomer-retrosynthesis",
            json={
                "smiles": "Nc1ccc(N)cc1",
                "target_role": "auto",
                "num_beams": 2,
                "num_return_sequences": 1,
            },
        )
        release_holder.set()
        holder.join(timeout=2)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"].startswith("GPU_QUEUE_FULL:")


def test_monomer_retrosynthesis_auto_device_falls_back_for_unsupported_cuda(monkeypatch) -> None:
    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_device_capability() -> tuple[int, int]:
            return (6, 1)

        @staticmethod
        def get_arch_list() -> list[str]:
            return ["sm_75", "sm_80"]

    class FakeTorch:
        cuda = FakeCuda()

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            return FakeTorch
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    assert _resolve_device("auto") == "cpu"


def test_reaction_t5_runtime_uses_local_model_files_only(monkeypatch) -> None:
    import sys

    from app.services import monomer_retrosynthesis

    calls = []

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, model_id: str, **kwargs):
            calls.append(("tokenizer", model_id, kwargs))
            return cls()

    class FakeModel:
        @classmethod
        def from_pretrained(cls, model_id: str, **kwargs):
            calls.append(("model", model_id, kwargs))
            return cls()

        def to(self, device: str) -> None:
            self.device = device

        def eval(self) -> None:
            self.eval_called = True

    fake_torch = SimpleNamespace()
    fake_transformers = SimpleNamespace(
        AutoModelForSeq2SeqLM=FakeModel,
        AutoTokenizer=FakeTokenizer,
    )
    monomer_retrosynthesis._RUNTIME_CACHE.clear()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    runtime = _get_runtime("local-reactiont5", "cpu")

    assert runtime.device == "cpu"
    assert calls == [
        ("tokenizer", "local-reactiont5", {"local_files_only": True}),
        ("model", "local-reactiont5", {"local_files_only": True}),
    ]
    monomer_retrosynthesis._RUNTIME_CACHE.clear()

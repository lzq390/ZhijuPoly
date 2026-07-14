from __future__ import annotations

from contextlib import contextmanager
from threading import Event
from time import monotonic, sleep
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.routers import polytao as polytao_router_module
from app.routers.polytao import router as polytao_router
from app.services.gpu_runtime_registry import GpuRuntimeRegistry
from app.services.in_memory_jobs import BoundedInMemoryJobStore
from app.services.polytao import normalize_polytao_candidates
from app.services.polytao_jobs import PolytaoJobManager
from app.services.polytao_runtime import (
    MAX_GENERATION_BATCH_SIZE,
    BackendPolytaoRuntime,
    PolytaoGenerationResult,
    RuntimeProbe,
)


DEFAULT_DESCRIPTORS = {
    "MolWt": 264,
    "HeavyAtomCount": 19,
    "NHOHCount": 0,
    "NOCount": 4,
    "NumAliphaticCarbocycles": 1,
    "NumAliphaticHeterocycles": 0,
    "NumAliphaticRings": 1,
    "NumAromaticCarbocycles": 0,
    "NumAromaticHeterocycles": 0,
    "NumAromaticRings": 0,
    "NumHAcceptors": 4,
    "NumHDonors": 0,
    "NumHeteroatoms": 6,
    "NumRotatableBonds": 5,
    "RingCount": 1,
}


class FakePolytaoRuntime:
    def __init__(self, *, blocker: tuple[Event, Event] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.blocker = blocker

    def probe(self) -> RuntimeProbe:
        return RuntimeProbe(model_files_ready=True, runtime_ready=True)

    def generate(self, **kwargs: Any) -> PolytaoGenerationResult:
        self.calls.append(kwargs)
        if self.blocker is not None:
            started, release = self.blocker
            started.set()
            assert release.wait(timeout=3)
        result = {
            "prompt": kwargs["prompt"],
            "query_time_ms": 1.5,
            "requested_count": kwargs["candidate_count"],
            "returned_count": 1,
            "attempts": 1,
            "filter_counter": {"duplicate": 1},
            "results": [
                {
                    "rank": 1,
                    "generated_smiles": "*CC*",
                    "raw_smiles": "[*]CC[*]",
                    "valid_smiles": True,
                    "sa_score": None,
                    "warnings": [],
                }
            ],
        }
        return PolytaoGenerationResult(result=result, query_time_ms=1.5, returned_count=1)


class ColdPolytaoRuntime(FakePolytaoRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.loaded = False
        self.ensure_loaded_calls = 0

    def ensure_loaded(self):
        self.ensure_loaded_calls += 1
        self.loaded = True
        return self


class DegradedPolytaoRuntime:
    def probe(self) -> RuntimeProbe:
        return RuntimeProbe(
            model_files_ready=False,
            runtime_ready=False,
            runtime_error="missing PolyTAO model files: config.json",
        )

    def generate(self, **kwargs: Any) -> PolytaoGenerationResult:
        raise AssertionError("degraded PolyTAO runtime must not receive jobs")


class FailingPolytaoRuntime(FakePolytaoRuntime):
    def generate(self, **kwargs: Any) -> PolytaoGenerationResult:
        raise RuntimeError("backend runtime failed")


def _create_app(
    *,
    runtime: object | None = None,
    polytao_enabled: bool = True,
    polytao_max_active_jobs: int = 1,
    polytao_rate_limit_per_ip_per_minute: int = 5,
    polytao_rate_limit_window_seconds: int = 60,
    max_waiting_inferences: int = 8,
) -> FastAPI:
    settings = Settings(
        app_postgres_dsn="postgresql://unused",
        pi_postgres_dsn="postgresql://unused",
        lab_data_postgres_dsn="postgresql://unused",
        csv_source_path="database/data1.csv",
        experimental_process_csv_path="database/missing_process.csv",
        experimental_property_csv_path="database/missing_property.csv",
        allowed_origins="http://localhost:5173",
        structured_data_backend="postgres",
        pi_reverse_backend="postgres",
        model_enabled=False,
        polytao_enabled=polytao_enabled,
        polytao_max_active_jobs=polytao_max_active_jobs,
        polytao_rate_limit_per_ip_per_minute=polytao_rate_limit_per_ip_per_minute,
        polytao_rate_limit_window_seconds=polytao_rate_limit_window_seconds,
    )
    app = FastAPI()
    app.state.settings = settings
    selected_runtime = runtime if runtime is not None else FakePolytaoRuntime()
    app.state.polytao_runtime = selected_runtime
    registry = GpuRuntimeRegistry(max_waiting_inferences=max_waiting_inferences)

    def load_runtime():
        ensure_loaded = getattr(selected_runtime, "ensure_loaded", None)
        return ensure_loaded() if callable(ensure_loaded) else selected_runtime

    registry.register("polytao", enabled=polytao_enabled, loader=load_runtime)
    if polytao_enabled:
        probe = selected_runtime.probe()
        if (
            probe.model_files_ready
            and probe.runtime_ready
            and getattr(selected_runtime, "loaded", True) is not False
        ):
            registry.mark_ready("polytao", selected_runtime)
    app.state.gpu_runtime_registry = registry
    app.state.in_memory_job_store = BoundedInMemoryJobStore(instance_id="a" * 16)
    app.state.polytao_job_manager = PolytaoJobManager(
        max_workers=1,
        max_active_jobs=polytao_max_active_jobs,
        store=app.state.in_memory_job_store,
    )
    app.include_router(polytao_router)
    return app


@contextmanager
def _client_for(app: FastAPI):
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.state.polytao_job_manager.shutdown(wait=True)


def _request_payload(**overrides):
    payload = {
        "descriptors": DEFAULT_DESCRIPTORS,
        "input_smiles": None,
        "candidate_count": 1,
        "temperature": 1.0,
        "top_k": 100,
        "top_p": 0.999,
        "max_length": 300,
    }
    payload.update(overrides)
    return payload


def _wait_for_terminal(client: TestClient, job_id: str, *, timeout: float = 3.0) -> dict[str, Any]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        response = client.get(f"/api/v1/conditional-generation/polytao/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        sleep(0.01)
    raise AssertionError("PolyTAO job did not reach a terminal state")


def test_polytao_descriptor_endpoint_calculates_rdkit_prompt() -> None:
    app = _create_app(polytao_enabled=False)
    with _client_for(app) as client:
        response = client.post(
            "/api/v1/conditional-generation/polytao/descriptors",
            json={"smiles": "CCO"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["canonical_smiles"] == "CCO"
    assert len(data["descriptors"]) == 15
    assert data["descriptors"][1] == {"name": "HeavyAtomCount", "value": 3.0}
    assert len(data["prompt"].split(",")) == 15


def test_polytao_status_is_process_local_and_does_not_report_database_health() -> None:
    app = _create_app()
    with _client_for(app) as client:
        response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["available"] is True
    assert data["runtime_ready"] is True
    assert data["worker_mode"] == "backend-in-memory"
    assert data["db_configured"] is None
    assert data["db_ready"] is None
    assert data["db_error"] is None
    assert data["active_jobs"] == 0


def test_polytao_status_reports_disabled_runtime() -> None:
    app = _create_app(polytao_enabled=False)
    with _client_for(app) as client:
        data = client.get("/api/v1/conditional-generation/polytao/status").json()

    assert data["enabled"] is False
    assert data["available"] is False


def test_lazy_status_is_cold_without_loading_and_first_job_loads_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ColdPolytaoRuntime()
    monkeypatch.setattr(polytao_router_module, "generate_2d_svg", lambda smiles: "<svg />")
    app = _create_app(runtime=runtime)
    with _client_for(app) as client:
        status_response = client.get("/api/v1/conditional-generation/polytao/status")
        assert status_response.json()["worker_status"] == "cold"
        assert status_response.json()["available"] is True
        assert runtime.ensure_loaded_calls == 0

        submitted = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
        )
        assert submitted.status_code == 202
        job = _wait_for_terminal(client, submitted.json()["job_id"])

    assert job["status"] == "completed"
    assert runtime.ensure_loaded_calls == 1


def test_completed_job_retains_svg_and_get_does_not_recompute_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svg_calls: list[str] = []
    monkeypatch.setattr(
        polytao_router_module,
        "generate_2d_svg",
        lambda smiles: (svg_calls.append(smiles), "<svg>stored</svg>")[1],
    )
    app = _create_app()
    with _client_for(app) as client:
        submitted = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
        )
        job_id = submitted.json()["job_id"]
        completed = _wait_for_terminal(client, job_id)
        second_read = client.get(
            f"/api/v1/conditional-generation/polytao/jobs/{job_id}"
        ).json()

    assert completed["result"]["results"][0]["structure_svg"] == "<svg>stored</svg>"
    assert second_read["result"] == completed["result"]
    assert svg_calls == ["*CC*"]


def test_gpu_queue_full_is_an_accepted_job_with_failed_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(polytao_router_module, "generate_2d_svg", lambda smiles: "<svg />")
    app = _create_app(max_waiting_inferences=0)
    registry = app.state.gpu_runtime_registry
    with registry.inference_session("polytao", timeout_seconds=1):
        with _client_for(app) as client:
            submitted = client.post(
                "/api/v1/conditional-generation/polytao/jobs",
                json=_request_payload(),
            )
            assert submitted.status_code == 202
            job = _wait_for_terminal(client, submitted.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error_message"].startswith("GPU_QUEUE_FULL:")


def test_degraded_runtime_rejects_submission_without_retaining_a_job() -> None:
    app = _create_app(runtime=DegradedPolytaoRuntime())
    with _client_for(app) as client:
        status_response = client.get("/api/v1/conditional-generation/polytao/status")
        submitted = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
        )

    assert status_response.json()["available"] is False
    assert submitted.status_code == 503
    assert app.state.polytao_job_manager.retained_jobs == 0


def test_runtime_failure_is_retained_as_failed_job() -> None:
    app = _create_app(runtime=FailingPolytaoRuntime())
    with _client_for(app) as client:
        submitted = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
        )
        assert submitted.status_code == 202
        job = _wait_for_terminal(client, submitted.json()["job_id"])

    assert job["status"] == "failed"
    assert job["error_message"] == "backend runtime failed"


def test_submit_rate_limit_is_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(polytao_router_module, "generate_2d_svg", lambda smiles: "<svg />")
    app = _create_app(polytao_max_active_jobs=2, polytao_rate_limit_per_ip_per_minute=1)
    with _client_for(app) as client:
        first = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
            headers={"x-forwarded-for": "198.51.100.1"},
        )
        second = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
            headers={"x-forwarded-for": "198.51.100.1"},
        )
        other_ip = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
            headers={"x-forwarded-for": "198.51.100.2"},
        )

    assert first.status_code == 202
    assert second.status_code == 429
    assert other_ip.status_code == 202


def test_lane_capacity_is_atomic_and_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(polytao_router_module, "generate_2d_svg", lambda smiles: "<svg />")
    started = Event()
    release = Event()
    app = _create_app(runtime=FakePolytaoRuntime(blocker=(started, release)))
    with _client_for(app) as client:
        first = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
        )
        assert first.status_code == 202
        assert started.wait(timeout=3)
        second = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(),
        )
        release.set()
        _wait_for_terminal(client, first.json()["job_id"])

    assert second.status_code == 429
    assert second.headers["retry-after"] == "5"
    assert app.state.polytao_job_manager.retained_jobs == 1


def test_job_lookup_uses_404_for_wrong_namespace_and_410_for_old_instance() -> None:
    app = _create_app()
    current_token = "0" * 32
    with _client_for(app) as client:
        malformed = client.get("/api/v1/conditional-generation/polytao/jobs/not-a-job")
        wrong_namespace = client.get(
            "/api/v1/conditional-generation/polytao/jobs/"
            f"conditional_generation.{('a' * 16)}.{current_token}"
        )
        old_instance = client.get(
            "/api/v1/conditional-generation/polytao/jobs/"
            f"polytao.{('b' * 16)}.{current_token}"
        )
        legacy_uuid = client.get(
            "/api/v1/conditional-generation/polytao/jobs/"
            "123e4567-e89b-42d3-a456-426614174000"
        )
        legacy_uuid_hex = client.get(
            "/api/v1/conditional-generation/polytao/jobs/"
            "123e4567e89b42d3a456426614174000"
        )

    assert malformed.status_code == 404
    assert wrong_namespace.status_code == 404
    assert old_instance.status_code == 410
    assert legacy_uuid.status_code == 410
    assert legacy_uuid_hex.status_code == 410


def test_status_becomes_unavailable_after_job_admission_stops() -> None:
    app = _create_app()
    app.state.polytao_job_manager.stop_accepting()
    with _client_for(app) as client:
        status_response = client.get("/api/v1/conditional-generation/polytao/status")

    assert status_response.status_code == 200
    assert status_response.json()["available"] is False


def test_generation_request_rejects_missing_descriptor() -> None:
    descriptors = dict(DEFAULT_DESCRIPTORS)
    descriptors.pop("MolWt")
    app = _create_app()
    with _client_for(app) as client:
        response = client.post(
            "/api/v1/conditional-generation/polytao/jobs",
            json=_request_payload(descriptors=descriptors),
        )

    assert response.status_code == 422


def test_polytao_candidate_normalization_filters_invalid_duplicates_and_attachment_points() -> None:
    candidates, filters = normalize_polytao_candidates(
        ["", "not-a-smiles", "CCO", "[*]CC[*]", "*CC*", "*OCC*"],
        requested_count=10,
    )

    assert [candidate.generated_smiles for candidate in candidates] == ["*CC*", "*CCO*"]
    assert filters["empty_raw_smiles"] == 1
    assert filters["rdkit_parse_failed"] == 1
    assert filters["star_count_lt_2"] == 1
    assert filters["duplicate"] == 1


def test_backend_runtime_micro_batches_maximum_request(tmp_path) -> None:
    class Tensor:
        def to(self, _device):
            return self

    class Tokenizer:
        pad_token_id = 0
        eos_token_id = 1

        def __call__(self, _prompt, *, return_tensors):
            assert return_tensors == "pt"
            return {"input_ids": Tensor()}

        def decode(self, _output, *, skip_special_tokens):
            assert skip_special_tokens is True
            return "[*]CC[*]"

    class Model:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def generate(self, **kwargs):
            batch_size = int(kwargs["num_return_sequences"])
            self.batch_sizes.append(batch_size)
            return list(range(batch_size))

    class Cuda:
        def __init__(self) -> None:
            self.empty_cache_calls = 0

        def empty_cache(self) -> None:
            self.empty_cache_calls += 1

    class Torch:
        def __init__(self) -> None:
            self.cuda = Cuda()

        @contextmanager
        def inference_mode(self):
            yield

    tokenizer = Tokenizer()
    model = Model()
    torch = Torch()
    runtime = BackendPolytaoRuntime(
        model_dir=tmp_path,
        device="cuda",
        model_id="polytao-test",
    )
    runtime._load = lambda: (tokenizer, model, torch, "cuda")  # type: ignore[method-assign]

    generated = runtime.generate(
        prompt="test",
        candidate_count=50,
        temperature=2.0,
        top_k=500,
        top_p=1.0,
        max_length=512,
    )

    assert model.batch_sizes == [MAX_GENERATION_BATCH_SIZE] * 50
    assert max(model.batch_sizes) == 2
    assert torch.cuda.empty_cache_calls == 50
    assert generated.result["attempts"] == 1
    assert generated.returned_count == 1


def test_backend_runtime_rejects_empty_model_output(tmp_path) -> None:
    class Tensor:
        def to(self, _device):
            return self

    class Tokenizer:
        pad_token_id = 0
        eos_token_id = 1

        def __call__(self, _prompt, *, return_tensors):
            return {"input_ids": Tensor()}

        def decode(self, _output, *, skip_special_tokens):
            return "[*]CC[*]"

    class Model:
        def generate(self, **_kwargs):
            return []

    class Cuda:
        def empty_cache(self) -> None:
            pass

    class Torch:
        cuda = Cuda()

        @contextmanager
        def inference_mode(self):
            yield

    runtime = BackendPolytaoRuntime(
        model_dir=tmp_path,
        device="cuda",
        model_id="polytao-test",
    )
    runtime._load = lambda: (Tokenizer(), Model(), Torch(), "cuda")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="no decoded candidates"):
        runtime.generate(
            prompt="test",
            candidate_count=1,
            temperature=1.0,
            top_k=100,
            top_p=0.999,
            max_length=300,
        )


def test_backend_runtime_bounds_all_filtered_batches(tmp_path) -> None:
    class Tensor:
        def to(self, _device):
            return self

    class Tokenizer:
        pad_token_id = 0
        eos_token_id = 1

        def __call__(self, _prompt, *, return_tensors):
            return {"input_ids": Tensor()}

        def decode(self, _output, *, skip_special_tokens):
            return ""

    class Model:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            return list(range(int(kwargs["num_return_sequences"])))

    class Cuda:
        def empty_cache(self) -> None:
            pass

    class Torch:
        cuda = Cuda()

        @contextmanager
        def inference_mode(self):
            yield

    model = Model()
    runtime = BackendPolytaoRuntime(
        model_dir=tmp_path,
        device="cuda",
        model_id="polytao-test",
    )
    runtime._load = lambda: (Tokenizer(), model, Torch(), "cuda")  # type: ignore[method-assign]

    generated = runtime.generate(
        prompt="test",
        candidate_count=50,
        temperature=1.0,
        top_k=100,
        top_p=0.999,
        max_length=300,
    )

    assert model.calls == 50
    assert generated.returned_count == 0
    assert generated.result["filter_counter"] == {"empty_raw_smiles": 100}


@pytest.mark.parametrize("failure_stage", ["encode", "generate", "decode"])
def test_backend_runtime_cleanup_preserves_primary_batch_error(
    tmp_path,
    failure_stage: str,
) -> None:
    class Tensor:
        def to(self, _device):
            if failure_stage == "encode":
                raise RuntimeError("encode failed")
            return self

    class Tokenizer:
        pad_token_id = 0
        eos_token_id = 1

        def __call__(self, _prompt, *, return_tensors):
            return {"input_ids": Tensor()}

        def decode(self, _output, *, skip_special_tokens):
            if failure_stage == "decode":
                raise RuntimeError("decode failed")
            return "[*]CC[*]"

    class Model:
        def generate(self, **kwargs):
            if failure_stage == "generate":
                raise RuntimeError("generate failed")
            return list(range(int(kwargs["num_return_sequences"])))

    class Cuda:
        def __init__(self) -> None:
            self.empty_cache_calls = 0

        def empty_cache(self) -> None:
            self.empty_cache_calls += 1
            raise RuntimeError("cleanup failed")

    class Torch:
        def __init__(self) -> None:
            self.cuda = Cuda()

        @contextmanager
        def inference_mode(self):
            yield

    torch = Torch()
    runtime = BackendPolytaoRuntime(
        model_dir=tmp_path,
        device="cuda",
        model_id="polytao-test",
    )
    runtime._load = lambda: (Tokenizer(), Model(), torch, "cuda")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match=rf"^{failure_stage} failed$"):
        runtime.generate(
            prompt="test",
            candidate_count=1,
            temperature=1.0,
            top_k=100,
            top_p=0.999,
            max_length=300,
        )

    assert torch.cuda.empty_cache_calls == 1

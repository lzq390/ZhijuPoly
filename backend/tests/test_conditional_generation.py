from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.models import (
    ConditionalGenerationCandidate,
    ConditionalGenerationTgRequest,
    ConditionalGenerationTgResponse,
)
from app.services.conditional_generation import (
    GeneratedSmiles,
    run_conditional_generation,
    to_model_smiles,
    to_rdkit_smiles,
)
import app.services.conditional_generation_runtime as runtime_module
from app.services.conditional_generation_runtime import TorchConditionalGenerationRuntime, _resolve_device
from app.utils.exceptions import ModelArtifactError


class FakeRuntime:
    def __init__(self, generated: list[str], predicted_tg: dict[str, float]) -> None:
        self.generated = generated
        self.predicted_tg = predicted_tg
        self.calls: list[float] = []

    def generate_once(
        self,
        *,
        input_smiles: str,
        delta_tg: float,
        top_k: int,
        temperature: float,
        max_length: int,
    ) -> GeneratedSmiles:
        self.calls.append(delta_tg)
        value = self.generated.pop(0) if self.generated else ""
        return GeneratedSmiles(raw_smiles=value, rdkit_smiles=to_rdkit_smiles(value))

    def predict_tg(self, smiles: str) -> float:
        return self.predicted_tg[smiles]


class ArtifactErrorRuntime(FakeRuntime):
    def generate_once(
        self,
        *,
        input_smiles: str,
        delta_tg: float,
        top_k: int,
        temperature: float,
        max_length: int,
    ) -> GeneratedSmiles:
        raise ModelArtifactError("missing model dependency")


class FakeCuda:
    def __init__(self, *, available: bool, capability: tuple[int, int], arches: list[str]) -> None:
        self._available = available
        self._capability = capability
        self._arches = arches

    def is_available(self) -> bool:
        return self._available

    def get_device_capability(self) -> tuple[int, int]:
        return self._capability

    def get_arch_list(self) -> list[str]:
        return self._arches


class FakeTorch:
    def __init__(self, cuda: FakeCuda) -> None:
        self.cuda = cuda


class FakeModel:
    def __init__(self, *args: object, **kwargs: object) -> None:
        return None

    def to(self, device: str) -> "FakeModel":
        return self

    def load_state_dict(self, state: dict, strict: bool = False) -> None:
        return None

    def eval(self) -> None:
        return None


def test_star_smiles_normalization_accepts_bracketed_and_bare_stars() -> None:
    assert to_model_smiles("[*]CC[*]") == "*CC*"
    assert to_rdkit_smiles("*CC*") == "*CC*"


def test_star_smiles_normalization_rejects_invalid_smiles() -> None:
    assert to_model_smiles("not-a-smiles") is None
    assert to_rdkit_smiles("not-a-smiles") is None


def test_conditional_generation_filters_candidates_and_uses_delta_tg_directly() -> None:
    runtime = FakeRuntime(
        generated=["*CC*", "not-a-smiles", "*CCC*", "*CCC*", "*COC*"],
        predicted_tg={
            "*CCC*": 126.0,
            "*COC*": 131.0,
        },
    )

    result = run_conditional_generation(
        input_smiles="[*]CC[*]",
        delta_tg=30.0,
        candidate_count=2,
        top_k=5,
        temperature=1.0,
        runtime=runtime,
        max_attempts=5,
    )

    assert runtime.calls == [30.0, 30.0, 30.0, 30.0, 30.0]
    assert result.delta_tg == 30.0
    assert result.attempts == 5
    assert result.filter_counter == {
        "same_as_input": 1,
        "rdkit_parse_failed": 1,
        "duplicate": 1,
    }
    assert [candidate.generated_smiles for candidate in result.candidates] == ["*CCC*", "*COC*"]
    assert [candidate.tg_error for candidate in result.candidates] == [None, None]
    assert [candidate.rank for candidate in result.candidates] == [1, 2]


def test_conditional_generation_rejects_input_without_two_attachment_points() -> None:
    runtime = FakeRuntime(generated=["*CCC*"], predicted_tg={"*CCC*": 130.0})

    with pytest.raises(ValueError, match="at least two attachment"):
        run_conditional_generation(
            input_smiles="*CC",
            delta_tg=20.0,
            candidate_count=1,
            top_k=5,
            temperature=1.0,
            runtime=runtime,
        )


def test_conditional_generation_propagates_model_artifact_errors() -> None:
    runtime = ArtifactErrorRuntime(generated=[], predicted_tg={})

    with pytest.raises(ModelArtifactError, match="missing model dependency"):
        run_conditional_generation(
            input_smiles="*CC*",
            delta_tg=20.0,
            candidate_count=1,
            top_k=5,
            temperature=1.0,
            runtime=runtime,
        )


def test_generation_device_auto_falls_back_when_cuda_arch_is_unsupported() -> None:
    torch_module = FakeTorch(FakeCuda(available=True, capability=(6, 1), arches=["sm_75", "sm_80"]))

    assert _resolve_device(torch_module, "auto") == "cpu"
    with pytest.raises(ModelArtifactError, match="sm_61"):
        _resolve_device(torch_module, "cuda")


@pytest.mark.parametrize(
    "delta_tg",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_conditional_generation_request_rejects_non_finite_delta_tg(delta_tg: float) -> None:
    with pytest.raises(ValidationError):
        ConditionalGenerationTgRequest(
            smiles="*CC*",
            delta_tg=delta_tg,
        )


def test_conditional_generation_runtime_load_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = TorchConditionalGenerationRuntime(model_dir=Path("/unused"), device="auto")
    first_entered = Event()
    release_first = Event()
    entry_lock = Lock()
    load_entries = 0

    def fake_assert_artifacts() -> None:
        nonlocal load_entries
        with entry_lock:
            load_entries += 1
            current_entry = load_entries
        if current_entry == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)

    fake_torch = SimpleNamespace(
        nn=SimpleNamespace(),
        cuda=SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None),
        load=lambda *args, **kwargs: {"model_state_dict": {}, "cond_mean": 0.0, "cond_std": 1.0},
    )
    fake_transformers = SimpleNamespace(
        AutoModel=object,
        AutoTokenizer=SimpleNamespace(from_pretrained=lambda path: object()),
    )
    fake_featurizers = SimpleNamespace(SimpleMoleculeMolGraphFeaturizer=object)
    fake_data = SimpleNamespace(BatchMolGraph=object)
    fake_nn = SimpleNamespace(BondMessagePassing=object, MeanAggregation=object)

    def fake_require_dependency(name: str) -> object:
        return {
            "torch": fake_torch,
            "torch.nn.functional": object(),
            "transformers": fake_transformers,
            "chemprop.featurizers": fake_featurizers,
            "chemprop.data": fake_data,
            "chemprop.nn": fake_nn,
        }[name]

    monkeypatch.setattr(runtime, "_assert_artifacts", fake_assert_artifacts)
    monkeypatch.setattr(runtime_module, "_require_dependency", fake_require_dependency)
    monkeypatch.setattr(runtime_module, "_resolve_device", lambda torch_module, device_setting: "cpu")
    monkeypatch.setattr(runtime_module, "_build_model_classes", lambda *args: (FakeModel, FakeModel))
    monkeypatch.setattr(runtime_module.joblib, "load", lambda path: [] if str(path).endswith("top10_desc_names.pkl") else object())

    errors: list[BaseException] = []

    def load_runtime() -> None:
        try:
            runtime._load()
        except BaseException as exc:
            errors.append(exc)

    first_thread = Thread(target=load_runtime)
    first_thread.start()
    assert first_entered.wait(timeout=2)

    second_thread = Thread(target=load_runtime)
    second_thread.start()
    sleep(0.05)
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert errors == []
    assert load_entries == 1


def test_conditional_generation_job_api_reports_disabled_service(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
        gen_model_enabled=False,
    )
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/v1/conditional-generation/tg/jobs",
        json={
            "smiles": "*CC*",
            "delta_tg": 30,
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "conditional generation service is disabled"


def test_conditional_generation_job_api_rejects_non_finite_tg_without_500(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
        gen_model_enabled=True,
    )
    client = TestClient(create_app(settings), raise_server_exceptions=False)

    response = client.post(
        "/api/v1/conditional-generation/tg/jobs",
        content='{"smiles":"*CC*","delta_tg":NaN}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_conditional_generation_job_api_returns_terminal_result(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
        gen_model_enabled=True,
        gen_job_workers=1,
    )
    app = create_app(settings)
    app.state.conditional_generation_runner = lambda request_body: ConditionalGenerationTgResponse(
        input_smiles=request_body.smiles,
        normalized_input_smiles="*CC*",
        delta_tg=request_body.delta_tg,
        query_time_ms=12.0,
        requested_count=request_body.candidate_count,
        returned_count=1,
        attempts=1,
        filter_counter={},
        results=[
            ConditionalGenerationCandidate(
                rank=1,
                generated_smiles="*COC*",
                structure_svg="<svg />",
                predicted_tg=129.0,
                tg_error=None,
                similarity_score=0.25,
                sa_score=3.5,
            )
        ],
    )

    with TestClient(app) as client:
        create_response = client.post(
            "/api/v1/conditional-generation/tg/jobs",
            json={
                "smiles": "*CC*",
                "delta_tg": 30,
                "candidate_count": 1,
            },
        )
        assert create_response.status_code == 202
        job_id = create_response.json()["job_id"]

        status_payload = None
        for _ in range(20):
            status_response = client.get(f"/api/v1/conditional-generation/tg/jobs/{job_id}")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if status_payload["status"] in {"completed", "failed", "cancelled"}:
                break
            sleep(0.05)

    assert status_payload is not None
    assert status_payload["status"] == "completed"
    assert status_payload["result"]["delta_tg"] == 30.0
    assert status_payload["result"]["results"][0]["tg_error"] is None
    assert status_payload["result"]["results"][0]["generated_smiles"] == "*COC*"

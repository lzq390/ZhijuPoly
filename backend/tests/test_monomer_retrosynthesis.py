from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import MonomerRetrosynthesisResponse
from app.services.monomer_retrosynthesis import _resolve_device
from app.utils.exceptions import ModelArtifactError


def _settings(tmp_path: Path, *, retro_model_enabled: bool = True) -> Settings:
    return Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
        retro_model_enabled=retro_model_enabled,
    )


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
    client = TestClient(create_app(_settings(tmp_path)))

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
    assert response.json()["detail"] == "retrosynthesis model is unavailable"


def test_monomer_retrosynthesis_route_returns_candidates(tmp_path: Path, monkeypatch) -> None:
    def fake_model(*args, **kwargs) -> MonomerRetrosynthesisResponse:
        return MonomerRetrosynthesisResponse(
            input_smiles="Nc1ccc(N)cc1",
            canonical_smiles="Nc1ccc(N)cc1",
            target_role="auto",
            inferred_target_role="diamine",
            model_id="fake",
            device="cpu",
            query_time_ms=1.0,
            total=0,
            candidates=[],
        )

    monkeypatch.setattr("app.routers.monomer_retrosynthesis.predict_monomer_precursors", fake_model)
    client = TestClient(create_app(_settings(tmp_path)))

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

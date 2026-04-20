from __future__ import annotations

from pathlib import Path

import pytest

from app.services import predictor
from app.utils.exceptions import (
    InvalidSmilesError,
    ModelArtifactError,
    UnsupportedPredictionPropertyError,
)


def test_smiles_to_features_returns_expected_shape() -> None:
    features = predictor.smiles_to_features("CCO")

    assert features.shape == (1, predictor.DESCRIPTOR_COUNT)
    assert predictor.DESCRIPTOR_COUNT == 210


def test_smiles_to_features_rejects_invalid_smiles() -> None:
    with pytest.raises(InvalidSmilesError):
        predictor.smiles_to_features("not-a-smiles")


def test_predict_returns_requested_property_subset() -> None:
    result = predictor.predict(
        "CCO",
        ["Glass transition temperature", "O2 Permeability Barrer"],
    )

    assert set(result) == {"Glass transition temperature", "O2 Permeability Barrer"}
    assert all(isinstance(value, float) for value in result.values())


def test_get_available_properties_excludes_temporarily_skipped_model() -> None:
    available = predictor.get_available_properties()

    assert "Glass transition temperature" in available
    assert "Tensile stress strength at break" in available


def test_load_model_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    predictor._model_cache.clear()
    load_calls = 0
    real_joblib_load = predictor.joblib.load

    def counting_load(path: Path):
        nonlocal load_calls
        load_calls += 1
        return real_joblib_load(path)

    monkeypatch.setattr(predictor.joblib, "load", counting_load)

    first = predictor.load_model("Glass transition temperature")
    second = predictor.load_model("Glass transition temperature")

    assert first is second
    assert load_calls == 1


def test_predict_rejects_empty_property_list() -> None:
    with pytest.raises(UnsupportedPredictionPropertyError):
        predictor.predict("CCO", [])


def test_load_model_raises_for_missing_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    predictor._model_cache.clear()
    monkeypatch.setitem(
        predictor.PROPERTY_MODELS,
        "Fake property",
        "missing-model.pkl",
    )

    try:
        with pytest.raises(ModelArtifactError):
            predictor.load_model("Fake property")
    finally:
        predictor.PROPERTY_MODELS.pop("Fake property", None)


def test_predict_supports_tensile_stress_strength_model() -> None:
    result = predictor.predict("CCO", ["Tensile stress strength at break"])

    assert "Tensile stress strength at break" in result
    assert isinstance(result["Tensile stress strength at break"], float)

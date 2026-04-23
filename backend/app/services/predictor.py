from __future__ import annotations

import warnings
from pathlib import Path

import joblib
import numpy as np
from rdkit import Chem
from rdkit.Chem import Descriptors

from app.config import PROJECT_ROOT
from app.utils.exceptions import (
    InvalidSmilesError,
    ModelArtifactError,
    UnsupportedPredictionPropertyError,
)

try:
    from sklearn.exceptions import InconsistentVersionWarning
except Exception:  # pragma: no cover - sklearn is expected at runtime
    InconsistentVersionWarning = Warning


PROPERTY_MODELS: dict[str, str] = {
    "Glass transition temperature": "rf_Glass transition temperature_exp.pkl",
    "Melting temperature": "rf_Melting temperature_exp.pkl",
    "Thermal decomposition temperature": "rf_Thermal decomposition temperature_exp.pkl",
    "Thermal decomposition weight loss": "rf_Thermal decomposition weight loss_exp.pkl",
    "Elongation at break": "rf_Elongation at break_exp.pkl",
    "Tensile stress strength at break": "rf_Tensile stress strength at break_exp.pkl",
    "O2 Permeability Barrer": "rf_O2 Permeability Barrer_exp.pkl",
    "Co2 Permeability Barrer": "rf_Co2 Permeability Barrer_exp.pkl",
    "H2 Permeability Barrer": "rf_H2 Permeability Barrer_exp.pkl",
}

PROPERTY_LABELS_ZH: dict[str, str] = {
    "Glass transition temperature": "玻璃化转变温度",
    "Melting temperature": "熔融温度",
    "Thermal decomposition temperature": "热分解温度",
    "Thermal decomposition weight loss": "热分解失重率",
    "Elongation at break": "断裂伸长率",
    "Tensile stress strength at break": "断裂拉伸强度",
    "O2 Permeability Barrer": "O₂ 渗透性",
    "Co2 Permeability Barrer": "CO₂ 渗透性",
    "H2 Permeability Barrer": "H₂ 渗透性",
}

PROPERTY_UNITS: dict[str, str] = {
    "Glass transition temperature": "°C",
    "Melting temperature": "°C",
    "Thermal decomposition temperature": "°C",
    "Thermal decomposition weight loss": "%",
    "Elongation at break": "%",
    "Tensile stress strength at break": "MPa",
    "O2 Permeability Barrer": "Barrer",
    "Co2 Permeability Barrer": "Barrer",
    "H2 Permeability Barrer": "Barrer",
}

# The shipped models expect the historical 210-descriptor RDKit layout.
# In the current RDKit build, `SPS` appears twice: once near the front and once
# as the last appended descriptor. The trained models align with the first 210
# entries, so we drop only the trailing descriptor.
DESCRIPTOR_ITEMS: tuple[tuple[str, object], ...] = tuple(Descriptors.descList[:-1])
DESCRIPTOR_NAMES: tuple[str, ...] = tuple(name for name, _ in DESCRIPTOR_ITEMS)
DESCRIPTOR_FUNCS = tuple(func for _, func in DESCRIPTOR_ITEMS)
DESCRIPTOR_COUNT = len(DESCRIPTOR_FUNCS)
FEATURE_CLIP_ABS = 1e6

_model_cache: dict[str, object] = {}


def _get_model_dir(model_dir: Path | None = None) -> Path:
    return model_dir or (PROJECT_ROOT / "model")


def _get_model_path(property_name: str, model_dir: Path | None = None) -> Path:
    try:
        model_name = PROPERTY_MODELS[property_name]
    except KeyError as exc:
        raise UnsupportedPredictionPropertyError(
            f"unsupported prediction property: {property_name}"
        ) from exc
    return _get_model_dir(model_dir) / model_name


def _normalize_feature_vector(values: list[float]) -> np.ndarray:
    feature_array = np.asarray(values, dtype=float)
    feature_array = np.nan_to_num(feature_array, nan=0.0, posinf=0.0, neginf=0.0)
    feature_array = np.clip(feature_array, -FEATURE_CLIP_ABS, FEATURE_CLIP_ABS)
    return feature_array.reshape(1, -1)


def get_available_properties(model_dir: Path | None = None) -> list[str]:
    available = []
    for property_name in PROPERTY_MODELS:
        if _get_model_path(property_name, model_dir).exists():
            available.append(property_name)
    return available


def load_model(property_name: str, model_dir: Path | None = None):
    model_path = _get_model_path(property_name, model_dir).resolve()
    cache_key = str(model_path)
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    if not model_path.exists():
        raise ModelArtifactError(f"model file not found for '{property_name}': {model_path}")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", InconsistentVersionWarning)
        model = joblib.load(model_path)

    expected_features = getattr(model, "n_features_in_", None)
    if expected_features is not None and int(expected_features) != DESCRIPTOR_COUNT:
        raise ModelArtifactError(
            "model feature size mismatch for "
            f"'{property_name}': expected {DESCRIPTOR_COUNT}, got {expected_features}"
        )

    if not hasattr(model, "predict"):
        raise ModelArtifactError(f"model artifact does not expose predict(): {model_path}")

    _model_cache[cache_key] = model
    return model


def smiles_to_features(smiles: str) -> np.ndarray:
    value = smiles.strip()
    if not value:
        raise InvalidSmilesError("smiles must not be empty")

    mol = Chem.MolFromSmiles(value)
    if mol is None:
        raise InvalidSmilesError(f"invalid smiles: {smiles}")

    descriptor_values: list[float] = []
    for descriptor_func in DESCRIPTOR_FUNCS:
        try:
            descriptor_values.append(float(descriptor_func(mol)))
        except Exception:
            descriptor_values.append(float("nan"))

    return _normalize_feature_vector(descriptor_values)


def predict(smiles: str, properties: list[str], model_dir: Path | None = None) -> dict[str, float]:
    if not properties:
        raise UnsupportedPredictionPropertyError("at least one property must be requested")

    features = smiles_to_features(smiles)
    available_properties = set(get_available_properties(model_dir))
    predictions: dict[str, float] = {}
    for property_name in properties:
        if property_name not in available_properties:
            raise UnsupportedPredictionPropertyError(
                f"unsupported prediction property: {property_name}"
            )
        model = load_model(property_name, model_dir)
        value = model.predict(features)[0]
        predictions[property_name] = float(value)

    return predictions

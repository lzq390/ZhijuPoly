from __future__ import annotations

import copy
from typing import Any


DEMO_PROTOCOL = "DensityDemo"
FORMAL_PROTOCOLS = ("Density", "Transport", "HVap", "Dielectric", "Compressibility")
ALL_PROTOCOLS = (DEMO_PROTOCOL, *FORMAL_PROTOCOLS)
PATH_FIELDS = ("params_dir", "output_dir", "working_dir")
MANAGED_PATH_VALUES = {
    "params_dir": "managed_params",
    "output_dir": "managed_output",
    "working_dir": "managed_working",
}


DEFAULT_FORMAL_CONFIGS: dict[str, dict[str, Any]] = {
    "Density": {
        "protocol": "Density",
        "params_dir": "managed_params",
        "output_dir": "managed_output",
        "working_dir": "managed_working",
        "temperature": 298,
        "natoms": 10000,
        "components": {
            "DMC": 249,
            "EC": 170,
            "LI": 34,
            "PF6": 34,
        },
        "smiles": {
            "DMC": "COC(=O)OC",
            "EC": "O=C1OCCO1",
            "LI": "[Li+]",
            "PF6": "F[P-](F)(F)(F)(F)F",
        },
    },
    "Transport": {
        "protocol": "Transport",
        "params_dir": "managed_params",
        "output_dir": "managed_output",
        "working_dir": "managed_working",
        "temperature": 298,
        "natoms": 10000,
        "components": {
            "DMC": 249,
            "EC": 170,
            "LI": 34,
            "PF6": 34,
        },
        "smiles": {
            "DMC": "COC(=O)OC",
            "EC": "O=C1OCCO1",
            "LI": "[Li+]",
            "PF6": "F[P-](F)(F)(F)(F)F",
        },
    },
    "HVap": {
        "protocol": "HVap",
        "params_dir": "managed_params",
        "output_dir": "managed_output",
        "working_dir": "managed_working",
        "temperature": 298,
        "natoms": 5000,
        "components": {"ACT": 1},
        "smiles": {"ACT": "CC(C)=O"},
    },
    "Dielectric": {
        "protocol": "Dielectric",
        "params_dir": "managed_params",
        "output_dir": "managed_output",
        "working_dir": "managed_working",
        "temperature": 298,
        "natoms": 5000,
        "npt_steps": 2000000,
        "nvt_steps": 8000000,
        "dipole_interval": 1000,
        "components": {"DMC": 1},
        "smiles": {"DMC": "COC(=O)OC"},
    },
    "Compressibility": {
        "protocol": "Compressibility",
        "params_dir": "managed_params",
        "output_dir": "managed_output",
        "working_dir": "managed_working",
        "temperature": 298,
        "natoms": 5000,
        "npt_steps": 5000000,
        "components": {"DMC": 1},
        "smiles": {"DMC": "COC(=O)OC"},
    },
}


REQUIRED_RESULT_FILES = {
    "Density": "density_results.json",
    "Transport": "results.json",
    "HVap": "hvap_results.json",
    "Dielectric": "dielectric_results.json",
    "Compressibility": "compressibility_results.json",
}


def formal_protocol_metadata() -> list[dict[str, Any]]:
    return [
        {
            "protocol": protocol,
            "run_mode": "formal",
            "default_config": copy.deepcopy(DEFAULT_FORMAL_CONFIGS[protocol]),
            "required_result_file": REQUIRED_RESULT_FILES[protocol],
        }
        for protocol in FORMAL_PROTOCOLS
    ]


def _positive_int_field(config: dict[str, Any], field: str, default: int) -> int:
    value = config.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"config_json.{field} must be a positive integer")
    return value


def validate_formal_config(config: dict[str, Any], expected_protocol: str) -> dict[str, Any]:
    if expected_protocol not in FORMAL_PROTOCOLS:
        raise ValueError(f"unsupported formal ByteFF2 protocol: {expected_protocol}")
    if not isinstance(config, dict):
        raise ValueError("config_json must be an object")
    protocol = config.get("protocol")
    if protocol != expected_protocol:
        raise ValueError("config_json.protocol must match the selected protocol")
    components = config.get("components")
    smiles = config.get("smiles")
    if not isinstance(components, dict) or not components:
        raise ValueError("config_json.components must be a non-empty object")
    if not isinstance(smiles, dict) or not smiles:
        raise ValueError("config_json.smiles must be a non-empty object")
    if set(components) != set(smiles):
        raise ValueError("config_json.components and config_json.smiles must have the same keys")

    for name, ratio in components.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("component names must be non-empty strings")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or ratio <= 0:
            raise ValueError(f"component ratio for {name} must be a positive number")
    for name, value in smiles.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"SMILES for {name} must be a non-empty string")
        if "*" in value:
            raise ValueError("formal ByteFF2 SMILES must not contain polymer attachment points")

    temperature = config.get("temperature")
    natoms = config.get("natoms")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or temperature <= 0:
        raise ValueError("config_json.temperature must be a positive number")
    if isinstance(natoms, bool) or not isinstance(natoms, int) or natoms <= 0:
        raise ValueError("config_json.natoms must be a positive integer")
    if natoms > 10_000:
        raise ValueError("config_json.natoms must be <= 10000")
    if expected_protocol == "HVap" and len(components) != 1:
        raise ValueError("HVap formal protocol only supports one component")
    if expected_protocol == "Dielectric":
        _positive_int_field(config, "npt_steps", 2000000)
        _positive_int_field(config, "nvt_steps", 6000000)
        _positive_int_field(config, "dipole_interval", 500)
    if expected_protocol == "Compressibility":
        npt_steps = _positive_int_field(config, "npt_steps", 5000000)
        if npt_steps <= 1000000:
            raise ValueError("Compressibility npt_steps must be greater than 1000000")

    sanitized = copy.deepcopy(config)
    for field in PATH_FIELDS:
        sanitized[field] = MANAGED_PATH_VALUES[field]
    return sanitized


def estimate_requested_steps(protocol: str, config: dict[str, Any]) -> int:
    if protocol == "Density":
        return 1500000
    if protocol == "Transport":
        return 15000000
    if protocol == "HVap":
        return 6500000
    if protocol == "Dielectric":
        return _positive_int_field(config, "npt_steps", 2000000) + _positive_int_field(config, "nvt_steps", 6000000)
    if protocol == "Compressibility":
        return _positive_int_field(config, "npt_steps", 5000000)
    return _positive_int_field(config, "steps", 1000)

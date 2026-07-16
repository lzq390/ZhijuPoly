from __future__ import annotations

import copy
from typing import Any


DEMO_PROTOCOL = "DensityDemo"
FORMAL_PROTOCOLS = ("Density", "Transport", "HVap", "Dielectric", "Compressibility")
PATH_FIELDS = ("params_dir", "output_dir", "working_dir")

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


def sanitize_formal_config(config: dict[str, Any], protocol: str, job_root: str) -> dict[str, Any]:
    if protocol not in FORMAL_PROTOCOLS:
        raise ValueError(f"unsupported formal ByteFF2 protocol: {protocol}")
    if config.get("protocol") != protocol:
        raise ValueError("config_json.protocol must match the selected protocol")
    natoms = config.get("natoms")
    if isinstance(natoms, bool) or not isinstance(natoms, int) or natoms <= 0:
        raise ValueError("config_json.natoms must be a positive integer")
    if natoms > 10_000:
        raise ValueError("config_json.natoms must be <= 10000")
    sanitized = copy.deepcopy(config)
    sanitized["params_dir"] = f"{job_root}/params"
    sanitized["output_dir"] = f"{job_root}/outputs"
    sanitized["working_dir"] = f"{job_root}/working"
    return sanitized


def required_result_file(protocol: str) -> str:
    try:
        return REQUIRED_RESULT_FILES[protocol]
    except KeyError as exc:
        raise ValueError(f"unsupported formal ByteFF2 protocol: {protocol}") from exc


def protocol_default_config(protocol: str) -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_FORMAL_CONFIGS[protocol])


def estimate_requested_steps(protocol: str, config: dict[str, Any]) -> int:
    if protocol == "Density":
        return 1500000
    if protocol == "Transport":
        return 15000000
    if protocol == "HVap":
        return 6500000
    if protocol == "Dielectric":
        return int(config.get("npt_steps", 2000000)) + int(config.get("nvt_steps", 6000000))
    if protocol == "Compressibility":
        return int(config.get("npt_steps", 5000000))
    return int(config.get("steps", 1000))

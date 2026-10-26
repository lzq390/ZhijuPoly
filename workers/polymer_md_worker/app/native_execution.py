"""Explicit RadonPy call bindings; never reinterpret an unknown user selection."""
from __future__ import annotations

import importlib
import math
from copy import deepcopy
from numbers import Real
from typing import Any

DEFAULT_METHODS = {
    "opt_method": "wb97m-d3bj", "opt_basis": "6-31G(d,p)", "charge": "RESP",
    "charge_method": "HF", "charge_basis": "6-31G(d)",
    "sp_method": "wb97m-d3bj", "sp_basis": "6-311G(d,p)",
    "polar_method": "wb97m-d3bj", "polar_basis": "6-311+G(2d,p)",
}
FORCEFIELDS = {"GAFF": ("gaff", "GAFF"), "GAFF2": ("gaff2", "GAFF2"),
              "GAFF2_mod": ("gaff2_mod", "GAFF2_mod"), "Dreiding": ("dreiding", "Dreiding"),
              "Dreiding_UT": ("dreiding", "Dreiding_UT")}
QM_UNITS = {"qm_total_energy": "kJ/mol", "qm_homo": "eV", "qm_lumo": "eV",
            "qm_dipole": "D", "qm_polarizability": "angstrom^3"}
NATIVE_PROPERTIES = frozenset({"density", "Rg", "self-diffusion", "Cp", "Cv", "compressibility",
    "isentropic_compressibility", "bulk_modulus", "isentropic_bulk_modulus", "volume_expansion",
    "linear_expansion", "r2", "static_dielectric_const", "nematic_order_parameter",
    "refractive_index", "thermal_conductivity", "thermal_diffusivity", "tg", "sp_total", "sp_ced", *QM_UNITS})
METHODS = frozenset({"HF", "wb97m-d3bj"})
BASES = frozenset({"6-31G(d)", "6-31G(d,p)", "6-311G(d,p)", "6-311+G(2d,p)"})
CHARGES = frozenset({"RESP", "ESP", "Mulliken", "Lowdin", "gasteiger", "zero"})
PROPERTY_UNITS = {"density": "g/cm3", "Rg": "A", "self-diffusion": "m2/s", "Cp": "J/(kg K)", "Cv": "J/(kg K)",
    "compressibility": "1/Pa", "isentropic_compressibility": "1/Pa", "bulk_modulus": "Pa", "isentropic_bulk_modulus": "Pa",
    "volume_expansion": "1/K", "linear_expansion": "1/K", "r2": "nm2", "static_dielectric_const": "1", "nematic_order_parameter": "1",
    "refractive_index": "1", "thermal_conductivity": "W/(m K)", "thermal_diffusivity": "m2/s", "tg": "K",
    "sp_total": "MPa^0.5", "sp_ced": "J/cm3", **QM_UNITS}
BASE_PROPERTIES = frozenset(PROPERTY_UNITS) - {*QM_UNITS, "refractive_index", "thermal_conductivity", "thermal_diffusivity", "tg", "sp_total", "sp_ced"}


def configuration(request: dict) -> dict:
    value = request.get("native_configuration")
    if value is None:
        return dict(DEFAULT_METHODS)
    # The scientific prefix intentionally has Pydantic 1; HTTP contracts use
    # Pydantic 2 in the separate Worker prefix. Keep this boundary JSON-only.
    if not isinstance(value, dict) or set(value) != {*DEFAULT_METHODS, "properties"}:
        raise ValueError("native configuration requires the exact scientific fields")
    for key in DEFAULT_METHODS:
        allowed = CHARGES if key == "charge" else BASES if key.endswith("basis") else METHODS
        if not isinstance(value[key], str) or value[key] not in allowed:
            raise ValueError("unsupported native parameter: " + key)
    properties = value["properties"]
    if (not isinstance(properties, list) or not properties or len(properties) > len(NATIVE_PROPERTIES)
            or any(not isinstance(key, str) or key not in NATIVE_PROPERTIES for key in properties)
            or len(properties) != len(set(properties))):
        raise ValueError("unsupported native property selection")
    return {**{key: value[key] for key in DEFAULT_METHODS}, "properties": sorted(properties)}


def selected_properties(request: dict) -> frozenset[str] | None:
    value = request.get("native_configuration")
    return frozenset(configuration(request)["properties"]) if value is not None else None


def molecular_only(request: dict) -> bool:
    selected = selected_properties(request)
    return request.get("model_configuration") is None and bool(selected) and selected <= QM_UNITS.keys()


def make_forcefield(request: dict):
    name = request.get("forcefield", "GAFF2_mod")
    if name not in FORCEFIELDS:
        raise ValueError("unsupported native RadonPy forcefield")
    module, symbol = FORCEFIELDS[name]
    return getattr(importlib.import_module("radonpy.ff." + module), symbol)()


def terminal_method(config: dict) -> str:
    if config["charge"] in {"gasteiger", "zero"}:
        return "RadonPy.assign_charges(" + config["charge"] + ");terminal_QM_optimization_not_applicable"
    return (config["opt_method"] + "/" + config["opt_basis"] + "; " + config["charge"]
            + " " + config["charge_method"] + "/" + config["charge_basis"])


def cache_compatible(config: dict) -> bool:
    return all(config[key] == DEFAULT_METHODS[key]
               for key in ("opt_method", "opt_basis", "charge", "charge_method", "charge_basis"))


def basis_kwargs(request: dict, step: str) -> dict:
    if request.get("native_configuration") is None:
        return {}
    return {"basis_gen": {"Br": "6-31G(d,p)" if step == "optimization" else "6-311G(d,p)", "I": "lanl2dz"}}


def parameter_applicability(request: dict) -> dict:
    config = configuration(request)
    selected = selected_properties(request)
    single = selected is None or bool(selected & {"qm_total_energy", "qm_homo", "qm_lumo", "qm_dipole"})
    polar = selected is None or bool(selected & {"qm_polarizability", "refractive_index"})
    quantum_charges = config["charge"] not in {"gasteiger", "zero"}
    evidence = {}
    for key in DEFAULT_METHODS:
        applicable = (quantum_charges if key.startswith("charge_") else single if key.startswith("sp_")
                      else polar if key.startswith("polar_") else True)
        evidence[key] = {"value": config[key], "applicable": applicable,
            "reason": None if applicable else "charge_model_does_not_use_QM_method_or_basis" if key.startswith("charge_") else "dependent_molecular_calculation_not_requested"}
    evidence["terminal_optimization"] = {"applicable": quantum_charges,
        "reason": None if quantum_charges else "native_classical_charge_assignment_skips_terminal_QM_optimization"}
    return evidence


def property_record(key: str, value: Any, unit: str, method: str, stage: str,
                    window: dict[str, Any], uncertainty: float | None = None,
                    quality: str = "passed", reason: str | None = None,
                    conditions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Format measured or unavailable records in the HTTP-only worker prefix.

    NumPy real scalars implement Real; NumPy booleans do not. Keep scientific
    number validation here without importing the optional analysis packages.
    """
    finite = not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(float(value))
    valid_error = (not isinstance(uncertainty, bool) and isinstance(uncertainty, Real)
                   and math.isfinite(float(uncertainty)) and uncertainty >= 0)
    return {"key": key, "value": float(value) if finite else None, "unit": unit,
            "method": method, "conditions": conditions or {}, "source_stage": stage,
            "analysis_range": window, "uncertainty": float(uncertainty) if valid_error else None,
            "quality_status": quality if finite else "failed", "reason": reason if finite else reason or "non_finite_or_missing"}


def complete_requested_results(request: dict, result: dict, *, stage: str | None = None,
                               reason: str = "selected_property_not_produced") -> dict:
    """Fill missing selected records, never overwrite measurements from a prior stage.

    stage=None is for the manager's final summary (including failed/skipped
    branches). Stage-scoped calls do not prematurely publish future properties.
    """
    selected = selected_properties(request)
    if selected is None:
        selected = BASE_PROPERTIES if stage and stage.startswith("analyze_") else frozenset()
    if stage is not None:
        if stage.startswith("analyze_"):
            owned = BASE_PROPERTIES | {"refractive_index"} | QM_UNITS.keys()
        else:
            owned = {"monomer_qm": set(QM_UNITS), "tc_analyze": {"thermal_conductivity", "thermal_diffusivity"},
                     "sp_analyze": {"sp_total", "sp_ced"}, "tg_analyze": {"tg"}}.get(stage, set())
        selected = selected & owned
    output = deepcopy(result)
    records = {item["key"]: item for item in output.get("properties", []) if isinstance(item, dict) and isinstance(item.get("key"), str)}
    for key in sorted(selected - records.keys()):
        group = ("thermal" if key in {"thermal_conductivity", "thermal_diffusivity"} else "sp" if key in {"sp_total", "sp_ced"}
                 else "glass_transition" if key == "tg" else "molecular" if key in QM_UNITS else "basic")
        quality = output.get("quality_summary", {}).get(group, {})
        group_reason = quality.get("reason") if isinstance(quality, dict) else None
        if not group_reason and isinstance(quality, dict) and isinstance(quality.get("reasons"), list):
            group_reason = ";".join(item for item in quality["reasons"] if isinstance(item, str))
        actual_reason = group_reason if isinstance(group_reason, str) and group_reason else reason
        # Never lend an equilibrium window to a skipped/failed TC, SP or Tg
        # branch. Only a measured record of the same analysis family is valid.
        family_keys = ({"thermal_conductivity", "thermal_diffusivity"} if group == "thermal" else {"sp_total", "sp_ced"}
                       if group == "sp" else {"tg"} if group == "glass_transition" else set(QM_UNITS)
                       if group == "molecular" else BASE_PROPERTIES | {"refractive_index"})
        window = next((item["analysis_range"] for item_key, item in records.items()
                       if item_key in family_keys and item.get("method") != "selected_property_unavailable"
                       and isinstance(item.get("analysis_range"), dict)),
                      {"start_ps": None, "stop_ps": None, "sample_count": 0})
        source = ("monomer_qm" if key in QM_UNITS else "tc" if key in {"thermal_conductivity", "thermal_diffusivity"}
                  else "sp" if key in {"sp_total", "sp_ced"} else "tg" if key == "tg"
                  else "eq3" if stage == "analyze_0" else "additional_" + stage.rsplit("_", 1)[1] if stage and stage.startswith("analyze_") else "not_executed")
        records[key] = property_record(key, None, PROPERTY_UNITS[key], "selected_property_unavailable", source,
            window, reason=actual_reason, conditions={"requested": True, "availability_stage": stage or "terminal_summary"})
    output["properties"] = [records[key] for key in sorted(records)]
    output["parameter_applicability"] = parameter_applicability(request)
    if output.get("quality_status") == "passed" and any(item.get("quality_status") != "passed" for item in records.values()):
        output["quality_status"] = "warning"
    return output


def qm_records(request: dict, qm: dict) -> list[dict]:
    selected = selected_properties(request)
    if selected is None:
        return []
    config = configuration(request)
    records = []
    for key in sorted(selected & QM_UNITS.keys()):
        method = (config["polar_method"] + "/" + config["polar_basis"]
                  if key == "qm_polarizability" else config["sp_method"] + "/" + config["sp_basis"])
        conditions = {"scope": "monomer", "method_basis": method,
                      **basis_kwargs(request, "polarizability" if key == "qm_polarizability" else "single_point"),
                      "native_function": "polar" if key == "qm_polarizability" else "energy/homo/lumo/dipole"}
        reason = None
        if isinstance(qm.get("components"), list):
            conditions["components"] = [{"id": component["id"], "smiles": component["smiles"],
                                          "value": component.get(key), "unit": QM_UNITS[key]}
                                         for component in qm["components"]]
            conditions["aggregation"] = "individual_molecular_outputs;no_bulk_scalar_defined"
            reason = "multicomponent_molecular_values_are_reported_per_component"
        records.append(property_record(key, qm.get(key), QM_UNITS[key], "RadonPy.QMw." + method,
            "monomer_qm", {"sample_count": 1, "start_ps": None, "stop_ps": None},
            conditions=conditions, reason=reason))
    return records


def retain_requested(request: dict, result: dict, qm: dict | None = None) -> dict:
    selected = selected_properties(request)
    if selected is None:
        return result
    output = deepcopy(result)
    records = {item["key"]: item for item in output.get("properties", []) if item.get("key") in selected}
    if qm is not None:
        records.update({item["key"]: item for item in qm_records(request, qm)})
    output["properties"] = [records[key] for key in sorted(records)]
    output["requested_properties"] = sorted(selected)
    output["effective_native_configuration"] = configuration(request)
    output["forcefield"] = request.get("forcefield", "GAFF2_mod")
    return output

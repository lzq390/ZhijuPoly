"""Real, restartable RadonPy stages. Runs only inside a Worker-owned CPU scope.

The immutable checkpoint embeds the complete RDKit/RadonPy molecular state.
No stage reads a guessed latest file or substitutes demonstration observations.
"""
from __future__ import annotations

import argparse
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import threading
import time
from typing import Any

from .runtime import probe_runtime, sha256
from .native_execution import (configuration, selected_properties, make_forcefield, cache_compatible,
                               terminal_method, qm_records, retain_requested, basis_kwargs,
                               parameter_applicability, complete_requested_results)
from .model_execution import model_configuration, components as model_components, construct_model, native_proximity_guard
from .generated_input import forcefield_snapshot, validate_input, equilibrium_schedule

TERMINALS = {"CH3": "*C", "H": "*[H]", "OH": "*O"}
PREDECESSOR = {"monomer_qm": "terminal_qm", "build": "monomer_qm", "eq1": "build", "eq2": "eq1", "eq3": "eq2",
               "analyze_0": "eq3", "additional_1": "analyze_0", "analyze_1": "additional_1",
               "additional_2": "analyze_1", "analyze_2": "additional_2", "tc_analyze": "tc",
               "sp_analyze": "sp", "tg_analyze": "tg"}


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as output:
        json.dump(value, output, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def assert_finite(values, label: str):
    import numpy as np
    if not np.isfinite(np.asarray(values, dtype=float)).all():
        raise RuntimeError(label + ": non-finite result")


def seed_engines(seed: int):
    """RadonPy does not forward its kwargs seed to ETKDG; set it explicitly."""
    import numpy as np
    from rdkit import rdBase
    from rdkit.Chem import AllChem
    random.seed(seed)
    np.random.seed(seed)
    rdBase.SeedRandomNumberGenerator(seed)
    for name in ("ETKDG", "ETKDGv2", "ETKDGv3"):
        original = getattr(AllChem, name)
        def factory(*args, _original=original, **kwargs):
            parameters = _original(*args, **kwargs)
            parameters.randomSeed = seed
            return parameters
        setattr(AllChem, name, factory)


def mpi_processes(atom_count: int) -> int:
    """Server-managed ranks: avoid spending the budget on tiny-cell exchanges."""
    return min(16, max(1, math.ceil(atom_count / 500)))


def _finite_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _positive_number(value) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _dependency_errors(key: str, record: dict) -> list[str]:
    if not isinstance(record, dict) or not record:
        return [key + "_missing"]
    reasons = []
    if record.get("quality_status") != "passed":
        reasons.append(key + "_quality_not_passed")
    if _positive_number(record.get("value")) is None:
        reasons.append(key + "_non_finite_or_non_positive")
    expected_unit = {"density": "g/cm3", "Cp": "J/(kg K)", "static_dielectric_const": "1",
                     "thermal_conductivity": "W/(m K)"}.get(key)
    if expected_unit is not None and record.get("unit") != expected_unit:
        reasons.append(key + "_unit_mismatch")
    return reasons


def _dependency_context(record: dict) -> dict:
    """Keep the scientific source, without copying arbitrary private metadata."""
    record = record if isinstance(record, dict) else {}
    context = {key: record[key] for key in ("key", "unit", "method", "source_stage", "quality_status", "reason")
               if isinstance(record.get(key), str)}
    context["value"] = _finite_number(record.get("value"))
    error = _finite_number(record.get("uncertainty"))
    context["uncertainty"] = error if error is not None and error >= 0 else None
    window = record.get("analysis_range")
    if isinstance(window, dict):
        context["analysis_range"] = {key: _finite_number(window.get(key)) for key in ("start_ps", "stop_ps")}
    conditions = record.get("conditions")
    if isinstance(conditions, dict):
        measured = {key: _finite_number(conditions[key]) for key in ("temperature_k", "pressure_atm", "time_step_fs") if key in conditions}
        if conditions.get("ensemble") in {"NPT", "NVT", "NVE"}:
            measured["ensemble"] = conditions["ensemble"]
        if conditions.get("axis") in {"x", "y", "z"}:
            measured["axis"] = conditions["axis"]
        if isinstance(conditions.get("decomp"), bool):
            measured["decomp"] = conditions["decomp"]
        context["conditions"] = measured
    return context


def _derived_record(key: str, value, unit: str, method: str, source: str,
                    window: dict, reason: str, conditions: dict) -> dict:
    number = _positive_number(value)
    return {"key": key, "value": number, "unit": unit, "method": method,
            "conditions": conditions, "source_stage": source, "analysis_range": dict(window),
            "uncertainty": None, "quality_status": "warning" if number is not None else "failed", "reason": reason}


def derive_optical_properties(properties: dict, qm: dict, source: str, window: dict,
                              *, method_basis="wb97m-d3bj/6-311+G(2d,p)", native=False) -> list[dict]:
    """Always return both optional records; never propagate an unqualified input."""
    density = properties.get("density", {})
    dielectric = properties.get("static_dielectric_const", {})
    qm = qm if isinstance(qm, dict) else {}
    alpha = _positive_number(qm.get("qm_polarizability"))
    mass = _positive_number(qm.get("mol_weight"))
    reasons = _dependency_errors("density", density)
    component_inputs = None
    if isinstance(qm.get("components"), list):
        component_inputs = []
        for component in qm["components"]:
            count = _finite_number(component.get("repeat_unit_count"))
            component_alpha = _positive_number(component.get("qm_polarizability"))
            component_mass = _positive_number(component.get("mol_weight"))
            if count is None or count < 0 or int(count) != count or component_alpha is None or component_mass is None:
                reasons.append("component_QM_or_actual_composition_missing_or_invalid")
            component_inputs.append({"id": component.get("id"), "repeat_unit_count": count,
                                     "polarizability_angstrom3": component_alpha, "mol_weight_g_mol": component_mass})
        total = sum(item["repeat_unit_count"] or 0 for item in component_inputs)
        if not component_inputs or total <= 0:
            reasons.append("actual_repeat_unit_composition_missing_or_empty")
        if not reasons:
            for item in component_inputs:
                item["repeat_unit_mole_fraction"] = item["repeat_unit_count"] / total
            alpha = sum(item["polarizability_angstrom3"] * item["repeat_unit_mole_fraction"] for item in component_inputs)
            mass = sum(item["mol_weight_g_mol"] * item["repeat_unit_mole_fraction"] for item in component_inputs)
    if alpha is None:
        reasons.append("qm_polarizability_missing_non_finite_or_non_positive")
    if mass is None:
        reasons.append("qm_mol_weight_missing_non_finite_or_non_positive")
    conditions = {"density": _dependency_context(density),
                  "qm": {"polarizability_angstrom3": alpha, "mol_weight_g_mol": mass,
                         "polarizability_method": method_basis,
                         "mol_weight_method": "RadonPy_molecular_weight"},
                  "uncertainty_model": "not_propagated_from_QM_and_MD_dependencies"}
    if component_inputs is not None:
        conditions["qm"]["components"] = component_inputs
        conditions["qm"]["composition_source"] = "actual_constructed_repeat_unit_counts"
        conditions["qm"]["mixing_model"] = "repeat_unit_additive_static_Lorentz_Lorenz"
    phi, refractive = None, None
    if not reasons:
        phi = 4 * math.pi * density["value"] * 6.02214076e23 * alpha * 1e-24 / (3 * mass)
        if not math.isfinite(phi) or not 0 < phi < 1:
            reasons.append("lorentz_lorenz_phi_outside_open_interval_0_1")
        else:
            if native:
                from radonpy.core import calc
                if component_inputs is None:
                    refractive = float(calc.refractive_index(alpha, density["value"], mass))
                else:
                    import numpy as np
                    refractive = float(calc.refractive_index([item["polarizability_angstrom3"] for item in component_inputs],
                        density["value"], [item["mol_weight_g_mol"] for item in component_inputs],
                        ratio=np.asarray([item["repeat_unit_mole_fraction"] for item in component_inputs])))
            else:
                refractive = math.sqrt((1 + 2 * phi) / (1 - phi))
            if _positive_number(refractive) is None:
                reasons.append("refractive_index_non_finite_or_non_positive")
                refractive = None
    conditions["lorentz_lorenz_phi"] = _finite_number(phi)
    ri = _derived_record("refractive_index", refractive, "1", "radonpy.core.calc.refractive_index" if native else "static_Lorentz_Lorenz_QM_polarizability",
        source, window, ";".join(reasons) if reasons else "static_electronic_approximation;uncertainty_not_propagated", conditions)
    corrected_reasons = _dependency_errors("static_dielectric_const", dielectric)
    if refractive is None:
        corrected_reasons.extend(["refractive_index_unavailable", *reasons])
    corrected = None
    if not corrected_reasons:
        corrected = dielectric["value"] + refractive**2 - 1
        if _positive_number(corrected) is None:
            corrected_reasons.append("electronic_corrected_dielectric_non_finite_or_non_positive")
    corrected_conditions = {**conditions, "static_dielectric_const": _dependency_context(dielectric),
                            "refractive_index": _dependency_context(ri)}
    electronic = _derived_record("electronic_corrected_dielectric", corrected, "1",
        "epsilon_fixed_charge_plus_n_squared_minus_1", source, window,
        ";".join(corrected_reasons) if corrected_reasons else "electronic_correction_approximation;uncertainty_not_propagated",
        corrected_conditions)
    return [ri, electronic]


def derive_thermal_diffusivity(thermal: dict, basic: dict, source: str, window: dict, *, native=False) -> dict:
    """An algebraic value is available only when every measured dependency passed."""
    dependencies = {"thermal_conductivity": thermal.get("thermal_conductivity", {}),
                    "density": basic.get("density", {}), "Cp": basic.get("Cp", {})}
    reasons = [reason for key, value in dependencies.items() for reason in _dependency_errors(key, value)]
    value = None
    if not reasons:
        denominator = dependencies["density"]["value"] * 1000 * dependencies["Cp"]["value"]
        if _positive_number(denominator) is None:
            reasons.append("density_Cp_denominator_non_finite_or_non_positive")
        else:
            if native:
                from radonpy.core import calc
                value = float(calc.thermal_diffusivity(dependencies["thermal_conductivity"]["value"],
                                                     dependencies["density"]["value"], dependencies["Cp"]["value"]))
            else:
                value = dependencies["thermal_conductivity"]["value"] / denominator
            if _positive_number(value) is None:
                reasons.append("thermal_diffusivity_non_finite_or_non_positive")
    conditions = {"dependencies": {key: _dependency_context(record) for key, record in dependencies.items()},
                  "density_conversion_kg_m3_per_g_cm3": 1000,
                  "uncertainty_model": "not_propagated_from_kappa_density_Cp;cross_stage_covariances_not_estimated"}
    return _derived_record("thermal_diffusivity", value, "m2/s", "radonpy.core.calc.thermal_diffusivity" if native else "kappa_over_density_Cp", source, window,
        ";".join(reasons) if reasons else "derived_estimate;uncertainty_not_propagated_from_kappa_density_Cp", conditions)


def normalize_rnemd_input(text: str) -> str:
    """Patch only the two frozen output guards for LAMMPS 2024 compatibility.

    Output policy is independently checked by validate_rnemd_input: dumpf=None,
    xtcf enabled. LAMMPS string Boolean operands cannot use ordinary filenames.
    Keep the guarded output commands and every scientific parameter unchanged.
    """
    for name, enabled in (("dumpf", "0"), ("xtcf", "1")):
        legacy, normalized = '"${' + name + '} != None"', '"' + enabled + '"'
        pattern = re.compile(r"(?m)^([ \t]*if[ \t]+)(" + re.escape(legacy) + "|" + re.escape(normalized)
                             + r")([ \t]+then(?:[ \t]+&)?[ \t]*)$")
        matches = list(pattern.finditer(text))
        # A numeric guard is identifiable by its fixed following output command,
        # so unrelated if "0"/"1" blocks must never be rewritten or counted.
        expected_command = r'dump\s+' + ("1 all custom" if name == "dumpf" else "2 all xtc")
        selected = [match for match in matches if re.match(r'\s*"?' + expected_command + r"\b", text[match.end():])]
        if len(selected) != 1:
            raise RuntimeError("RNEMD missing, ambiguous or unsupported output guard: " + name)
        match = selected[0]
        text = text[:match.start()] + match[1] + normalized + match[3] + text[match.end():]
    return text


def validate_rnemd_input(text: str, request: dict) -> dict:
    """Parse the frozen template without evaluating arbitrary LAMMPS expressions."""
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().removesuffix("&").strip()
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1].strip()
        if line:
            lines.append(" ".join(line.split()))
    if any(line.startswith("if ") and ("${dumpf}" in line or "${xtcf}" in line) for line in lines):
        raise RuntimeError("RNEMD output guards were not normalized to fixed Booleans")
    names = {"axis", "rep", "repo", "TimeSt", "NStep", "Ttemp", "slab", "exchg", "Nevery", "seed",
             "Nfreq", "invslab", "xtcf", "dumpf"}
    variables = {}
    for line in lines:
        match = re.fullmatch(r"variable (\w+) (equal|string) (.+)", line)
        if match and match[1] in names:
            if match[1] in variables:
                raise RuntimeError("RNEMD duplicate controlled variable: " + match[1])
            variables[match[1]] = (match[2], match[3])
    if set(variables) != names:
        raise RuntimeError("RNEMD missing controlled input variables")
    if variables["axis"] != ("string", request.get("tc_axis")) or request.get("tc_axis") not in {"x", "y", "z"}:
        raise RuntimeError("RNEMD axis differs from requested direction")
    numeric = {}
    for name in ("rep", "repo", "TimeSt", "NStep", "Ttemp", "slab", "exchg", "Nevery", "seed"):
        style, value = variables[name]
        if style != "equal" or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
            raise RuntimeError("RNEMD controlled value must be a numeric literal: " + name)
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise RuntimeError("RNEMD controlled value is not finite/positive: " + name)
        numeric[name] = parsed
    duration, temperature = _positive_number(request.get("tc_ns")), _positive_number(request.get("temp"))
    replication = request.get("tc_rep")
    if (duration is None or not .01 <= duration <= 10 or temperature is None or not 100 <= temperature <= 1000
            or isinstance(replication, bool) or not isinstance(replication, int) or not 1 <= replication <= 5):
        raise RuntimeError("RNEMD request lacks finite positive controls")
    expected = {"rep": request.get("tc_rep"), "repo": 1, "TimeSt": .2,
                "NStep": round(duration * 1000000 / .2), "Ttemp": temperature,
                "slab": 20, "exchg": 1000, "Nevery": 1}
    for name, value in expected.items():
        tolerance = 5e-7 if name == "Ttemp" else 1e-12
        if not isinstance(value, (int, float)) or not math.isclose(numeric[name], value, rel_tol=0, abs_tol=tolerance):
            raise RuntimeError("RNEMD generated parameter differs from locked request/protocol: " + name)
    for name in ("rep", "repo", "NStep", "slab", "exchg", "Nevery", "seed"):
        if not numeric[name].is_integer():
            raise RuntimeError("RNEMD integer control is fractional: " + name)
    if not 1000 <= numeric["seed"] < 999999:
        raise RuntimeError("RNEMD generated velocity seed differs from frozen seed policy")
    required = {"units real", "timestep ${TimeSt}", "reset_timestep 0",
                "fix mp all thermal/conductivity ${exchg} ${axis} ${slab}",
                "compute layers all chunk/atom bin/1d ${axis} lower ${invslab} units reduced",
                "fix 2 all ave/chunk ${Nevery} ${Nfreq} ${exchg} layers temp density/mass file ${Tprof} norm sample",
                "dump 2 all xtc 1000 ${xtcf}", "dump_modify 2 unwrap yes",
                "velocity all create ${Ttemp} ${seed} mom yes rot yes dist gaussian"}
    if not required.issubset(lines) or variables["Nfreq"] != ("equal", "${exchg}/${Nevery}") or variables["invslab"] != ("equal", "1/${slab}"):
        raise RuntimeError("RNEMD exchange/profile/trajectory commands differ from locked template")
    if variables["dumpf"] != ("string", "None") or variables["xtcf"][0] != "string" or variables["xtcf"][1] == "None":
        raise RuntimeError("RNEMD requires XTC output and disabled bulk ASCII dump")
    guarded_outputs = [('if "0" then', "dump 1 all custom 1000 ${dumpf} id type mol xs ys zs ix iy iz"),
                       ('if "1" then', "dump 2 all xtc 1000 ${xtcf}")]
    if not all(any(line == guard and i + 1 < len(lines) and lines[i + 1] == command for i, line in enumerate(lines))
               for guard, command in guarded_outputs):
        raise RuntimeError("RNEMD ASCII/XTC output condition differs from frozen template")
    if any("heat/flux" in line for line in lines):
        raise RuntimeError("RNEMD decomposition is forbidden in this protocol")
    runs = [line for line in lines if line.startswith("run ")]
    if runs != ["run 10000", "run ${NStep}"]:
        raise RuntimeError("RNEMD preheat/production runs differ from locked protocol")
    if [line for line in lines if line.startswith("units ")] != ["units real"] or [line for line in lines if line.startswith("timestep ")] != ["timestep ${TimeSt}"]:
        raise RuntimeError("RNEMD units/timestep were overridden")
    unique_commands = {"reset_timestep ": "reset_timestep 0",
                       "fix mp ": "fix mp all thermal/conductivity ${exchg} ${axis} ${slab}",
                       "fix 2 ": "fix 2 all ave/chunk ${Nevery} ${Nfreq} ${exchg} layers temp density/mass file ${Tprof} norm sample",
                       "dump 1 ": "dump 1 all custom 1000 ${dumpf} id type mol xs ys zs ix iy iz"}
    if any([line for line in lines if line.startswith(prefix)] != [expected_line] for prefix, expected_line in unique_commands.items()):
        raise RuntimeError("RNEMD fixed exchange/profile/reset/guarded-output command was overridden")
    thermos = [line for line in lines if line.startswith("thermo ")]
    if thermos != ["thermo 1000", "thermo ${exchg}", "thermo ${exchg}"]:
        raise RuntimeError("RNEMD thermo cadence differs from locked protocol")
    if [line for line in lines if line.startswith("dump 2 ")] != ["dump 2 all xtc 1000 ${xtcf}"]:
        raise RuntimeError("RNEMD XTC output was overridden")
    if not any(re.fullmatch(r"fix NVT (all|nowater0) nvt temp \$\{Ttemp\} \$\{Ttemp\} 100", line) for line in lines):
        raise RuntimeError("RNEMD original NVT preheat thermostat is missing")
    replicas = {"x": "${rep} ${repo} ${repo}", "y": "${repo} ${rep} ${repo}", "z": "${repo} ${repo} ${rep}"}
    if len([line for line in lines if line.startswith("replicate ")]) != 3:
        raise RuntimeError("RNEMD replication commands differ from locked template")
    for axis, arguments in replicas.items():
        condition = ('if "${axis} == x" then' if axis == "x" else f'elif "${{axis}} == {axis}"')
        if not any(line == condition and i + 1 < len(lines) and lines[i + 1] == "replicate " + arguments for i, line in enumerate(lines)):
            raise RuntimeError("RNEMD replication axis mapping differs from locked template")
    return {"axis": variables["axis"][1], "rep": int(numeric["rep"]), "rep_other": int(numeric["repo"]),
            "time_step_fs": numeric["TimeSt"], "steps": int(numeric["NStep"]), "target_temperature_k": numeric["Ttemp"],
            "slab_count": int(numeric["slab"]), "exchange_steps": int(numeric["exchg"]),
            "exchange_interval_ps": numeric["exchg"] * numeric["TimeSt"] / 1000,
            "profile_nevery_steps": int(numeric["Nevery"]), "profile_nrepeat": int(numeric["exchg"] / numeric["Nevery"]),
            "profile_steps": int(numeric["exchg"]), "thermo_steps": int(numeric["exchg"]),
            "thermo_interval_ps": numeric["exchg"] * numeric["TimeSt"] / 1000,
            "xtc_steps": 1000, "xtc_interval_ps": 1000 * numeric["TimeSt"] / 1000,
            "preheat_steps": 10000, "preheat_ps": 10000 * numeric["TimeSt"] / 1000, "velocity_gaussian_seed": int(numeric["seed"]),
            "decomposition": False, "bulk_ascii_dump": False,
            "compatibility_patch": "fixed_output_boolean_guards",
            "sampling_policy": "locked_RNEMD_cadence;request_sample_interval_ps_applies_to_EQ_only"}


def load_state(stage: str, checkpoint: Path | None, checkpoint_sha256: str | None, request: dict) -> dict:
    request_digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if stage == "terminal_qm":
        return {"completed": [], "molecules": {}, "qm": {}, "stages": {}, "request_sha256": request_digest}
    if not checkpoint or not checkpoint_sha256:
        raise RuntimeError("a journal-selected immutable checkpoint and digest are required")
    if checkpoint.is_symlink() or sha256(checkpoint) != checkpoint_sha256:
        raise RuntimeError("unsafe checkpoint")
    state = json.loads(checkpoint.read_text())
    previous = state["completed"][-1]
    branches = {"analyze_0", "analyze_1", "analyze_2", "sp_analyze", "tc_analyze"}
    valid_predecessor = previous in branches if stage in {"tc", "sp", "tg"} else previous == PREDECESSOR[stage]
    if not valid_predecessor:
        raise RuntimeError("checkpoint stage identity mismatch")
    if state.get("request_sha256") != request_digest:
        raise RuntimeError("checkpoint belongs to a different scientific request")
    return state


def molecule(state, name):
    from radonpy.core import utils
    return utils.JSONToMol_dict(state["molecules"][name])


def store_molecule(state, name, mol):
    from radonpy.core import utils
    from rdkit import Chem
    if not isinstance(mol, Chem.Mol) or mol.GetNumAtoms() == 0 or mol.GetNumConformers() == 0:
        raise RuntimeError("engine did not return a molecular checkpoint")
    assert_finite(mol.GetConformer().GetPositions(), "molecular coordinates")
    state["molecules"][name] = utils.MolToJSON_dict(mol)


@contextmanager
def progress(work: Path, stage: str, log: Path, steps: int):
    stop = threading.Event()
    def update():
        while not stop.is_set():
            current, physical = 0, 0.0
            if log.is_file():
                # A bounded tail avoids repeatedly loading a many-gigabyte log.
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 32768))
                    for line in stream.read().decode(errors="replace").splitlines():
                        words = line.split()
                        if len(words) > 3 and words[0].isdigit():
                            try:
                                current, physical = int(words[0]), float(words[1]) * .001
                            except ValueError:
                                pass
            atomic_json(work / "progress.json", {"stage": stage, "current_step": current, "total_steps": steps, "time_ps": physical})
            stop.wait(2)
    thread = threading.Thread(target=update, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def qm_terminal(request: dict, state: dict, directory: Path, allow_cached: bool = True):
    from radonpy.core import utils
    from radonpy.sim import qm
    config = configuration(request)
    cache = probe_runtime().get("terminal_assets", {}) if allow_cached and cache_compatible(config) else {}
    provenance = {}
    for name in sorted({request["terminal_head"], request["terminal_tail"]}):
        asset = cache.get(name)
        if asset:
            path = Path(asset["path"])
            if path.is_symlink() or sha256(path) != asset["sha256"]:
                raise RuntimeError("built-in terminal asset integrity mismatch")
            record = json.loads(path.read_text())
            if record["method"] != "wb97m-d3bj/6-31G(d,p); RESP HF/6-31G(d)":
                raise RuntimeError("terminal method differs from locked protocol")
            state["molecules"]["terminal_" + name] = record["molecule"]
            provenance[name] = {"smiles": TERMINALS[name], "method": record["method"], "source": "read_only_builtin_cache", "sha256": asset["sha256"]}
            continue
        mol = utils.mol_from_smiles(TERMINALS[name])
        ok = qm.assign_charges(mol, charge=config["charge"], opt=True,
                               opt_method=config["opt_method"], opt_basis=config["opt_basis"],
                               charge_method=config["charge_method"], charge_basis=config["charge_basis"],
                               work_dir=str(directory), tmp_dir=str(directory),
                               log_name="terminal_" + name, omp=16, memory=16000,
                               total_charge=0, total_multiplicity=1)
        if ok is not True:
            raise RuntimeError("terminal QM/RESP did not converge: " + name)
        assert_finite([a.GetDoubleProp("AtomicCharge") for a in mol.GetAtoms()], "terminal RESP")
        store_molecule(state, "terminal_" + name, mol)
        provenance[name] = {"smiles": TERMINALS[name], "method": terminal_method(config), "source": "task_private_verified_QM"}
    return {"terminal_assets": provenance, "effective_native_configuration": config,
            "parameter_applicability": parameter_applicability(request)}


def _qm_monomer_single(request: dict, state: dict, directory: Path):
    import numpy as np
    from radonpy.core import utils, calc
    from radonpy.sim import qm
    from radonpy.sim.qm_wrapper import QMw
    from rdkit import Chem
    config = configuration(request)
    selected = selected_properties(request)
    mol = utils.mol_from_smiles(request["smiles"])
    if mol is None or any(a.GetNumRadicalElectrons() or a.GetFormalCharge() for a in mol.GetAtoms()):
        raise ValueError("only neutral closed-shell monomers are supported")
    # Preserve the locked MM search. Explicit serial DFT checks avoid upstream's
    # finite-energy fallback when every optimization failed to converge.
    mol, mm_energy = qm.conformation_search(mol, ff=make_forcefield(request), nconf=1000, dft_nconf=0,
        work_dir=str(directory), tmp_dir=str(directory), psi4_mp=0, mm_mp=0,
        etkdg_omp=16, psi4_omp=16, omp=0, mpi=mpi_processes(mol.GetNumAtoms()), gpu=0, memory=16000,
        log_name="monomer", solver_path=os.environ["LAMMPS_EXEC"], random_seed=request["seed"])
    if mol is None or not len(mm_energy):
        raise RuntimeError("conformation search failed")
    converged, evidence = [], []
    for conf in sorted(mol.GetConformers(), key=lambda item: item.GetId())[:4]:
        wrapper = QMw(mol, confId=conf.GetId(), work_dir=str(directory), tmp_dir=str(directory),
                      name=f"monomer_dft_{conf.GetId()}", method=config["opt_method"], basis=config["opt_basis"],
                      **basis_kwargs(request, "optimization"), omp=16, memory=16000)
        energy, coords = wrapper.optimize(ignore_conv_error=False, geom_iter=50, geom_conv="QCHEM", geom_algorithm="RFO")
        valid = bool(not wrapper.error_flag and math.isfinite(float(energy)) and np.isfinite(coords).all())
        evidence.append({"conformer": conf.GetId(), "converged": valid, "energy_kj_mol": float(energy) if math.isfinite(float(energy)) else None})
        if valid:
            converged.append((float(energy), utils.deepcopy_mol(wrapper.mol), conf.GetId()))
    if not converged:
        raise RuntimeError("no DFT conformer converged; refusing MM fallback")
    _, mol, cid = min(converged, key=lambda item: item[0])
    chosen = Chem.Conformer(mol.GetConformer(cid))
    mol.RemoveAllConformers()
    chosen.SetId(0)
    mol.AddConformer(chosen, assignId=True)
    if qm.assign_charges(mol, charge=config["charge"], opt=False,
                         opt_method=config["opt_method"], opt_basis=config["opt_basis"],
                         charge_method=config["charge_method"], charge_basis=config["charge_basis"],
                         work_dir=str(directory), tmp_dir=str(directory),
                         log_name="monomer_RESP", omp=16, memory=16000, total_charge=0, total_multiplicity=1) is not True:
        raise RuntimeError("monomer RESP did not converge")
    charges = [a.GetDoubleProp("AtomicCharge") for a in mol.GetAtoms()]
    assert_finite(charges, "monomer RESP")
    if abs(sum(charges)) > 1e-4:
        raise RuntimeError("monomer RESP charge is not neutral")
    props = {"mol_weight": calc.molecular_weight(mol)}
    if selected is None or selected & {"qm_total_energy", "qm_homo", "qm_lumo", "qm_dipole"}:
        wrapper = QMw(mol, work_dir=str(directory), tmp_dir=str(directory), name="monomer_sp",
                      method=config["sp_method"], basis=config["sp_basis"],
                      **basis_kwargs(request, "single_point"), omp=16, memory=16000)
        energy = wrapper.energy()
        if wrapper.error_flag:
            raise RuntimeError("monomer single point failed")
        props.update(qm_total_energy=energy, qm_homo=wrapper.homo, qm_lumo=wrapper.lumo,
                     qm_dipole=float(np.linalg.norm(wrapper.dipole)))
    if selected is None or selected & {"qm_polarizability", "refractive_index"}:
        wrapper = QMw(mol, work_dir=str(directory), tmp_dir=str(directory), name="monomer_polar",
                      method=config["polar_method"], basis=config["polar_basis"],
                      **basis_kwargs(request, "polarizability"), omp=16, memory=16000)
        alpha, tensor = wrapper.polar(mp=0)
        if wrapper.error_flag:
            raise RuntimeError("monomer static polarizability failed")
        assert_finite([alpha, *np.asarray(tensor).ravel()], "QM polarizability")
        props["qm_polarizability"] = float(alpha)
    assert_finite(list(props.values()), "QM properties")
    state["qm"] = {k: float(v) for k, v in props.items()}
    store_molecule(state, "monomer", mol)
    return {"qm": state["qm"], "convergence": evidence, "qm_energy_unit": "kJ/mol", "polarizability_unit": "angstrom^3",
            "effective_native_configuration": config, "forcefield": request.get("forcefield", "GAFF2_mod"),
            "properties": qm_records(request, state["qm"])}


def qm_monomer(request: dict, state: dict, directory: Path):
    config = model_configuration(request)
    if config is None:
        return _qm_monomer_single(request, state, directory)
    evidence, component_results = [], []
    for index, component in enumerate(config["components"]):
        component_dir = directory / f"component_{index + 1}"
        component_dir.mkdir(parents=True, mode=0o700)
        local_state = {"molecules": {}}
        # Every repeat unit has its own conformer, optimization and charge
        # evidence. The alias top-level SMILES is never reused for component B.
        result = _qm_monomer_single({**request, "smiles": component["smiles"]}, local_state, component_dir)
        checkpoint_name = f"monomer_{index + 1}"
        state["molecules"][checkpoint_name] = local_state["molecules"]["monomer"]
        component_results.append({"id": component["id"], "smiles": component["smiles"], "qm": local_state["qm"],
                                  "molecule_checkpoint": checkpoint_name, "convergence": result["convergence"]})
        evidence.extend({**entry, "component_id": component["id"]} for entry in result["convergence"])
    state["qm_components"] = component_results
    if len(component_results) == 1:
        state["qm"] = component_results[0]["qm"]
        state["molecules"]["monomer"] = state["molecules"]["monomer_1"]
    else:
        state["qm"] = {"components": [{"id": item["id"], "smiles": item["smiles"], **item["qm"]} for item in component_results]}
    return {"qm": state["qm"], "component_qm": component_results, "convergence": evidence,
            "qm_energy_unit": "kJ/mol", "polarizability_unit": "angstrom^3",
            "effective_native_configuration": configuration(request), "forcefield": request.get("forcefield", "GAFF2_mod"),
            "properties": qm_records(request, state["qm"])}


def build(request: dict, state: dict, directory: Path):
    from radonpy.core import poly, utils
    if model_configuration(request) is not None:
        definitions = model_components(request)
        evidence = state.get("qm_components", [])
        if len(evidence) != len(definitions) or any((item.get("id"), item.get("smiles")) != (component["id"], component["smiles"]) for item, component in zip(evidence, definitions)):
            raise RuntimeError("model components do not match the verified QM checkpoint")
        mols = [molecule(state, item["molecule_checkpoint"]) for item in evidence]
        cell, metadata = construct_model(request, mols, molecule(state, "terminal_" + request["terminal_head"]),
                                         molecule(state, "terminal_" + request["terminal_tail"]), directory,
                                         make_forcefield, mpi_processes)
        store_molecule(state, "cell", cell)
        utils.MolToPDBFile(cell, str(directory / "cell.pdb"))
        state["model"] = metadata
        if len(definitions) > 1:
            counts = {item["id"]: item["repeat_unit_count"] for item in metadata["actual_composition"]}
            state["qm"] = {"components": [{"id": item["id"], "smiles": item["smiles"],
                "repeat_unit_count": counts[item["id"]], **item["qm"]} for item in evidence]}
        return metadata
    mol, head, tail = (molecule(state, key) for key in ("monomer", "terminal_" + request["terminal_head"], "terminal_" + request["terminal_tail"]))
    dp = int(poly.calc_n_from_num_atoms([mol], request["n_atom"], terminal1=head, terminal2=tail))
    with native_proximity_guard():
        chain = poly.polymerize_rw(mol, dp, tacticity=request["tacticity"], ter1=head, ter2=tail,
                                    ff=make_forcefield(request), work_dir=str(directory), omp=0, mpi=mpi_processes(request["n_atom"]), gpu=0)
    if chain is None or not make_forcefield(request).ff_assign(chain):
        raise RuntimeError("polymer construction or forcefield assignment failed")
    count = chain.GetNumAtoms() * request["n_chain"]
    if count > 20000 or (request["calculation"] == "basic_tc" and count * request["tc_rep"] > 60000):
        raise ValueError("actual constructed cell exceeds the atom budget")
    cell = poly.amorphous_cell(chain, request["n_chain"], density=.05)
    store_molecule(state, "cell", cell)
    utils.MolToPDBFile(cell, str(directory / "cell.pdb"))
    state["model"] = {"actual_dp": dp, "atoms_per_chain": chain.GetNumAtoms(), "actual_atom_count": cell.GetNumAtoms(),
                      "n_chain": request["n_chain"], "initial_density_g_cm3": .05, "seed": request["seed"],
                      "forcefield": request.get("forcefield", "GAFF2_mod"), "tacticity": request["tacticity"], "actual_tacticity": poly.get_tacticity(chain),
                      "terminal_head": request["terminal_head"], "terminal_tail": request["terminal_tail"]}
    return state["model"]


def checked_lammps(directory: Path):
    from radonpy.sim import lammps
    class CheckedLAMMPS(lammps.LAMMPS):
        def exec(self, *args, **kwargs):
            verifier = getattr(self, "input_verifier", None)
            if verifier is not None:
                verifier(kwargs.get("input_file") or (args[0] if args else self.input_file))
            response = super().exec(*args, **kwargs)
            if response.returncode != 0:
                raise RuntimeError(f"LAMMPS returned {response.returncode}; see engine stdout")
            return response
    return CheckedLAMMPS(work_dir=str(directory), solver_path=os.environ["LAMMPS_EXEC"])


def bind_input_verifier(engine, directory: Path, mol, request: dict, stage: str, *, extra_validation=None, **controls) -> dict:
    """Guard the final input regenerated by native run(), immediately before exec."""
    snapshot = forcefield_snapshot(mol, request.get("forcefield", "GAFF2_mod"))
    evidence = {}
    def verify(name):
        path = Path(name)
        if path.is_absolute() or path.name != str(path):
            raise RuntimeError("scientific stage requires a local generated input filename")
        measured = validate_input(directory / path, snapshot, stage=stage, **controls)
        if extra_validation is not None:
            measured["protocol"] = extra_validation((directory / path).read_text())
        evidence.clear(); evidence.update(measured)
        atomic_json(directory / (path.stem + "-verification.json"), measured)
        return measured
    engine.input_verifier = verify
    return evidence


def trajectory_metadata(directory: Path, work: Path, stage: str, dt: float, interval_ps: float,
                        xtc_name="trajectory.xtc", pdb_name="cell.pdb", data_name="input.data"):
    import mdtraj as md
    xtc, pdb = directory / xtc_name, directory / pdb_name
    if not xtc.is_file():
        raise RuntimeError("requested LAMMPS trajectory was not produced")
    with md.formats.XTCTrajectoryFile(str(xtc)) as stream:
        count = len(stream)
    metadata = {"xtc_file": str(xtc.relative_to(work)), "pdb_file": str(pdb.relative_to(work)),
            "data_file": str((directory / data_name).relative_to(work)), "frame_count": count,
            "time_step_fs": dt, "sample_interval_ps": interval_ps, "source_stage": stage}
    if stage == "eq1":
        metadata.update(time_step_fs=None, sample_interval_ps=None, sample_interval_steps=round(interval_ps * 1000),
            time_schedule=[{"start_step": 0, "stop_step": 20000, "time_step_fs": .1, "start_time_ps": 0},
                           {"start_step": 20000, "stop_step": 2020000, "time_step_fs": 1, "start_time_ps": 2}])
    return metadata


def equilibration(request: dict, state: dict, work: Path, stage: str, directory: Path):
    from radonpy.core import utils
    from radonpy.sim.preset.eq import Equilibration
    mol = molecule(state, "cell")
    ranks = mpi_processes(mol.GetNumAtoms())
    engine = checked_lammps(directory)
    engine.make_dat(mol, file_name="input.data")
    utils.MolToPDBFile(mol, str(directory / "cell.pdb"))
    preset = Equilibration(mol, work_dir=str(directory), save_dir=str(directory))
    common = {"log_file": "engine.log", "dat_file": "input.data", "dump_file": None,
              "xtc_file": "trajectory.xtc", "last_str": "last.dump", "last_data": "last.data"}
    if stage == "eq1":
        md = preset.packing(f_density=.8, max_temp=700, **common)
    elif stage == "eq2":
        md = preset.eq21step(temp=request["temp"], press=request["pressure"], max_temp=600, max_press=50000,
                             set_init_velocity=True, **common)
    else:
        ns = request["eq_ns"] if stage == "eq3" else request["additional_ns"]
        preset.rg_file = "rg.profile"
        md = preset.sampling(temp=request["temp"], press=request["pressure"], step=round(ns * 1000000), **common)
    interval = round(request["sample_interval_ps"] * 1000)
    md.dump_freq = md.thermo_freq = interval
    md.log_append = False
    steps = sum(getattr(item, "step", 0) for item in md.wf)
    verified = bind_input_verifier(engine, directory, mol, request, stage, schedule=equilibrium_schedule(request, stage),
                                  thermo_steps=interval, trajectory_steps=interval, bulk_dump_steps=0, data_name="input.data")
    # Native run() generates the input. Generating it twice mutates each
    # sampling workflow's thermo_style twice and duplicates v_msd. The final
    # file is still verified by CheckedLAMMPS.exec before any engine starts.
    with progress(work, stage, directory / "engine.log", steps):
        mol = engine.run(md, mol=mol, input_file="input.in", output_file="engine.stdout", omp=0, mpi=ranks, gpu=0, intel="off", opt="off")
    store_molecule(state, "cell", mol)
    metadata = trajectory_metadata(directory, work, stage, 1.0, request["sample_interval_ps"])
    if (directory / "rg.profile").is_file():
        metadata["rg_file"] = str((directory / "rg.profile").relative_to(work))
    state["stages"][stage] = {"log_file": str((directory / "engine.log").relative_to(work)), "trajectory": metadata,
        "measured_files": {str(p.relative_to(work)): sha256(p) for p in [directory / "engine.log", directory / "trajectory.xtc", directory / "cell.pdb", directory / "input.data", directory / "input.in", directory / "input-verification.json"]}}
    return {"trajectory": metadata, "effective_config": {"input_sha256": sha256(directory / "input.in"),
            "units": "real", "thermo_steps": interval, "trajectory_steps": interval, "mpi": ranks, "gpu": 0,
            "generated_input_verification": verified},
            "current_step": steps, "total_steps": steps}


def analyze(request: dict, state: dict, work: Path, stage: str):
    from .scientific_analysis import analyze_equilibrium, analyze_trajectory_properties, property_record
    index = int(stage.rsplit("_", 1)[1])
    source = "eq3" if index == 0 else "additional_" + str(index)
    metadata = state["stages"][source]
    for name, digest in metadata["measured_files"].items():
        if sha256(work / name) != digest:
            raise RuntimeError("production input integrity mismatch")
    result = analyze_equilibrium(work / metadata["log_file"], stage=source, temp=request["temp"], pressure=request["pressure"])
    try:
        extra = analyze_trajectory_properties(work, metadata["trajectory"], stage=source, temp=request["temp"], burn_in_ps=2000.)
    except ValueError:
        # Scientifically insufficient/invalid observations are a quality outcome,
        # not a reason to rerun a successfully completed engine stage.
        window = result["properties"][0]["analysis_range"]
        extra = {"properties": [property_record(key, None, unit, "production_trajectory_analysis", source, window,
                   reason="insufficient_or_invalid_production_trajectory") for key, unit in
                   (("Rg", "A"), ("r2", "nm2"), ("static_dielectric_const", "1"), ("nematic_order_parameter", "1"))],
                 "quality": {"reason": "insufficient_or_invalid_production_trajectory"}, "equilibrated": False, "tc_eligible": False}
    result["properties"].extend(extra["properties"])
    result["quality"]["trajectory"] = extra["quality"]
    result["equilibrated"] = result["equilibrated"] and extra["equilibrated"]
    result["tc_eligible"] = result["tc_eligible"] and extra["tc_eligible"]
    result["quality_status"] = "not_converged" if not result["equilibrated"] else "warning" if any(p["quality_status"] != "passed" for p in result["properties"]) else "passed"
    result["trajectory"] = metadata["trajectory"]
    properties = {p["key"]: p for p in result["properties"]}
    window = result["properties"][0]["analysis_range"]
    selected = selected_properties(request)
    config = configuration(request)
    if selected is None or "refractive_index" in selected:
        optical = derive_optical_properties(properties, state.get("qm", {}), source, window,
            method_basis=config["polar_method"] + "/" + config["polar_basis"], native=selected is not None)
        result["properties"].extend(optical if selected is None else optical[:1])
    if result["equilibrated"] and any(p["quality_status"] != "passed" for p in result["properties"]):
        result["quality_status"] = "warning"
    result = complete_requested_results(request, result, stage=stage)
    state["analysis"] = {k: v for k, v in result.items() if k != "trajectory"}
    return retain_requested(request, result, state.get("qm", {}))


def thermal(request: dict, state: dict, work: Path, stage: str, directory: Path):
    from radonpy.core import poly, utils, calc
    from radonpy.sim.preset.tc import NEMD_MP
    from rdkit import Geometry as Geom
    if not state.get("analysis", {}).get("tc_eligible"):
        raise RuntimeError("RNEMD is blocked by equilibrium/orientation quality")
    mol = molecule(state, "cell")
    preset = NEMD_MP(mol, axis=request["tc_axis"], work_dir=str(directory), save_dir=str(directory), dump_file=None)
    ranks = mpi_processes(mol.GetNumAtoms() * request["tc_rep"])
    steps = round(request["tc_ns"] * 1000000 / .2)
    preset.make_lammps_input(step=steps, time_step=.2, temp=request["temp"], rep=request["tc_rep"], rep_other=1, decomp=False)
    # Keep the exact upstream input as an artifact, then normalize only its two
    # output guards; execute and hash the normalized input at the original path.
    input_path = directory / preset.in_file
    upstream_path = directory / "upstream-input.in"
    shutil.copyfile(input_path, upstream_path)
    text = normalize_rnemd_input(input_path.read_text())
    input_path.write_text(text)
    effective = validate_rnemd_input(text, request)
    effective["upstream_input_sha256"] = sha256(upstream_path)
    engine = checked_lammps(directory)
    verified = bind_input_verifier(engine, directory, mol, request, stage, data_name=preset.dat_file,
                                  extra_validation=lambda current: validate_rnemd_input(current, request))
    engine.input_verifier(preset.in_file)
    with progress(work, stage, directory / preset.log_file, steps):
        engine.exec(input_file=preset.in_file, output_file="engine.stdout", omp=0, mpi=ranks, gpu=0, intel="off", opt="off")
    effective["generated_input_verification"] = verified
    axis_rep = {"x": 1, "y": 1, "z": 1}
    axis_rep[request["tc_axis"]] = request["tc_rep"]
    mol = poly.super_cell(mol, **axis_rep)
    coords, _, cell, velocities, _ = checked_lammps(directory).read_traj_simple(str(directory / preset.last_str))
    assert_finite(coords, "RNEMD checkpoint")
    for i, atom in enumerate(mol.GetAtoms()):
        mol.GetConformer().SetAtomPosition(i, Geom.Point3D(*coords[i]))
        for j, name in enumerate(("vx", "vy", "vz")):
            atom.SetDoubleProp(name, float(velocities[i, j]))
    mol.cell = utils.Cell(cell[0, 1], cell[0, 0], cell[1, 1], cell[1, 0], cell[2, 1], cell[2, 0])
    store_molecule(state, "tc_cell", calc.mol_trans_in_cell(mol))
    state["stages"]["tc"] = {"log_file": str((directory / preset.log_file).relative_to(work)),
                              "profile_file": str((directory / preset.tprof_file).relative_to(work)),
                              "measured_files": {str(p.relative_to(work)): sha256(p) for p in (directory / preset.log_file, directory / preset.tprof_file,
                                  directory / preset.in_file, directory / preset.dat_file, directory / (Path(preset.in_file).stem + "-verification.json"))}}
    trajectory = trajectory_metadata(directory, work, stage, .2, .2, preset.xtc_file, preset.pdb_file, preset.dat_file)
    state["stages"]["tc"]["trajectory"] = trajectory
    return {"trajectory": trajectory, "effective_config": {**effective, "mpi": ranks,
            "input_sha256": sha256(directory / preset.in_file)},
            "current_step": steps, "total_steps": steps}


def execute(request: dict, stage: str, work: Path, checkpoint: Path | None = None, checkpoint_sha256: str | None = None) -> dict:
    import numpy as np
    from radonpy.core import const
    runtime = probe_runtime()
    if not runtime["ready"]:
        raise RuntimeError("scientific runtime is not ready: " + ";".join(runtime["errors"]))
    if request.get("seed") is None:
        raise ValueError("effective random seed is required")
    seed_engines(request["seed"])
    os.environ["LAMMPS_EXEC"] = runtime["lammps"]
    os.environ["PATH"] = str(Path(runtime["python"]).parent) + ":" + os.environ.get("PATH", "")
    const.lammps_exec = runtime["lammps"]
    const.mpi_cmd = runtime["mpi"] + " -np %i"
    state = load_state(stage, checkpoint, checkpoint_sha256, request)
    state.setdefault("runtime_fingerprint", runtime["runtime_fingerprint"])
    if state["runtime_fingerprint"] != runtime["runtime_fingerprint"]:
        raise RuntimeError("runtime fingerprint changed since checkpoint")
    directory = work / stage
    if directory.exists():
        # Only the interrupted stage is discarded. Verified prior stages are immutable.
        if directory.is_symlink():
            raise RuntimeError("unsafe stage directory")
        shutil.rmtree(directory)
    directory.mkdir(mode=0o700)
    if stage == "terminal_qm":
        result = qm_terminal(request, state, directory)
    elif stage == "monomer_qm":
        result = qm_monomer(request, state, directory)
    elif stage == "build":
        result = build(request, state, directory)
    elif stage.startswith("eq") or stage.startswith("additional_"):
        result = equilibration(request, state, work, stage, directory)
    elif stage.startswith("analyze_"):
        result = analyze(request, state, work, stage)
    elif stage in {"sp", "tg", "sp_analyze", "tg_analyze"}:
        from . import posteq_execution as posteq
        selected = selected_properties(request) or set()
        if not (selected & ({"sp_total", "sp_ced"} if stage.startswith("sp") else {"tg"})):
            raise ValueError("post-equilibrium stage was not requested")
        if stage in {"sp", "tg"}:
            result = (posteq.run_sp if stage == "sp" else posteq.run_tg)(request, state, work, directory)
        else:
            source = stage.removesuffix("_analyze")
            metadata = posteq.verified_metadata(state, work, source)
            if source == "sp":
                result = posteq.analyze_sp(work / metadata["log_file"], n_chain=metadata["conditions"]["n_chain"], conditions=metadata["conditions"])
            else:
                result = posteq.analyze_tg(work / metadata["log_file"], conditions=metadata["conditions"])
            result = retain_requested(request, result)
    elif stage == "tc":
        result = thermal(request, state, work, stage, directory)
    elif stage == "tc_analyze":
        from .scientific_analysis import analyze_rnemd
        files = state["stages"]["tc"]
        for name, digest in files["measured_files"].items():
            if sha256(work / name) != digest:
                raise RuntimeError("RNEMD measured input integrity mismatch")
        result = analyze_rnemd(work / files["log_file"], work / files["profile_file"], axis=request["tc_axis"], stage="tc")
        before = {p["key"]: p for p in state["analysis"]["properties"]}
        thermal_properties = {p["key"]: p for p in result["properties"]}
        selected = selected_properties(request)
        if selected is None or "thermal_diffusivity" in selected:
            result["properties"].append(derive_thermal_diffusivity(
                thermal_properties, before, "tc", result["properties"][0]["analysis_range"], native=selected is not None))
        if result["quality_status"] == "passed" and any(p["quality_status"] != "passed" for p in result["properties"]):
            result["quality_status"] = "warning"
    else:
        raise ValueError("unknown scientific stage")
    result = retain_requested(request, result) if stage == "tc_analyze" else result
    if stage == "monomer_qm" or "analyze" in stage:
        result = complete_requested_results(request, result, stage=stage)
    result.setdefault("parameter_applicability", parameter_applicability(request))
    result["runtime_fingerprint"] = runtime["runtime_fingerprint"]
    result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    state["completed"].append(stage)
    checkpoint = directory / "checkpoint.json"
    atomic_json(checkpoint, state)
    atomic_json(directory / "analysis.json", result)
    artifacts = []
    for file in sorted(directory.rglob("*")):
        if file.is_symlink():
            raise RuntimeError("scientific stage may not publish symlink artifacts")
        if file.is_file() and file.suffix not in {".npy", ".npz"}:
            suffix = file.suffix
            kind = "trajectory" if suffix in {".xtc", ".dump"} else "checkpoint" if file.name == "checkpoint.json" else "log" if suffix in {".log", ".out", ".stdout"} else "input" if suffix in {".in", ".data"} else "analysis"
            artifacts.append({"path": str(file.relative_to(work)), "kind": kind,
                              "media_type": "application/json" if suffix == ".json" else "text/plain" if suffix in {".in", ".log", ".out", ".stdout", ".profile"} else "application/octet-stream"})
    return {"stage": stage, "quality_status": result.get("quality_status", "passed"), "results": result,
            "equilibrated": result.get("equilibrated"), "tc_eligible": result.get("tc_eligible"),
            "checkpoint": {"path": str(checkpoint.relative_to(work)), "sha256": sha256(checkpoint)}, "artifacts": artifacts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--checkpoint-sha256")
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    args.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = execute(request, args.stage, args.work_dir.resolve(), args.checkpoint, args.checkpoint_sha256)
    atomic_json(args.result, result)


if __name__ == "__main__":
    main()

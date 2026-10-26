"""CPU-only analysis of measured data; never fabricates missing observations.

The adapter deliberately does not use RadonPy's cumulative fluctuation windows
or its NaN-tolerant check_eq. Every result carries its physical analysis window.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from .native_execution import property_record

KB = 1.3806504e-23  # Match the frozen RadonPy constants.
NA = 6.02214076e23
CAL2J = 4.184
ATM2PA = 101325.0


def parse_thermo(path: Path) -> pd.DataFrame:
    """Read the final numeric thermo block without accepting stale cached data."""
    blocks: list[pd.DataFrame] = []
    columns: list[str] | None = None
    rows: list[list[float]] = []
    for line in path.read_text(errors="replace").splitlines():
        words = line.split()
        if words and words[0] == "Step" and "Temp" in words:
            if columns and rows:
                blocks.append(pd.DataFrame(rows, columns=columns))
            columns, rows = words, []
        elif columns:
            try:
                values = [float(x) for x in words]
            except ValueError:
                values = []
            if len(values) == len(columns):
                rows.append(values)
            elif rows:
                blocks.append(pd.DataFrame(rows, columns=columns))
                columns, rows = None, []
    if columns and rows:
        blocks.append(pd.DataFrame(rows, columns=columns))
    if not blocks:
        raise ValueError("no numeric LAMMPS thermo block")
    frame = blocks[-1]
    # Repeated native input generation can append the same thermo observable
    # twice. Collapse a repeated name only when every observation agrees;
    # otherwise selecting that name would silently choose ambiguous data.
    first: dict[str, int] = {}
    keep: list[int] = []
    for index, name in enumerate(frame.columns):
        if name not in first:
            first[name] = index
            keep.append(index)
        elif not np.array_equal(frame.iloc[:, first[name]].to_numpy(float),
                                frame.iloc[:, index].to_numpy(float), equal_nan=True):
            raise ValueError("conflicting duplicate LAMMPS thermo column: " + name)
    if len(keep) != len(frame.columns):
        frame = frame.iloc[:, keep].copy()
    if "Time" not in frame or len(frame) < 2:
        raise ValueError("thermo requires Time and at least two observations")
    return frame


def physical_window(frame: pd.DataFrame, burn_in_ps: float) -> pd.DataFrame:
    time = frame["Time"].to_numpy(float) * 1e-3  # real units: fs -> ps
    if not np.isfinite(time).all() or np.any(np.diff(time) <= 0):
        raise ValueError("thermo times must be finite and strictly increasing")
    return frame.loc[time >= time[0] + burn_in_ps].copy()


def _analysis_range(selected_times_ps, *, requested_start_ps: float | None,
                    observed_start_ps: float | None, observed_stop_ps: float | None) -> dict[str, Any]:
    """Separate the requested cutoff from the samples that actually exist.

    An empty production set has no measured start or stop. Observed bounds
    describe the entire available trace, including the discarded transient.
    """
    selected = np.asarray(selected_times_ps, dtype=float)
    if selected.ndim != 1 or not np.isfinite(selected).all() or np.any(np.diff(selected) <= 0):
        raise ValueError("analysis samples must have finite strictly increasing times")
    bounds = (requested_start_ps, observed_start_ps, observed_stop_ps)
    if any(value is not None and not math.isfinite(float(value)) for value in bounds):
        raise ValueError("analysis window bounds must be finite or null")
    return {"start_ps": float(selected[0]) if len(selected) else None,
            "stop_ps": float(selected[-1]) if len(selected) else None,
            "requested_start_ps": float(requested_start_ps) if requested_start_ps is not None else None,
            "observed_start_ps": float(observed_start_ps) if observed_start_ps is not None else None,
            "observed_stop_ps": float(observed_stop_ps) if observed_stop_ps is not None else None,
            "sample_count": len(selected)}


def _block_estimate(values: np.ndarray, function=None) -> tuple[float, float | None, np.ndarray]:
    arr = np.asarray(values, float)
    calculate = function or np.mean
    value = float(calculate(arr))
    blocks = np.array([float(calculate(block)) for block in np.array_split(arr, 5) if len(block) >= 20])
    if len(blocks) < 3 or not np.isfinite(blocks).all():
        return value, None, blocks
    return value, float(np.std(blocks, ddof=1) / math.sqrt(len(blocks))), blocks


def fluctuation_properties(frame: pd.DataFrame, temp: float, pressure: float) -> dict[str, float]:
    """NPT mass-specific thermodynamic fluctuation estimators in SI units."""
    volume = frame["Volume"].to_numpy(float) * 1e-30
    density = frame["Density"].to_numpy(float) * 1000
    energy = frame["TotEng"].to_numpy(float) * CAL2J * 1000 / NA
    if not all(np.isfinite(x).all() for x in (volume, density, energy)) or np.any(volume <= 0) or np.any(density <= 0):
        raise ValueError("non-finite or non-positive NPT observations")
    mass = float(np.mean(volume * density))
    vmean = float(np.mean(volume))
    enthalpy = energy + pressure * ATM2PA * volume
    cp = float(np.var(enthalpy) / (mass * KB * temp**2))
    compress = float(np.var(volume) / (KB * temp * vmean))
    covariance = float(np.mean((enthalpy - enthalpy.mean()) * (volume - volume.mean())))
    alpha = covariance / (KB * temp**2 * vmean)
    cv = cp - vmean * temp * alpha**2 / (compress * mass) if compress > 0 else math.nan
    compress_s = compress - vmean * temp * alpha**2 / (cp * mass) if cp > 0 else math.nan
    return {"Cp": cp, "Cv": cv, "compressibility": compress,
            "isentropic_compressibility": compress_s,
            "bulk_modulus": 1 / compress if compress > 0 else math.nan,
            "isentropic_bulk_modulus": 1 / compress_s if compress_s > 0 else math.nan,
            "volume_expansion": alpha, "linear_expansion": alpha / 3}


def analyze_equilibrium(log_file: Path, *, stage: str, temp: float, pressure: float,
                        burn_in_ps: float = 2000.0) -> dict[str, Any]:
    raw = parse_thermo(log_file)
    selected = physical_window(raw, burn_in_ps)
    time_ps = raw["Time"].to_numpy(float) * 1e-3
    window = _analysis_range(selected.Time.to_numpy(float) * 1e-3,
        requested_start_ps=float(time_ps[0] + burn_in_ps),
        observed_start_ps=float(time_ps[0]), observed_stop_ps=float(time_ps[-1]))
    conditions = {"temperature_k": temp, "pressure_atm": pressure, "ensemble": "NPT"}
    props: list[dict[str, Any]] = []
    reasons = []
    required = ["Temp", "Density", "TotEng", "E_bond", "E_angle", "E_dihed", "E_vdwl"]
    if "Volume" not in raw and all(k in raw for k in ("Lx", "Ly", "Lz")):
        raw["Volume"] = raw.Lx * raw.Ly * raw.Lz
        selected = physical_window(raw, burn_in_ps)
    if len(selected) < 500:
        reasons.append("insufficient_production_samples")
    if any(k not in raw for k in (*required, "Volume")):
        reasons.append("missing_required_thermo_columns")
    elif not np.isfinite(selected[[*required, "Volume"]].to_numpy(float)).all():
        reasons.append("non_finite_production_samples")
    units = {"density": "g/cm3", "temperature": "K", "total_energy": "kJ/mol"}
    means = [("Density", "density", 1.0), ("Temp", "temperature", 1.0), ("TotEng", "total_energy", CAL2J)]
    for column, key, factor in means:
        if column in selected and len(selected):
            value, error, _ = _block_estimate(selected[column].to_numpy(float) * factor)
            props.append(property_record(key, value, units[key], "production_mean_with_5_block_SE", stage, window,
                                         error, "warning" if reasons else "passed", ";".join(reasons) or None, conditions))
        else:
            props.append(property_record(key, None, units[key], "production_mean", stage, window, reason="insufficient_production_samples", conditions=conditions))
    checks: dict[str, Any] = {"sample_count": len(selected), "burn_in_ps": burn_in_ps,
                            "analysis_range": window, "finite": not any("non_finite" in r for r in reasons)}
    # Finite gates precede every comparison; NaN cannot pass by comparison semantics.
    if not reasons:
        for column, relative_limit, absolute_limit in [("TotEng", 0.0005, 0), ("E_bond", 0.001, 0),
                ("E_angle", 0.001, 0), ("E_dihed", 0.002, 0), ("E_vdwl", 0, 30 / CAL2J), ("Density", 0, 0.001)]:
            values = selected[column].to_numpy(float)
            # Stability is measured over equal non-overlapping production blocks.
            block_means = np.array([x.mean() for x in np.array_split(values, 5)])
            sd = float(np.std(block_means, ddof=1))
            limit = max(abs(float(values.mean())) * relative_limit, absolute_limit, 1e-12)
            passed = math.isfinite(sd) and sd <= limit
            checks[column] = {"block_mean_sd": sd, "limit": limit, "passed": passed}
            if not passed:
                reasons.append("unstable_" + column)
        observed_temp = float(selected.Temp.mean())
        if not math.isfinite(observed_temp) or abs(observed_temp - temp) > max(5.0, temp * 0.02):
            reasons.append("temperature_out_of_tolerance")
        specs = {"Cp": "J/(kg K)", "Cv": "J/(kg K)", "compressibility": "1/Pa", "isentropic_compressibility": "1/Pa",
                 "bulk_modulus": "Pa", "isentropic_bulk_modulus": "Pa", "volume_expansion": "1/K", "linear_expansion": "1/K"}
        values = fluctuation_properties(selected, temp, pressure)
        block_values = [fluctuation_properties(block, temp, pressure) for block in np.array_split(selected, 5)]
        for key, value in values.items():
            estimate = np.array([b[key] for b in block_values])
            error = float(np.std(estimate, ddof=1) / math.sqrt(5)) if np.isfinite(estimate).all() else None
            valid = math.isfinite(value) and (key in {"volume_expansion", "linear_expansion"} or value > 0)
            stable = valid and error is not None and error <= max(abs(value) * 0.25, 1e-30)
            quality = "passed" if stable else "warning" if valid else "failed"
            reason = None if stable else "block_uncertainty_or_sampling_not_converged" if valid else "non_physical_fluctuation_estimate"
            props.append(property_record(key, value if valid else None, specs[key], "NPT_fluctuation_5_block_SE", stage, window, error, quality, reason, conditions))
    else:
        for key, unit in {"Cp": "J/(kg K)", "Cv": "J/(kg K)", "compressibility": "1/Pa", "isentropic_compressibility": "1/Pa",
                          "bulk_modulus": "Pa", "isentropic_bulk_modulus": "Pa", "volume_expansion": "1/K", "linear_expansion": "1/K"}.items():
            props.append(property_record(key, None, unit, "NPT_fluctuation", stage, window, reason=";".join(reasons), conditions=conditions))
    series = []
    stride = max(1, len(raw) // 1000)
    for col, key, unit, factor in [("Density", "density", "g/cm3", 1), ("Temp", "temperature", "K", 1), ("TotEng", "total_energy", "kJ/mol", CAL2J)]:
        if col in raw:
            points = [{"time_ps": float(time_ps[i]), "value": float(raw[col].iloc[i] * factor)} for i in range(0, len(raw), stride)
                      if math.isfinite(float(raw[col].iloc[i]))]
            series.append({"key": key, "label": key, "unit": unit, "points": points})
    if "v_msd" in selected and len(selected) >= 500:
        x = selected.Time.to_numpy(float) * 1e-15
        y = selected.v_msd.to_numpy(float) * 1e-20
        if np.isfinite(y).all():
            regression = stats.linregress(x, y)
            estimates = np.array([stats.linregress(block.Time.to_numpy(float) * 1e-15,
                                   block.v_msd.to_numpy(float) * 1e-20).slope / 6
                                  for block in np.array_split(selected, 5)])
            diffusion = float(regression.slope / 6)
            error = float(np.std(estimates, ddof=1) / math.sqrt(5)) if np.isfinite(estimates).all() else None
            reliable = diffusion > 0 and regression.rvalue**2 >= 0.95 and error is not None and error <= diffusion * 0.25
            props.append(property_record("self-diffusion", diffusion if diffusion > 0 else None, "m2/s",
                "Einstein_all_atom_MSD_com_removed_linear_fit", stage, window, error,
                "passed" if reliable else "warning", None if reliable else "long_time_diffusive_regime_not_confirmed",
                {**conditions, "fit_r2": float(regression.rvalue**2) if math.isfinite(regression.rvalue) else None,
                 "observable": "all_atom_not_chain_COM"}))
    if reasons:
        for prop in props:
            if prop["quality_status"] == "passed":
                prop.update(quality_status="warning", reason=";".join(reasons))
    property_warning = any(p["quality_status"] != "passed" for p in props)
    return {"properties": props, "series": series, "quality": {"checks": checks, "reasons": reasons},
            "equilibrated": not reasons, "tc_eligible": not reasons,
            "quality_status": "not_converged" if reasons else "warning" if property_warning else "passed"}


def _charges_from_data(path: Path, atom_count: int) -> np.ndarray:
    charges = np.full(atom_count, np.nan)
    reading = False
    for line in path.read_text().splitlines():
        text = line.split("#", 1)[0].strip()
        if text == "Atoms":
            reading = True
            continue
        if not reading or not text:
            continue
        fields = text.split()
        if not fields[0].isdigit():
            break
        if len(fields) < 7:
            raise ValueError("LAMMPS full atom style is required for partial charges")
        index = int(fields[0]) - 1
        if not 0 <= index < atom_count or math.isfinite(float(charges[index])):
            raise ValueError("duplicate or invalid atom ID in charge data")
        charges[index] = float(fields[3])
    if not np.isfinite(charges).all() or abs(float(charges.sum())) > 1e-4:
        raise ValueError("finite neutral per-atom charges are required for static dielectric")
    return charges


def analyze_trajectory_properties(work_dir: Path, metadata: dict, *, stage: str, temp: float,
                                  burn_in_ps: float = 2000.0) -> dict[str, Any]:
    """Stream unwrapped production frames; retain scalar traces, never whole trajectories."""
    import mdtraj
    xtc = _safe_file(work_dir, metadata["xtc_file"])
    pdb = _safe_file(work_dir, metadata["pdb_file"])
    chain_indices, endpoints, charges = None, None, None
    charge_error, endpoint_error = None, None
    times, rg_values, r2_values, order_values, dipoles, volumes = [], [], [], [], [], []
    origin = None
    last_observed_time = None
    frame_offset = 0
    for chunk in mdtraj.iterload(str(xtc), top=str(pdb), chunk=100):
        chunk_times = trajectory_times_ps(metadata, np.arange(frame_offset, frame_offset + chunk.n_frames), chunk.time)
        frame_offset += chunk.n_frames
        if (not np.isfinite(chunk_times).all() or np.any(np.diff(chunk_times) <= 0)
                or (last_observed_time is not None and float(chunk_times[0]) <= last_observed_time)):
            raise ValueError("non-finite or non-monotonic trajectory time")
        last_observed_time = float(chunk_times[-1])
        if chain_indices is None:
            chain_indices = [[atom.index for atom in chain.atoms] for chain in chunk.topology.chains]
            if not chain_indices or any(not chain for chain in chain_indices):
                raise ValueError("trajectory requires nonempty polymer chain topology")
            endpoints = []
            for chain in chunk.topology.chains:
                heads = [r.index for r in chain.residues if r.name == "TU0"]
                tails = [r.index for r in chain.residues if r.name == "TU1"]
                if len(heads) != 1 or len(tails) != 1:
                    endpoint_error = "missing_or_ambiguous_TU0_TU1_end_groups"
                else:
                    endpoints.append((heads[0], tails[0]))
            try:
                charge_name = metadata.get("data_file") or metadata.get("dat_file")
                if not charge_name:
                    raise ValueError("charge data is missing")
                charges = _charges_from_data(_safe_file(work_dir, charge_name), chunk.n_atoms)
            except (ValueError, OSError):
                charge_error = "missing_invalid_or_non_neutral_partial_charges"
        if origin is None:
            origin = float(chunk_times[0])
        selected = np.flatnonzero(chunk_times >= origin + burn_in_ps)
        if not len(selected):
            continue
        frame = chunk[selected]
        if not np.isfinite(frame.xyz).all() or not np.isfinite(frame.time).all():
            raise ValueError("non-finite production trajectory")
        per_chain = []
        for atom_indices in chain_indices:
            masses = np.array([frame.topology.atom(i).element.mass for i in atom_indices], dtype=float)
            if not np.isfinite(masses).all() or np.any(masses <= 0):
                raise ValueError("atomic masses are missing from topology")
            xyz = frame.xyz[:, atom_indices].astype(np.float64)
            center = np.sum(xyz * masses[None, :, None], axis=1) / masses.sum()
            radius = np.sqrt(np.sum(np.sum((xyz - center[:, None])**2, axis=2) * masses, axis=1) / masses.sum())
            per_chain.append(radius * 10)  # nm -> Angstrom, mass weighted
        rg_values.extend(np.mean(per_chain, axis=0).tolist())
        if endpoint_error is None:
            contact, _ = mdtraj.compute_contacts(frame, contacts=endpoints, scheme="closest", periodic=False)
            r2_values.extend(np.mean(contact**2, axis=1).tolist())  # native nm²; never silently label Å²
        order_values.extend(mdtraj.compute_nematic_order(frame, indices="residues").tolist())
        if charges is not None:
            if frame.unitcell_volumes is None or not np.isfinite(frame.unitcell_volumes).all() or np.any(frame.unitcell_volumes <= 0):
                charge_error, charges = "missing_or_non_positive_periodic_cell", None
            else:
                dipoles.extend(mdtraj.dipole_moments(frame, charges).tolist())
                volumes.extend(frame.unitcell_volumes.tolist())
        times.extend(chunk_times[selected].tolist())
    window = _analysis_range(times,
        requested_start_ps=float(origin + burn_in_ps) if origin is not None else None,
        observed_start_ps=origin, observed_stop_ps=last_observed_time)
    if len(times) < 100:
        reason = "insufficient_production_trajectory_frames"
        properties = [property_record(key, None, unit, "production_trajectory", stage, window, reason=reason)
                      for key, unit in [("Rg", "A"), ("r2", "nm2"), ("nematic_order_parameter", "1"), ("static_dielectric_const", "1")]]
        return {"properties": properties, "quality": {"production_frame_count": len(times), "analysis_range": window, "reasons": [reason]},
                "equilibrated": False, "tc_eligible": False, "quality_status": "not_converged"}
    conditions = {"temperature_k": temp, "frames": len(times), "ensemble": "NPT", "unwrapped": True}
    properties = []
    structure_equilibrated = True
    for key, values, unit, method, tolerance in [
        ("Rg", rg_values, "A", "mass_weighted_chain_Rg_mean_5_block_SE", 0.01),
        ("r2", r2_values, "nm2", "TU0_TU1_closest_atom_distance_squared_5_block_SE", 0.25),
        ("nematic_order_parameter", order_values, "1", "residue_director_nematic_tensor_5_block_SE", 0.25),
    ]:
        if len(values) != len(times):
            properties.append(property_record(key, None, unit, method, stage, window,
                reason=endpoint_error or "missing_observations", conditions=conditions))
            continue
        value, error, _ = _block_estimate(np.asarray(values))
        stable = error is not None and error <= max(abs(value) * tolerance, 1e-3 if key == "nematic_order_parameter" else 1e-30)
        if key == "Rg" and not stable:
            structure_equilibrated = False
        properties.append(property_record(key, value, unit, method, stage, window, error,
            "passed" if stable else "warning", None if stable else "structure_block_uncertainty_not_converged", conditions))
    if charges is not None and len(dipoles) == len(times):
        observations = np.column_stack([dipoles, volumes])

        def dielectric(block):
            moments = block[:, :3] * 1.602176634e-28  # e·nm -> C·m
            variance = float(np.mean(np.sum((moments - moments.mean(axis=0))**2, axis=1)))
            return 1 + variance / (3 * 8.8541878128e-12 * KB * temp * float(block[:, 3].mean()) * 1e-27)

        value, error, _ = _block_estimate(observations, dielectric)
        stable = error is not None and error <= max((value - 1) * 0.25, 0.01)
        properties.append(property_record("static_dielectric_const", value, "1", "fixed_charge_dipole_fluctuation_5_block_SE",
            stage, window, error, "passed" if stable else "warning", None if stable else "dipole_sampling_not_converged", conditions))
    else:
        properties.append(property_record("static_dielectric_const", None, "1", "fixed_charge_dipole_fluctuation",
            stage, window, reason=charge_error or "missing_dipole_observations", conditions=conditions))
    order = next((item["value"] for item in properties if item["key"] == "nematic_order_parameter"), None)
    return {"properties": properties, "quality": {"production_frame_count": len(times), "analysis_range": window, "structure_equilibrated": structure_equilibrated,
                "nematic_order_below_0_1": order is not None and order < 0.1, "endpoint_error": endpoint_error, "charge_error": charge_error},
            "equilibrated": structure_equilibrated,
            "tc_eligible": structure_equilibrated and order is not None and order < 0.1,
            "quality_status": "warning" if any(p["quality_status"] != "passed" for p in properties) else "passed"}


def read_average_blocks(path: Path) -> list[tuple[int, list[str], np.ndarray]]:
    """Preserve real timesteps and the ave/chunk column header, including density."""
    text = path.read_text().splitlines()
    columns = None
    for line in text:
        tokens = line.strip().lstrip("#").split()
        if tokens and tokens[0] == "Chunk" and "temp" in tokens:
            columns = tokens
    if columns is None:
        raise ValueError("temperature profile requires an explicit Chunk/temp header")
    lines = [x.strip() for x in text if x.strip() and not x.strip().startswith("#")]
    out, index = [], 0
    while index < len(lines):
        header = lines[index].split()
        if len(header) != 3:
            raise ValueError("invalid ave/chunk block header")
        timestep, count = int(header[0]), int(header[1])
        rows = [[float(x) for x in line.split()] for line in lines[index + 1:index + 1 + count]]
        if len(rows) != count or any(len(row) != len(columns) for row in rows):
            raise ValueError("incomplete ave/chunk block")
        out.append((timestep, columns, np.asarray(rows)))
        index += count + 1
    return out


def read_average(path: Path) -> list[np.ndarray]:
    return [block[2] for block in read_average_blocks(path)]


def _rnemd_production_clock(frame: pd.DataFrame, time_step_fs: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Validate a fixed-dt native clock before aligning it with profile steps.

    A native reset_timestep can preserve elapsed Time while resetting Step.
    Only a single constant offset is accepted; a changing offset is a broken
    clock, rather than an offset that can safely be removed. The 1e-6 fs
    tolerance is absolute, so a long trace cannot hide a physical time jump.
    """
    if (not isinstance(time_step_fs, (int, float)) or isinstance(time_step_fs, bool)
            or not math.isfinite(time_step_fs) or time_step_fs <= 0):
        raise ValueError("RNEMD timestep must be finite and positive")
    if "Step" not in frame:
        raise ValueError("RNEMD thermo requires Step")
    steps, native = frame.Step.to_numpy(float), frame.Time.to_numpy(float)
    if (not np.isfinite(steps).all() or np.any(steps < 0) or np.any(steps != np.floor(steps))
            or np.any(np.diff(steps) <= 0)):
        raise ValueError("RNEMD Step must be finite nonnegative strictly increasing integers")
    if not np.isfinite(native).all() or np.any(np.diff(native) <= 0):
        raise ValueError("RNEMD Time must be finite and strictly increasing")
    production = steps * time_step_fs
    offset = native - production
    tolerance_fs = 1e-6
    if (not np.isfinite(production).all() or not np.isfinite(offset).all()
            or not np.allclose(offset, offset[0], rtol=0, atol=tolerance_fs)):
        raise ValueError("RNEMD Time - Step * timestep must be a constant finite offset")
    normalized = frame.copy()
    normalized["Time"] = production
    return normalized, {"time_step_fs": float(time_step_fs), "native_time_offset_fs": float(offset[0]),
        "native_time_offset_ps": float(offset[0] * 1e-3), "offset_tolerance_fs": tolerance_fs,
        "production_time_basis": "Step * time_step_fs",
        "observed_step_start": float(steps[0]), "observed_step_stop": float(steps[-1]),
        "native_observed_start_ps": float(native[0] * 1e-3), "native_observed_stop_ps": float(native[-1] * 1e-3)}


def _finite_number(value) -> float | None:
    return float(value) if math.isfinite(float(value)) else None


def _gradient_fit_record(fit, coordinates: np.ndarray, profile_window: dict[str, Any]) -> dict[str, Any]:
    return {"slope_k_m": _finite_number(fit.slope), "intercept_k": _finite_number(fit.intercept),
            "r2": _finite_number(fit.rvalue**2), "slope_standard_error_k_m": _finite_number(fit.stderr),
            "intercept_standard_error_k": _finite_number(fit.intercept_stderr),
            "fit_window": {"start_m": float(coordinates[0]), "stop_m": float(coordinates[-1]),
                           "sample_count": len(coordinates)}, "profile_time_window": profile_window}


def analyze_rnemd(log_file: Path, profile_file: Path, *, axis: str, stage: str,
                 burn_in_ps: float = 800.0, time_step_fs: float = 0.2) -> dict[str, Any]:
    frame, clock = _rnemd_production_clock(parse_thermo(log_file), time_step_fs)
    if not math.isfinite(burn_in_ps) or burn_in_ps < 0:
        raise ValueError("RNEMD burn-in must be finite and nonnegative")
    # Profile timesteps start from the production reset. An absolute cutoff
    # keeps thermo and profiles on one clock even when the first sample is late.
    time_ps = frame.Time.to_numpy(float) * 1e-3
    selected = frame.loc[time_ps >= burn_in_ps].copy()
    props, reasons = [], []
    profile = read_average_blocks(profile_file)
    window = _analysis_range(selected.Time.to_numpy(float) * 1e-3, requested_start_ps=burn_in_ps,
        observed_start_ps=float(time_ps[0]), observed_stop_ps=float(time_ps[-1]))
    steps = np.asarray([item[0] for item in profile], dtype=float)
    times = steps * time_step_fs * 1e-3
    if (not np.isfinite(steps).all() or np.any(steps < 0) or not np.isfinite(times).all()
            or np.any(np.diff(steps) <= 0) or np.any(np.diff(times) <= 0)):
        raise ValueError("temperature profile timesteps must be finite nonnegative and strictly increasing")
    # Native ave/chunk blocks average the preceding exchange interval. Their
    # end timestamps are not instantaneous measurements. The frozen preset
    # starts averaging at production step zero and uses a fixed interval.
    starts = np.concatenate(([0.0], steps[:-1])) if len(steps) else np.array([])
    intervals = steps - starts
    if len(intervals) and (np.any(intervals <= 0) or np.any(intervals != intervals[0])):
        raise ValueError("RNEMD profile averaging interval must be fixed and anchored at production step zero")
    interval_steps = int(intervals[0]) if len(intervals) else None
    start_times = starts * time_step_fs * 1e-3
    profile_mask = start_times >= burn_in_ps
    chosen_records = [item for include, item in zip(profile_mask, profile) if include]
    chosen = [item[2] for item in chosen_records]
    selected_times = times[profile_mask]
    selected_support_starts = start_times[profile_mask]
    profile_window = _analysis_range(selected_times, requested_start_ps=burn_in_ps,
        observed_start_ps=float(times[0]) if len(times) else None,
        observed_stop_ps=float(times[-1]) if len(times) else None)
    quality = {"clock": clock, "production_thermo_samples": len(selected), "profile_blocks": len(profile),
               "production_profile_blocks": len(chosen), "analysis_range": window,
               "production_profile_window": profile_window,
               "profile_averaging": {"interval_steps": interval_steps,
                   "interval_ps": float(interval_steps * time_step_fs * 1e-3) if interval_steps is not None else None,
                   "fixed_interval_verified": len(steps) >= 2, "first_block_start_ps": 0.0 if len(steps) else None,
                   "block_support": "(previous_profile_end, profile_end]"},
               "production_profile_support": {"start_ps": float(selected_support_starts[0]) if len(chosen) else None,
                   "stop_ps": float(selected_times[-1]) if len(chosen) else None,
                   "requested_start_ps": float(burn_in_ps), "block_count": len(chosen)}}
    conditions = {"axis": axis, "decomp": False, "time_step_fs": time_step_fs,
                  "time_basis": "production_Step_times_dt", "burn_in_ps": burn_in_ps,
                  "observable": "single_axis_finite_size_single_run",
                  "uncertainty_scope": "within_run_time_blocks_no_size_extrapolation_or_independent_seeds"}
    if len(profile) < 3 or len(selected) < 20:
        reason = "insufficient_RNEMD_steady_state_samples"
        return {"properties": [property_record("thermal_conductivity", None, "W/(m K)", "RNEMD_Muller_Plathe", stage,
                    window, reason=reason, conditions=conditions)], "series": [],
                "quality": {**quality, "reasons": [reason]},
                "quality_status": "not_converged"}
    if len(chosen) < 20:
        reason = "insufficient_RNEMD_production_temperature_profiles"
        return {"properties": [property_record("thermal_conductivity", None, "W/(m K)", "RNEMD_Muller_Plathe", stage,
                    window, reason=reason, conditions=conditions)], "series": [],
                "quality": {**quality, "reasons": [reason]},
                "quality_status": "not_converged"}
    values = np.stack(chosen)
    if not np.isfinite(values).all():
        raise ValueError("non-finite RNEMD profiles")
    mean = values.mean(axis=0)
    columns = profile[0][1]
    if any(item[1] != columns for item in profile):
        raise ValueError("temperature profile columns changed")
    temperature_column = columns.index("temp")
    coordinate_column = columns.index("Coord1")
    count_column = columns.index("Ncount")
    nslab = len(mean)
    length = float(frame[{"x": "Lx", "y": "Ly", "z": "Lz"}[axis]].iloc[-1])
    if not math.isfinite(length) or length <= 0:
        raise ValueError("RNEMD box length must be finite and positive")
    # Profile columns: chunk, reduced coordinate, atom count, temperature.
    coordinates = mean[:, coordinate_column] * length * 1e-10
    left = slice(1, nslab // 2)
    right = slice(nslab // 2 + 1, nslab)
    if nslab < 10 or nslab % 2 or np.any(values[:, :, count_column] <= 0) or not np.allclose(values[:, :, coordinate_column], mean[:, coordinate_column]):
        raise ValueError("RNEMD requires populated even slab count with stable coordinates")
    lfit = stats.linregress(coordinates[left], mean[left, temperature_column])
    rfit = stats.linregress(coordinates[right], mean[right, temperature_column])
    qualified = 0
    for block in values:
        ok = False  # Intentionally reset every time block (upstream defect).
        a = stats.linregress(coordinates[left], block[left, temperature_column])
        b = stats.linregress(coordinates[right], block[right, temperature_column])
        ok = math.isfinite(a.rvalue) and math.isfinite(b.rvalue) and a.rvalue**2 >= 0.95 and b.rvalue**2 >= 0.95
        qualified += int(ok)
    rate = qualified / len(values)
    gradient = (abs(lfit.slope) + abs(rfit.slope)) / 2
    if "v_heatflux" not in selected:
        raise ValueError("missing normalized RNEMD v_heatflux column")
    if not np.isfinite(selected.v_heatflux.to_numpy(float)).all():
        raise ValueError("non-finite RNEMD exchanged energy")
    # Frozen upstream input ALREADY converts f_mp/(2*area) into J/m².
    # Dividing this cumulative areal energy by time in seconds gives W/m².
    fit = stats.linregress(selected.Time.to_numpy(float) * 1e-15, selected.v_heatflux.to_numpy(float))
    flux = abs(float(fit.slope))
    conductivity = flux / gradient if gradient > 0 else math.nan
    if not all(math.isfinite(float(x)) for x in [gradient, conductivity, flux, lfit.rvalue, rfit.rvalue, fit.rvalue]) or conductivity <= 0:
        reasons.append("non_finite_or_non_positive_heat_transport")
    if lfit.rvalue**2 < 0.98 or rfit.rvalue**2 < 0.98 or rate < 0.667:
        reasons.append("temperature_gradient_not_stable_or_linear")
    if fit.rvalue**2 < 0.98:
        reasons.append("exchange_energy_not_linear")
    if lfit.slope * rfit.slope >= 0:
        reasons.append("temperature_gradients_not_opposed")
    block_conductivity = []
    for positions in np.array_split(np.arange(len(values)), 5):
        if len(positions) < 4:
            continue
        block_mean = values[positions].mean(axis=0)
        first = stats.linregress(coordinates[left], block_mean[left, temperature_column])
        second = stats.linregress(coordinates[right], block_mean[right, temperature_column])
        block_gradient = (abs(first.slope) + abs(second.slope)) / 2
        t_start, t_stop = selected_support_starts[positions[0]], selected_times[positions[-1]]
        log_block = selected.loc[(selected.Time * 1e-3 >= t_start) & (selected.Time * 1e-3 <= t_stop)]
        if len(log_block) < 3 or not math.isfinite(block_gradient) or block_gradient <= 0:
            continue
        block_flux = stats.linregress(log_block.Time.to_numpy(float) * 1e-15, log_block.v_heatflux.to_numpy(float)).slope
        if math.isfinite(block_flux):
            block_conductivity.append(abs(float(block_flux)) / block_gradient)
    error = (float(np.std(block_conductivity, ddof=1) / math.sqrt(len(block_conductivity)))
             if len(block_conductivity) == 5 and np.isfinite(block_conductivity).all() else None)
    if error is None or error > max(abs(conductivity) * 0.25, 1e-30):
        reasons.append("conductivity_block_uncertainty_not_converged")
    props.append(property_record("thermal_conductivity", conductivity, "W/(m K)", "RNEMD_Muller_Plathe_5_block_SE", stage, window, error,
                                 quality="passed" if not reasons else "warning", reason=";".join(reasons) or None,
                                 conditions=conditions))
    exchange_fit = {"slope_w_m2": _finite_number(fit.slope), "intercept_j_m2": _finite_number(fit.intercept),
                    "r2": _finite_number(fit.rvalue**2), "slope_standard_error_w_m2": _finite_number(fit.stderr),
                    "intercept_standard_error_j_m2": _finite_number(fit.intercept_stderr), "fit_window": window}
    return {"properties": props, "series": [{"key": "temperature_profile", "label": "steady temperature profile", "unit": "K",
                    "points": [{"position_angstrom": float(x / 1e-10), "value": float(y)} for x, y in zip(coordinates, mean[:, temperature_column])]}],
            "quality": {**quality, "left_r2": _finite_number(lfit.rvalue**2), "right_r2": _finite_number(rfit.rvalue**2),
                        "qualified_block_rate": rate, "qualified_profile_blocks": qualified,
                        "temperature_gradient_fits": {"left": _gradient_fit_record(lfit, coordinates[left], profile_window),
                                                      "right": _gradient_fit_record(rfit, coordinates[right], profile_window)},
                        "exchange_energy_fit": exchange_fit,
                        "exchange_r2": _finite_number(fit.rvalue**2), "gradient_k_m": _finite_number(gradient), "heat_flux_w_m2": _finite_number(flux),
                        "conductivity_block_estimates": block_conductivity, "reasons": reasons},
            "quality_status": "passed" if not reasons else "not_converged"}


def _trajectory_metadata(stage_result: dict) -> dict:
    result = stage_result.get("results", stage_result)
    meta = result.get("trajectory")
    if not isinstance(meta, dict):
        raise ValueError("stage has no trajectory")
    return meta


def trajectory_times_ps(metadata: dict, frame_indices, native_times) -> np.ndarray:
    """Integrate the actual step schedule when XTC writes step * current dt.

    LAMMPS XTC timestamps jump after a timestep change. The schedule and dump
    interval come from the validated effective input, never guessed from frames.
    """
    native = np.asarray(native_times, dtype=float)
    indices = np.asarray(frame_indices)
    if indices.shape != native.shape or not np.issubdtype(indices.dtype, np.integer) or np.any(indices < 0):
        raise ValueError("invalid trajectory frame indices")
    schedule = metadata.get("time_schedule")
    if schedule is None:
        if not np.isfinite(native).all():
            raise ValueError("non-finite native trajectory time")
        return native
    interval = metadata.get("sample_interval_steps")
    if (not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0
            or not isinstance(schedule, list) or not schedule):
        raise ValueError("a step schedule requires a positive integer dump interval")
    result = np.full(native.shape, np.nan)
    previous_stop, previous_time = None, None
    steps = indices * interval
    for segment in schedule:
        if not isinstance(segment, dict):
            raise ValueError("invalid trajectory timestep schedule segment")
        start, stop = segment.get("start_step"), segment.get("stop_step")
        dt, origin = segment.get("time_step_fs"), segment.get("start_time_ps")
        if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(stop, int) or isinstance(stop, bool)
                or start < 0 or stop <= start or not isinstance(dt, (int, float)) or isinstance(dt, bool)
                or not math.isfinite(dt) or dt <= 0 or not isinstance(origin, (int, float)) or isinstance(origin, bool)
                or not math.isfinite(origin)):
            raise ValueError("invalid trajectory timestep schedule")
        if previous_stop is None:
            if start != 0:
                raise ValueError("trajectory timestep schedule must start at step zero")
        elif start != previous_stop or not math.isclose(origin, previous_time, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("trajectory timestep schedule must be contiguous in step and time")
        selected = (steps >= start) & (steps <= stop)
        result[selected] = origin + (steps[selected] - start) * dt * 1e-3
        previous_stop, previous_time = stop, origin + (stop - start) * dt * 1e-3
    if not np.isfinite(result).all():
        raise ValueError("trajectory frame is outside the effective timestep schedule")
    return result


def _safe_file(root: Path, relative: str) -> Path:
    from .journal import safe_file
    return safe_file(root, relative)


def read_trajectory(work_dir: Path, stage_result: dict, offset: int, limit: int) -> dict:
    import mdtraj
    meta = _trajectory_metadata(stage_result)
    frame_index = max(0, int(offset))
    if not 1 <= int(limit) <= 10000:
        raise ValueError("invalid trajectory atom limit")
    frame = mdtraj.load_frame(str(_safe_file(work_dir, meta["xtc_file"])), index=frame_index,
                              top=str(_safe_file(work_dir, meta["pdb_file"])))
    points = [{"atom_id": i + 1, "chain_id": atom.residue.chain.index + 1,
               "atom_type": atom.element.symbol if atom.element else atom.name,
               "x": float(frame.xyz[0, i, 0] * 10), "y": float(frame.xyz[0, i, 1] * 10), "z": float(frame.xyz[0, i, 2] * 10)}
              for i, atom in enumerate(frame.topology.atoms) if i < limit]
    box = frame.unitcell_lengths[0] * 10 if frame.unitcell_lengths is not None else [0, 0, 0]
    physical_time = trajectory_times_ps(meta, np.array([frame_index]), frame.time)[0]
    return {"stage_id": stage_result.get("stage", ""), "frame_index": frame_index,
            "frame_count": int(meta.get("frame_count", 0)), "time_ps": float(physical_time), "sampled_points": len(points),
            "atom_count": frame.n_atoms, "points": points, "box": dict(zip(("lx", "ly", "lz"), map(float, box))),
            "units": {"coordinates": "A", "time": "ps"}}


def analyze_distance(work_dir: Path, stage_result: dict, payload: dict) -> dict:
    import mdtraj
    meta = _trajectory_metadata(stage_result)
    atom1, atom2 = int(payload["atom_id_1"]), int(payload["atom_id_2"])
    if atom1 == atom2 or min(atom1, atom2) < 1:
        raise ValueError("two distinct positive atom IDs required")
    start, stop, stride = payload.get("start_frame", 0), payload.get("stop_frame"), payload.get("stride", 1)
    if start < 0 or stride < 1 or (stop is not None and stop <= start):
        raise ValueError("invalid frame range")
    points, index = [], 0
    # Iterate bounded chunks rather than loading a multi-GB trajectory.
    for chunk in mdtraj.iterload(str(_safe_file(work_dir, meta["xtc_file"])), top=str(_safe_file(work_dir, meta["pdb_file"])), chunk=100):
        physical_times = trajectory_times_ps(meta, np.arange(index, index + chunk.n_frames), chunk.time)
        if max(atom1, atom2) > chunk.n_atoms:
            raise ValueError("atom ID does not exist in this stage")
        for local in range(chunk.n_frames):
            frame = index + local
            if frame < start or (stop is not None and frame >= stop) or (frame - start) % stride:
                continue
            value = float(mdtraj.compute_distances(chunk[local], [[atom1 - 1, atom2 - 1]], periodic=bool(payload.get("use_pbc", True)))[0, 0] * 10)
            points.append({"frame": frame, "time_ps": float(physical_times[local]), "value": value})
            if len(points) > 10000:
                raise ValueError("distance request exceeds 10000 samples; increase stride")
        index += chunk.n_frames
        if stop is not None and index >= stop:
            break
    if not points:
        raise ValueError("empty distance sample range")
    values = [p["value"] for p in points]
    return {"atom_id_1": atom1, "atom_id_2": atom2, "use_pbc": bool(payload.get("use_pbc", True)), "unit": "A", "points": points,
            "stats": {"samples": len(points), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}}

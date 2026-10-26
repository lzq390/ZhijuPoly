"""Controlled native post-equilibrium protocols and measured-data analysis.

Tg uses the implemented EQMD step-wise branch, not TGMD.exec's incomplete
parameter forwarding. SP retains the native cohesive-energy definition while
using total group/group energies, so TALLY-specific decompositions are absent.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess

import numpy as np
import pandas as pd
from scipy import stats

from .runtime import sha256
from .scientific_analysis import physical_window, property_record, _analysis_range, _block_estimate
from .generated_input import segment

SP_STEPS = 500000
SP_BURN_IN_PS = 100.
TG_HIGH_K, TG_LOW_K, TG_INTERVAL_K = 800., 100., 10.
TG_EQUILIBRATION_STEPS = 500000
TG_STEPS_PER_INTERVAL = 20000
TG_THERMO_STEPS = 100
TG_PLATEAU_BURN_IN_PS = 2.


def thermo_blocks(path: Path) -> list[pd.DataFrame]:
    """Keep individual native run blocks; do not merge temperature plateaus."""
    blocks, columns, rows = [], None, []
    with path.open() as stream:
        for line in stream:
            words = line.split()
            if words and words[0] == "Step" and "Time" in words:
                if columns and rows:
                    blocks.append(pd.DataFrame(rows, columns=columns))
                columns, rows = words, []
            elif columns:
                try:
                    values = [float(word) for word in words]
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
        raise ValueError("post-equilibrium log has no numeric run blocks")
    return blocks


def qualified_source(state: dict, work: Path) -> str:
    if state.get("analysis", {}).get("equilibrated") is not True:
        raise RuntimeError("post-equilibrium calculation requires a qualified equilibrium state")
    analyzed = [name for name in state["completed"] if re.fullmatch(r"analyze_[0-2]", name)]
    if not analyzed:
        raise RuntimeError("qualified equilibrium checkpoint is missing")
    index = int(analyzed[-1].rsplit("_", 1)[1])
    source = "eq3" if index == 0 else f"additional_{index}"
    for name, digest in state["stages"][source]["measured_files"].items():
        path = work / name
        if path.is_symlink() or sha256(path) != digest:
            raise RuntimeError("qualified equilibrium input integrity mismatch")
    return source


def _metadata(work, files, conditions, source):
    return {"measured_files": {str(path.relative_to(work)): sha256(path) for path in files},
            "log_file": str(files[0].relative_to(work)), "conditions": conditions, "source_equilibrium_stage": source}


def verified_metadata(state, work, stage):
    metadata = state["stages"][stage]
    for name, digest in metadata["measured_files"].items():
        path = work / name
        if path.is_symlink() or sha256(path) != digest:
            raise RuntimeError(stage + " measured input integrity mismatch")
    return metadata


def _prepare(md, *, data, log, last, dump=None, thermo=1000):
    md.dat_file, md.log_file = data, log
    md.dump_file, md.xtc_file = dump, None
    md.outstr, md.write_data = last + ".dump", last + ".data"
    md.rst, md.log_append = False, False
    md.thermo_freq = md.dump_freq = thermo
    return md


def _check_npt_input(text, expected_runs, *, pressure):
    lines = [" ".join(line.split()) for line in text.splitlines()]
    if [line for line in lines if line.startswith("units ")] != ["units real"]:
        raise RuntimeError("native post-equilibrium input must use real units")
    if [int(line.split()[1]) for line in lines if re.fullmatch(r"run \d+", line)] != expected_runs:
        raise RuntimeError("native post-equilibrium run schedule differs from the managed protocol")
    timesteps = [float(line.split()[1]) for line in lines if line.startswith("timestep ")]
    if not timesteps or any(value != 1. for value in timesteps):
        raise RuntimeError("post-equilibrium timestep must be one femtosecond")
    npt = [line.split() for line in lines if line.startswith("fix ") and "npt" in line.split()]
    if len(npt) != len(expected_runs):
        raise RuntimeError("native post-equilibrium NPT schedule is incomplete")
    for fields in npt:
        if "iso" not in fields:
            raise RuntimeError("managed post-equilibrium pressure must be isotropic")
        index = fields.index("iso")
        if any(not math.isclose(float(fields[index + offset]), pressure) for offset in (1, 2)):
            raise RuntimeError("native pressure differs from the requested pressure")


def total_sp_workflow(md, n_chain):
    """Native SPMD rerun, total-only equivalent without pe/mol/tally.

    E_inter = pair_inter + long_total - sum(long_self_chain). The native
    analyzer's (sum(E_intra)-E_bulk)/V is exactly -E_inter/V. No vdW/Coulomb
    component columns or values are fabricated.
    """
    if not 2 <= n_chain <= 20:
        raise ValueError("SP requires two to twenty explicit polymer chains")
    wf = md.wf[-1]
    commands, terms = [], []
    for index in range(n_chain):
        commands += [f"group mol{index} molecule {index + 1}",
                     f"compute mol{index}_kspace mol{index} group/group mol{index} pair no kspace yes"]
        terms.append(f"c_mol{index}_kspace")
    commands += ["compute sp_pair_inter all group/group all pair yes kspace no molecule inter"]
    wf.add = commands
    wf.thermo_style = ["c_sp_pair_inter", *terms]
    wf.rerun_dump = "eq0.dump"
    # Real sampling records positions, images, velocities and the periodic box.
    wf.rerun_keyword = "dump x y z ix iy iz vx vy vz"
    md.outstr = md.write_data = None
    md.rst = md.log_append = False
    md.dump_file = md.xtc_file = None
    return md


def _verify_sp_rerun(text, n_chain):
    lines = [" ".join(raw.split("#", 1)[0].split()) for raw in text.splitlines() if raw.split("#", 1)[0].strip()]
    expected = ["compute sp_pair_inter all group/group all pair yes kspace no molecule inter"]
    for index in range(n_chain):
        expected += [f"group mol{index} molecule {index + 1}",
                     f"compute mol{index}_kspace mol{index} group/group mol{index} pair no kspace yes"]
    if any(lines.count(line) != 1 for line in expected):
        raise RuntimeError("SP total-energy observations differ from the managed native adaptation")
    if "pe/mol/tally" in text or re.search(r"\bpair_modify\b[^\n]*\btail\s+yes\b", text):
        raise RuntimeError("SP total-only observations cannot use TALLY or tail corrections")
    thermos = [line.split()[2:] for line in lines if line.startswith("thermo_style ")]
    required = {"time", "temp", "vol", "elong", "c_sp_pair_inter", *[f"c_mol{i}_kspace" for i in range(n_chain)]}
    if not thermos or not required <= set(thermos[-1]):
        raise RuntimeError("SP generated thermo omits required total-energy evidence")
    return {"method": "native_SPMD_rerun_total_group_group_adapter", "n_chain": n_chain,
            "decomposition": False, "pair_tail_correction": False, "TALLY_required": False}


def run_sp(request, state, work, directory):
    from . import scientific_runner as runner
    from radonpy.core import utils
    from radonpy.sim.preset.eq import Equilibration
    from radonpy.sim.preset.sp import SPMD
    source = qualified_source(state, work)
    mol = runner.molecule(state, "cell")
    count = utils.count_mols(mol)
    if count != request["n_chain"] or not 2 <= count <= 20:
        raise ValueError("SP requires two to twenty complete polymer chains")
    help_result = subprocess.run([os.environ["LAMMPS_EXEC"], "-h"], check=True, capture_output=True, text=True, timeout=30)
    (directory / "engine-capabilities.stdout").write_text(help_result.stdout + help_result.stderr)
    if not {"group/group", "pppm"} <= set(help_result.stdout.split()):
        raise RuntimeError("SP engine lacks total group/group or PPPM support")
    engine = runner.checked_lammps(directory)
    engine.make_dat(mol, file_name="sample.data")
    preset = Equilibration(mol, work_dir=str(directory), save_dir=str(directory))
    sample = preset.sampling(temp=request["temp"], press=request["pressure"], step=SP_STEPS)
    _prepare(sample, data="sample.data", log="sample.log", last="sample-last", dump="eq0.dump")
    sample_verified = runner.bind_input_verifier(engine, directory, mol, request, "sp_sampling",
        schedule=[segment(SP_STEPS, request["temp"], pressure=request["pressure"])], thermo_steps=1000,
        trajectory_steps=0, bulk_dump_steps=1000, data_name="sample.data",
        extra_validation=lambda text: _check_npt_input(text, [SP_STEPS], pressure=request["pressure"]))
    with runner.progress(work, "sp", directory / "sample.log", SP_STEPS):
        sampled = engine.run(sample, mol=mol, input_file="sample.in", output_file="sample.stdout",
                             omp=0, mpi=runner.mpi_processes(mol.GetNumAtoms()), gpu=0, intel="off", opt="off")
    runner.store_molecule(state, "sp_cell", sampled)
    # Preserve the native preset as the input construction source. Only the
    # total-energy observation commands replace its TALLY decomposition.
    native = SPMD(sampled, work_dir=str(directory), save_dir=str(directory), idx=0)
    rerun = total_sp_workflow(native.rerun(temp=request["temp"], press=request["pressure"],
        log_file="engine.log", dat_file="sample-last.data"), count)
    rerun_verified = runner.bind_input_verifier(engine, directory, sampled, request, "sp_rerun",
        schedule=[], trajectory_steps=0, bulk_dump_steps=0, data_name="sample-last.data", rerun=True,
        extra_validation=lambda current: _verify_sp_rerun(current, count))
    engine.make_input(rerun, file_name="rerun.in")
    text = (directory / "rerun.in").read_text()
    if "pe/mol/tally" in text or "rerun eq0.dump dump x y z ix iy iz vx vy vz" not in text:
        raise RuntimeError("SP input does not match the native total-only rerun adaptation")
    if re.search(r"\brun\s+[1-9]", text) or re.search(r"suffix\s+(omp|intel|gpu|kk)", text):
        raise RuntimeError("SP rerun must have no integration or accelerated pair suffix")
    if re.search(r"\bpair_modify\b[^\n]*\btail\s+yes\b", text) or re.search(r"\bpair_style\b[^\n]*/(omp|intel|gpu|kk)\b", text):
        raise RuntimeError("SP total-only observations require unaccelerated pair styles with tail correction disabled")
    engine.exec(input_file="rerun.in", output_file="rerun.stdout", omp=0,
                mpi=runner.mpi_processes(mol.GetNumAtoms()), gpu=0, intel="off", opt="off")
    conditions = {"temperature_k": request["temp"], "pressure_atm": request["pressure"], "time_step_fs": 1.,
        "sampling_ns": .5, "burn_in_ps": SP_BURN_IN_PS, "sample_interval_ps": 1., "n_chain": count,
        "ensemble": "NPT", "method": "native_SPMD_rerun_total_group_group_adapter",
        "decomposition": False, "source_equilibrium_stage": source,
        "cohesive_definition": "minus_inter_molecular_nonbonded_energy_over_volume",
        "energy_observation": "pair_inter_plus_long_total_minus_sum_chain_long_self",
        "TALLY_required": False, "pair_tail_correction": False,
        "averaging": "mean_of_per_frame_sqrt_CED;matches_native_Solubility_Parameter",
        "trajectory_source": "additional_real_NPT_positions_box_images_velocities"}
    files = [directory / "engine.log", directory / "sample.log", directory / "eq0.dump",
             directory / "sample.data", directory / "sample-last.data", directory / "sample.in", directory / "rerun.in",
             directory / "sample-verification.json", directory / "rerun-verification.json"]
    state["stages"]["sp"] = _metadata(work, files, conditions, source)
    return {"effective_config": {**conditions, "generated_input_verification": {"sampling": sample_verified, "rerun": rerun_verified}},
            "current_step": SP_STEPS, "total_steps": SP_STEPS}


def analyze_sp(log: Path, *, n_chain, conditions=None):
    conditions = dict(conditions or {})
    raw = thermo_blocks(log)[-1]
    required = ["Time", "Temp", "Volume", "E_long", "c_sp_pair_inter", *[f"c_mol{i}_kspace" for i in range(n_chain)]]
    missing = [column for column in required if column not in raw]
    if missing:
        raise ValueError("missing native SP total-energy observations: " + ",".join(missing))
    selected = physical_window(raw, SP_BURN_IN_PS)
    times = selected.Time.to_numpy(float) * .001
    observed = raw.Time.to_numpy(float) * .001
    window = _analysis_range(times, requested_start_ps=float(observed[0] + SP_BURN_IN_PS),
                             observed_start_ps=float(observed[0]), observed_stop_ps=float(observed[-1]))
    reasons, values, errors, traces = [], {"sp_total": None, "sp_ced": None}, {}, {}
    if len(selected) < 100:
        reasons.append("insufficient_SP_production_samples")
    elif (not np.isfinite(selected[required].to_numpy(float)).all() or np.any(selected.Volume.to_numpy(float) <= 0)
          or np.any(selected.Temp.to_numpy(float) <= 0)):
        reasons.append("non_finite_or_non_positive_SP_observations")
    elif (conditions.get("temperature_k") is not None
          and abs(float(selected.Temp.mean()) - conditions["temperature_k"]) > max(5., conditions["temperature_k"] * .05)):
        reasons.append("SP_sampling_temperature_not_reached")
    else:
        inter = selected.c_sp_pair_inter.to_numpy(float) + selected.E_long.to_numpy(float) - selected[[f"c_mol{i}_kspace" for i in range(n_chain)]].sum(axis=1).to_numpy(float)
        ced = -inter * 4184. / 6.02214076e23 / (selected.Volume.to_numpy(float) * 1e-24)
        if not np.isfinite(ced).all() or np.any(ced <= 0):
            reasons.append("cohesive_energy_density_non_finite_or_non_positive")
        else:
            traces = {"sp_ced": ced, "sp_total": np.sqrt(ced)}
            for key, trace in traces.items():
                value, error, blocks = _block_estimate(trace)
                values[key], errors[key] = value, error
                if error is None or error > value * .1 or np.ptp(blocks) > value * .25:
                    reasons.append(key + "_block_stability_not_confirmed")
    quality = "not_converged" if any(value is None for value in values.values()) else "warning" if reasons else "passed"
    properties = [property_record(key, values[key], unit, "RadonPy_SPMD_total_group_group_5_block_SE", "sp", window,
        errors.get(key), quality="passed" if not reasons else "warning", reason=";".join(reasons) or None,
        conditions={**conditions, "block_count": 5, "uncertainty_model": "five_contiguous_time_blocks;not_independent_frames"})
        for key, unit in (("sp_total", "MPa^0.5"), ("sp_ced", "J/cm3"))]
    series = [{"key": key, "label": "溶解度参数" if key == "sp_total" else "内聚能密度", "unit": unit,
               "points": [{"time_ps": float(t), "value": float(value)} for t, value in zip(times, traces[key])][::max(1, len(times) // 300)]}
              for key, unit in (("sp_total", "MPa^0.5"), ("sp_ced", "J/cm3")) if key in traces]
    return {"properties": properties, "series": series, "quality_status": quality,
            "quality": {"reasons": reasons, "analysis_range": window, "block_count": 5}}


def run_tg(request, state, work, directory):
    from . import scientific_runner as runner
    from radonpy.sim.preset.tg import EQMD
    source = qualified_source(state, work)
    mol = runner.molecule(state, "cell")
    engine = runner.checked_lammps(directory)
    engine.make_dat(mol, file_name="input.data")
    preset = EQMD(mol, work_dir=str(directory), save_dir=str(directory), no_traj=True)
    hot = preset.equilibration(temp=TG_HIGH_K, press=request["pressure"], time_step=1.,
                              step=TG_EQUILIBRATION_STEPS, set_init_velocity=True)
    _prepare(hot, data="input.data", log="high_temperature.log", last="high-temperature-last", thermo=TG_THERMO_STEPS)
    hot_verified = runner.bind_input_verifier(engine, directory, mol, request, "tg_heating",
        schedule=[segment(TG_EQUILIBRATION_STEPS, TG_HIGH_K, pressure=request["pressure"])],
        thermo_steps=TG_THERMO_STEPS, trajectory_steps=0, bulk_dump_steps=0, data_name="input.data",
        extra_validation=lambda text: _check_npt_input(text, [TG_EQUILIBRATION_STEPS], pressure=request["pressure"]))
    total = TG_EQUILIBRATION_STEPS + 141 * (TG_STEPS_PER_INTERVAL // 2)
    with runner.progress(work, "tg", directory / "high_temperature.log", total):
        hot_mol = engine.run(hot, mol=mol, input_file="high-temperature.in", output_file="high-temperature.stdout",
                             omp=0, mpi=runner.mpi_processes(mol.GetNumAtoms()), gpu=0, intel="off", opt="off")
    cooling = preset.step_wise(max_temp=TG_HIGH_K, min_temp=TG_LOW_K, press=request["pressure"],
        time_step=1., interval_temp=TG_INTERVAL_K, cooling_rate=TG_STEPS_PER_INTERVAL,
        eq_dat_file="high-temperature-last.data", dat_file="tg-last.data", log_file="engine.log")
    _prepare(cooling, data="high-temperature-last.data", log="engine.log", last="tg-last", thermo=TG_THERMO_STEPS)
    cooling_schedule = []
    for temperature in np.arange(TG_HIGH_K, TG_LOW_K, -TG_INTERVAL_K):
        cooling_schedule += [segment(TG_STEPS_PER_INTERVAL // 2, temperature, pressure=request["pressure"]),
            segment(TG_STEPS_PER_INTERVAL // 2, temperature, temperature - TG_INTERVAL_K, pressure=request["pressure"])]
    cooling_schedule.append(segment(TG_STEPS_PER_INTERVAL // 2, TG_LOW_K, pressure=request["pressure"]))
    cooling_verified = runner.bind_input_verifier(engine, directory, hot_mol, request, "tg_cooling",
        schedule=cooling_schedule, thermo_steps=TG_THERMO_STEPS, trajectory_steps=0, bulk_dump_steps=0,
        data_name="high-temperature-last.data",
        extra_validation=lambda text: _check_npt_input(text, [TG_STEPS_PER_INTERVAL // 2] * 141,
                                                     pressure=request["pressure"]))
    with runner.progress(work, "tg", directory / "engine.log", total):
        cooled = engine.run(cooling, mol=hot_mol, input_file="step-wise.in", output_file="step-wise.stdout",
                            omp=0, mpi=runner.mpi_processes(mol.GetNumAtoms()), gpu=0, intel="off", opt="off")
    runner.store_molecule(state, "tg_cell", cooled)
    conditions = {"temperature_min_k": TG_LOW_K, "temperature_max_k": TG_HIGH_K, "temperature_interval_k": TG_INTERVAL_K,
        "pressure_atm": request["pressure"], "time_step_fs": 1., "plateau_duration_ps": TG_STEPS_PER_INTERVAL / 2000,
        "temperature_interval_duration_ps": TG_STEPS_PER_INTERVAL / 1000, "burn_in_ps": TG_PLATEAU_BURN_IN_PS,
        "heating_equilibration_ns": TG_EQUILIBRATION_STEPS / 1e6, "sampling_ns": 141 * (TG_STEPS_PER_INTERVAL // 2) / 1e6,
        "sample_interval_ps": TG_THERMO_STEPS / 1000, "density_curve_point_count": 71, "ensemble": "NPT",
        "method": "native_EQMD_equilibration_step_wise_with_corrected_density_two_line_analysis",
        "source_equilibrium_stage": source, "nominal_cooling_rate_k_per_ns": TG_INTERVAL_K / (TG_STEPS_PER_INTERVAL / 1e6)}
    files = [directory / "engine.log", directory / "high_temperature.log", directory / "input.data",
             directory / "high-temperature.in", directory / "step-wise.in", directory / "tg-last.data",
             directory / "high-temperature-verification.json", directory / "step-wise-verification.json"]
    state["stages"]["tg"] = _metadata(work, files, conditions, source)
    return {"effective_config": {**conditions, "generated_input_verification": {"heating": hot_verified, "cooling": cooling_verified}},
            "current_step": total, "total_steps": total}


def _two_line_fit(temperatures, densities):
    x, y = np.asarray(temperatures, float), np.asarray(densities, float)
    if len(x) < 20 or len(y) != len(x) or not np.isfinite(x).all() or not np.isfinite(y).all() or np.any(np.diff(x) <= 0) or np.any(y <= 0):
        return None
    candidates = []
    linear = stats.linregress(x, y)
    linear_sse = float(np.sum((y - linear.intercept - linear.slope * x)**2))
    sst = float(np.sum((y - y.mean())**2))
    if sst <= 0 or linear_sse <= 1e-20:
        return None
    for split in range(5, len(x) - 4):
        low, high = stats.linregress(x[:split], y[:split]), stats.linregress(x[split:], y[split:])
        separation = low.slope - high.slope
        if not high.slope < low.slope < 0 or separation <= max(1e-6, abs(high.slope) * .1, 3 * math.hypot(low.stderr, high.stderr)):
            continue
        intersection = (high.intercept - low.intercept) / (low.slope - high.slope)
        if not x[split - 1] <= intersection <= x[split]:
            continue
        fitted = np.r_[low.intercept + low.slope * x[:split], high.intercept + high.slope * x[split:]]
        sse = float(np.sum((y - fitted)**2))
        candidates.append({"intersection_k": float(intersection), "fit_r2": 1 - sse / sst,
            "fit_rmse_g_cm3": math.sqrt(sse / len(x)), "sse": sse, "single_line_sse": linear_sse,
            "low_temperature_slope": float(low.slope), "high_temperature_slope": float(high.slope), "split_index": split})
    if not candidates:
        return None
    best = min(candidates, key=lambda value: value["sse"])
    if best["fit_r2"] < .95 or best["fit_rmse_g_cm3"] > .02 or best["sse"] > linear_sse * .8:
        return None
    return best


def analyze_tg(log: Path, *, conditions=None):
    conditions = dict(conditions or {})
    blocks = thermo_blocks(log)
    expected = np.arange(TG_HIGH_K, TG_LOW_K - TG_INTERVAL_K / 2, -TG_INTERVAL_K)
    reasons, points, means, block_means, windows = [], [], [], [], []
    if len(blocks) != len(expected) * 2 - 1:
        reasons.append("native_step_wise_run_block_count_mismatch")
    else:
        for target, frame in zip(expected, blocks[::2]):
            if not {"Time", "Temp", "Density"} <= set(frame):
                reasons.append("missing_Tg_plateau_observations"); break
            selected = physical_window(frame, TG_PLATEAU_BURN_IN_PS)
            density = selected.Density.to_numpy(float)
            temperature = selected.Temp.to_numpy(float)
            time_ps = selected.Time.to_numpy(float) * .001
            if len(selected) < 50 or not np.isfinite(density).all() or np.any(density <= 0) or not np.isfinite(temperature).all():
                reasons.append("insufficient_or_invalid_Tg_plateau_samples"); break
            if abs(float(temperature.mean()) - target) > max(5., target * .05):
                reasons.append("Tg_plateau_temperature_not_reached"); break
            chunks = np.array_split(density, 5)
            if any(len(chunk) < 10 for chunk in chunks):
                reasons.append("insufficient_Tg_plateau_time_blocks"); break
            estimates = np.asarray([chunk.mean() for chunk in chunks])
            mean = float(density.mean())
            points.append({"temperature_k": float(target), "value": mean})
            means.append(mean); block_means.append(estimates)
            windows.append({"temperature_k": float(target), "start_ps": float(time_ps[0]), "stop_ps": float(time_ps[-1]), "sample_count": len(selected)})
    fit, error = None, None
    if not reasons:
        temperatures = expected[::-1]
        fit = _two_line_fit(temperatures, np.asarray(means)[::-1])
        if fit is None:
            reasons.append("no_supported_in_range_density_slope_transition")
        else:
            repeated = [_two_line_fit(temperatures, np.asarray(block_means)[::-1, index]) for index in range(5)]
            if any(item is None for item in repeated):
                reasons.append("Tg_transition_not_stable_across_time_blocks")
            else:
                intersections = np.asarray([item["intersection_k"] for item in repeated])
                error = float(intersections.std(ddof=1) / math.sqrt(5))
                if error > fit["intersection_k"] * .1:
                    reasons.append("Tg_block_uncertainty_too_large")
    value = fit["intersection_k"] if fit and not reasons else None
    quality = "warning" if value is not None else "not_converged"
    if value is not None:
        reasons.append("cooling_rate_and_independent_seed_sensitivity_not_evaluated")
    window = {"start_ps": windows[0]["start_ps"] if windows else None, "stop_ps": windows[-1]["stop_ps"] if windows else None,
              "sample_count": sum(item["sample_count"] for item in windows), "temperature_min_k": TG_LOW_K, "temperature_max_k": TG_HIGH_K}
    details = {"reasons": reasons, "density_curve_point_count": len(points), "plateau_analysis_windows": windows, **(fit or {})}
    record = property_record("tg", value, "K", "RadonPy_EQMD_step_wise_corrected_density_two_line_5_block_SE", "tg", window,
        error if value is not None else None, quality="warning", reason=";".join(reasons),
        conditions={**conditions, "fit_r2": fit["fit_r2"] if fit else None,
                    "fit_rmse_g_cm3": fit["fit_rmse_g_cm3"] if fit else None,
                    "density_curve_point_count": len(points), "block_count": 5,
                    "uncertainty_model": "five_contiguous_time_blocks_per_temperature;not_independent_seeds"})
    return {"properties": [record], "series": [{"key": "tg_density", "label": "Tg 温度—密度曲线", "unit": "g/cm3", "points": points}],
            "quality_status": quality, "quality": details}

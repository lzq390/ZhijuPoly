from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

NOT_PHYSICAL_WARNING = (
    "Density demo output is not equilibrated and is not a physical density estimate."
)
KJ_PER_MOL_TO_KCAL_PER_MOL = 0.239005736


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a 300-step single-SMILES ByteFF2 density demo, not formal DensityProtocol post-processing."
    )
    parser.add_argument("--byteff2-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--smiles", required=True)
    parser.add_argument("--steps", required=True, type=int)
    parser.add_argument("--report-interval", required=True, type=int)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    byteff2_root = Path(args.byteff2_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        entry = _find_demo_entry(byteff2_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if entry is not None:
        return _run_external_entry(entry, byteff2_root, output_dir, args)

    try:
        _run_builtin_monomer_demo(byteff2_root, output_dir, args)
        _validate_and_annotate_outputs(output_dir, args.job_id, args.smiles, args.steps)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0


def _find_demo_entry(byteff2_root: Path) -> Path | None:
    configured = os.getenv("BYTEFF2_DENSITY_DEMO_ENTRY")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = byteff2_root / path
        if not path.exists():
            raise FileNotFoundError(
                f"BYTEFF2_DENSITY_DEMO_ENTRY does not exist: {path}"
            )
        return path

    candidates = [
        byteff2_root / "example" / "4_MD_simulations" / "run_density_demo.py",
        byteff2_root / "example" / "4_MD_simulations" / "density_demo.py",
        byteff2_root / "scripts" / "run_density_demo.py",
        byteff2_root / "run_density_demo.py",
    ]
    return next((path for path in candidates if path.exists()), None)


def _run_external_entry(entry: Path, byteff2_root: Path, output_dir: Path, args: argparse.Namespace) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(byteff2_root), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    env["MONOMER_MD_DEMO_NOT_EQUILIBRATED"] = "1"

    if os.getenv("BYTEFF2_DENSITY_DEMO_ENTRY_MODE", "cli").strip().lower() == "legacy-env":
        env["RUN_ROOT"] = str(output_dir)
        env["REPO"] = str(byteff2_root)
        command = [sys.executable, str(entry)]
    else:
        command = [
            sys.executable,
            str(entry),
            "--job-id",
            args.job_id,
            "--smiles",
            args.smiles,
            "--steps",
            str(args.steps),
            "--report-interval",
            str(args.report_interval),
            "--output-dir",
            str(output_dir),
        ]
    completed = subprocess.run(command, cwd=byteff2_root, env=env, check=False)
    if completed.returncode != 0:
        return completed.returncode

    try:
        _validate_and_annotate_outputs(output_dir, args.job_id, args.smiles, args.steps)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0


def _run_builtin_monomer_demo(byteff2_root: Path, output_dir: Path, args: argparse.Namespace) -> None:
    sys.path.insert(0, str(byteff2_root))
    import numpy as np
    import openmm as omm
    import openmm.app as app
    import openmm.unit as ou
    import pandas as pd
    from MDAnalysis.lib.formats.libdcd import DCDFile

    from byteff2.md_utils import md_run
    from byteff2.toolkit import protocol

    steps = args.steps
    report_interval = max(1, min(args.report_interval, steps))
    timestep_fs = 2
    temperature_k = int(os.getenv("MONOMER_MD_DEMO_TEMPERATURE_K", "298"))
    natoms = int(os.getenv("MONOMER_MD_DEMO_NATOMS", "1000"))

    def demo_npt_run(top, system, positions, npt_steps=2000000, temperature=300, work_dir="."):
        del npt_steps
        top = copy.deepcopy(top)
        system = copy.deepcopy(system)
        pressure = 1.0 * ou.atmospheres
        frequency = 12
        system.addForce(omm.MonteCarloBarostat(pressure, temperature * ou.kelvin, frequency))
        integrator = omm.MTSLangevinIntegrator(
            temperature * ou.kelvin,
            0.1 / ou.picosecond,
            timestep_fs * ou.femtoseconds,
            [(0, 2), (1, 1)],
        )
        state_reporter = app.StateDataReporter(
            file=os.path.join(work_dir, "npt_state.csv"),
            reportInterval=report_interval,
            step=True,
            time=True,
            potentialEnergy=True,
            kineticEnergy=True,
            totalEnergy=True,
            temperature=True,
            volume=True,
            density=True,
            progress=False,
            remainingTime=False,
            speed=True,
            elapsedTime=False,
            separator=",",
            systemMass=None,
            totalSteps=None,
            append=False,
        )
        dcd_reporter = app.DCDReporter(
            os.path.join(work_dir, "npt.dcd"),
            reportInterval=report_interval,
            enforcePeriodicBox=False,
        )
        return md_run.openmm_run(
            task_name="npt-monomer-demo",
            top=top,
            system=system,
            positions=positions,
            integrator=integrator,
            reporter=[state_reporter, dcd_reporter],
            work_dir=work_dir,
            minimize=True,
            steps=steps,
            temperature=temperature,
        )

    config = {
        "protocol": "Density",
        "params_dir": str(output_dir / "density_demo_params"),
        "output_dir": str(output_dir),
        "working_dir": str(output_dir / "density_demo_working_dir"),
        "temperature": temperature_k,
        "natoms": natoms,
        "components": {"MONOMER": 1},
        "smiles": {"MONOMER": args.smiles},
    }

    original_npt_run = protocol.npt_run
    original_subprocess_run = subprocess.run
    protocol.npt_run = demo_npt_run

    def run_with_gro_box_fix(command, *command_args, **command_kwargs):
        _normalize_run_gmx_inputs(command)
        return original_subprocess_run(command, *command_args, **command_kwargs)

    subprocess.run = run_with_gro_box_fix
    try:
        md_protocol = protocol.DensityProtocol(config)
        original_build_system = md_protocol.build_system

        def build_system_with_capture(total_atoms, components_ratio, working_dir, build_gas=False):
            components = original_build_system(
                total_atoms, components_ratio, working_dir, build_gas=build_gas
            )
            md_protocol.demo_components = components
            return components

        md_protocol.build_system = build_system_with_capture
        start = time.perf_counter()
        md_protocol.run_protocol()
        elapsed = time.perf_counter() - start
    finally:
        protocol.npt_run = original_npt_run
        subprocess.run = original_subprocess_run

    state_path = output_dir / "npt_state.csv"
    df = pd.read_csv(state_path)
    density = _series_from_dataframe(df, fields=("Density (g/mL)", "density_g_cm3"))
    temperature = _series_from_dataframe(df, fields=("Temperature (K)", "temperature_k"))
    energy = _series_from_dataframe(
        df,
        fields=("Total Energy (kJ/mole)", "Total Energy (kJ/mol)", "total_energy_kj_mol"),
        value_multiplier=KJ_PER_MOL_TO_KCAL_PER_MOL,
    )
    components = getattr(md_protocol, "demo_components", {})
    molecule_counts = {name: int(component.molar_num) for name, component in components.items()}
    actual_atoms = sum(
        int(component.molar_num) * len(component.atoms) for component in components.values()
    )

    result = {
        "demo": True,
        "mode": "builtin-monomer-density-demo",
        "protocol": f"DensityProtocol with {steps}-step NPT demo patch",
        "elapsed_seconds": elapsed,
        "npt_steps": steps,
        "steps": steps,
        "report_interval": report_interval,
        "sample_count": int(len(df)),
        "dcd_frame_count": _count_dcd_frames(output_dir / "npt.dcd", DCDFile),
        "simulated_time_ps": steps * timestep_fs * 1e-3,
        "timestep_fs": timestep_fs,
        "temperature": temperature_k,
        "configured_natoms": natoms,
        "actual_atoms": int(actual_atoms),
        "molecule_counts": molecule_counts,
        "summary": _summary(density, temperature, energy, steps, len(df), actual_atoms, elapsed),
        "density_series": {"key": "density", "label": "Density", "unit": "g/cm3", "points": density},
        "temperature_series": {"key": "temperature", "label": "Temperature", "unit": "K", "points": temperature},
        "energy_series": {"key": "energy", "label": "Total energy", "unit": "kcal/mol", "points": energy},
        "trajectory_preview": None,
        "artifacts": {
            "density_demo_results_json": {"name": "density_demo_results_json", "path": "density_demo_results.json", "kind": "json"},
            "npt_state_csv": {"name": "npt_state_csv", "path": "npt_state.csv", "kind": "csv"},
            "npt_dcd": {"name": "npt_dcd", "path": "npt.dcd", "kind": "dcd"},
        },
        "outputs": {
            "density_demo_results_json": "density_demo_results.json",
            "npt_state_csv": "npt_state.csv",
            "npt_dcd": "npt.dcd",
        },
        "warnings": [NOT_PHYSICAL_WARNING],
    }
    with (output_dir / "density_demo_results.json").open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _normalize_run_gmx_inputs(command: Any) -> None:
    if not isinstance(command, str) or "run_gmx.sh" not in command:
        return
    try:
        tokens = shlex.split(command)
    except ValueError:
        return
    if len(tokens) < 4 or tokens[0] != "cd" or "run_gmx.sh" not in tokens:
        return
    _ensure_gro_box_lines(Path(tokens[1]))


def _ensure_gro_box_lines(working_dir: Path) -> None:
    for gro_path in working_dir.glob("*.gro"):
        _ensure_gro_box_line(gro_path)


def _ensure_gro_box_line(gro_path: Path) -> None:
    lines = gro_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return
    try:
        atom_count = int(lines[1].strip())
    except ValueError:
        return
    box_index = atom_count + 2
    if len(lines) < box_index:
        return
    fixed_lines = lines[:2]
    for line in lines[2:box_index]:
        fixed_lines.append(_normalize_gro_atom_line(line))

    box_nm = float(os.getenv("MONOMER_MD_DEMO_INPUT_BOX_NM", "10.0"))
    box_line = f"{box_nm:10.5f}{box_nm:10.5f}{box_nm:10.5f}"
    if len(lines) > box_index and lines[box_index].strip():
        fixed_lines.append(lines[box_index])
    else:
        fixed_lines.append(box_line)
    if len(lines) > box_index + 1:
        fixed_lines.extend(lines[box_index + 1 :])
    gro_path.write_text("\n".join(fixed_lines) + "\n", encoding="utf-8")


def _normalize_gro_atom_line(line: str) -> str:
    parts = line.split()
    if len(parts) < 6:
        return line
    residue = parts[0]
    residue_id_text = ""
    residue_name = ""
    for index, char in enumerate(residue):
        if not char.isdigit():
            residue_id_text = residue[:index]
            residue_name = residue[index:]
            break
    if not residue_id_text or not residue_name:
        return line
    try:
        residue_id = int(residue_id_text)
        atom_name = parts[1]
        atom_id = int(parts[2])
        x = float(parts[3])
        y = float(parts[4])
        z = float(parts[5])
        velocities = [float(value) for value in parts[6:9]]
    except ValueError:
        return line

    normalized = (
        f"{residue_id % 100000:5d}"
        f"{residue_name[:5]:<5}"
        f"{atom_name[:5]:>5}"
        f"{atom_id % 100000:5d}"
        f"{x:8.3f}{y:8.3f}{z:8.3f}"
    )
    if len(velocities) == 3:
        normalized += f"{velocities[0]:8.4f}{velocities[1]:8.4f}{velocities[2]:8.4f}"
    return normalized


def _series_from_dataframe(df: Any, *, fields: tuple[str, ...], value_multiplier: float = 1.0) -> list[dict[str, Any]]:
    import numpy as np
    import pandas as pd

    value_column = next((field for field in fields if field in df.columns), None)
    if value_column is None:
        return []
    step_column = next((field for field in ("#\"Step\"", "\"Step\"", "Step", "step") if field in df.columns), None)
    time_column = next((field for field in ("Time (ps)", "time_ps") if field in df.columns), None)
    values = pd.to_numeric(df[value_column], errors="coerce")
    steps = pd.to_numeric(df[step_column], errors="coerce") if step_column else None
    times = pd.to_numeric(df[time_column], errors="coerce") if time_column else None
    points: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        if not np.isfinite(value):
            continue
        point: dict[str, Any] = {"value": _finite_float(value * value_multiplier)}
        if steps is not None and np.isfinite(steps.iloc[index]):
            point["step"] = int(steps.iloc[index])
        else:
            point["frame"] = index
        if times is not None and np.isfinite(times.iloc[index]):
            point["time_ps"] = _finite_float(times.iloc[index])
        points.append(point)
    return points


def _summary(
    density: list[dict[str, Any]],
    temperature: list[dict[str, Any]],
    energy: list[dict[str, Any]],
    steps: int,
    sample_count: int,
    actual_atoms: int,
    elapsed_seconds: float,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "n_steps": steps,
        "sample_count": sample_count,
        "n_frames": sample_count,
        "n_atoms": actual_atoms,
        "elapsed_seconds": _finite_float(elapsed_seconds),
    }
    _add_series_stats(summary, density, "density_g_cm3")
    _add_series_stats(summary, temperature, "temperature_k")
    _add_series_stats(summary, energy, "total_energy_kcal_mol")
    return summary


def _add_series_stats(summary: dict[str, Any], points: list[dict[str, Any]], key: str) -> None:
    values = [point["value"] for point in points if isinstance(point.get("value"), (int, float))]
    if not values:
        return
    summary[f"final_{key}"] = _finite_float(values[-1])
    summary[f"mean_{key}"] = _finite_float(sum(values) / len(values))


def _count_dcd_frames(path: Path, dcd_file_cls: Any) -> int:
    count = 0
    with dcd_file_cls(str(path)) as dcd:
        for _frame in dcd:
            count += 1
    return count


def _finite_float(value: Any) -> float | None:
    import math

    value = float(value)
    return value if math.isfinite(value) else None


def _validate_and_annotate_outputs(
    output_dir: Path, job_id: str, smiles: str, steps: int
) -> None:
    required_files = [
        output_dir / "density_demo_results.json",
        output_dir / "npt_state.csv",
        output_dir / "npt.dcd",
    ]
    missing = [path.name for path in required_files if not path.exists()]
    if missing:
        raise RuntimeError("density demo did not produce: " + ", ".join(missing))

    result_path = output_dir / "density_demo_results.json"
    with result_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    warnings = list(payload.get("warnings") or [])
    if NOT_PHYSICAL_WARNING not in warnings:
        warnings.append(NOT_PHYSICAL_WARNING)
    payload.update(
        {
            "job_id": job_id,
            "smiles": smiles,
            "steps": steps,
            "not_equilibrated": True,
            "physical_density_estimate": False,
            "warnings": warnings,
            "outputs": {
                "density_demo_results_json": "density_demo_results.json",
                "npt_state_csv": "npt_state.csv",
                "npt_dcd": "npt.dcd",
            },
        }
    )
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())

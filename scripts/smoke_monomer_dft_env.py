#!/usr/bin/env python3
"""GPU smoke test for the isolated AIMNet2 monomer DFT runtime."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import socket
import stat
import sys
import time
import uuid
from typing import Any

import preflight_monomer_dft_env as preflight


CCO_BASELINE_EV = -4221.547007510834
CCO_TOLERANCE_EV = 0.001
WATER_COORDINATES = (
    (0.0000, 0.0000, 0.1173),
    (0.0000, 0.7572, -0.4692),
    (0.0000, -0.7572, -0.4692),
)
WATER_NUMBERS = (8, 1, 1)
PRODUCTION_REPO_ROOT = pathlib.Path("/data/lzq/gith/nexpoly")


def prepare_runtime(repo_root: pathlib.Path) -> dict[str, Any]:
    repo_root = preflight.require_development_repo_root(repo_root)
    preflight.require(not os.environ.get("PYTHONPATH"), "inherited PYTHONPATH must be unset or empty")
    values, formal_gpu_authority = preflight.effective_environment(
        repo_root,
        preflight.load_env_file(repo_root / ".env.monomer-dft.dev"),
    )
    resolved = preflight.validate_environment(
        repo_root,
        values,
        formal_gpu_authority,
    )
    git_result = preflight.validate_git(repo_root)

    broker_enabled = values["MONOMER_DFT_GPU_BROKER_ENABLED"] == "1"
    inherited_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    preflight.require(
        inherited_visible in ((None, "") if broker_enabled else (None, "", values["NEXPOLY_DFT_GPU_DEVICE"])),
        "Broker smoke must be CUDA-blind" if broker_enabled else f"CUDA_VISIBLE_DEVICES conflicts with physical GPU {values['NEXPOLY_DFT_GPU_DEVICE']}",
    )
    inherited_order = os.environ.get("CUDA_DEVICE_ORDER")
    preflight.require(inherited_order in (None, "", "PCI_BUS_ID"), "CUDA_DEVICE_ORDER conflicts with PCI_BUS_ID")
    for key, value in values.items():
        if key != "PYTHONPATH":
            os.environ[key] = value
    for key, value in resolved.items():
        if key in values:
            os.environ[key] = value
    if broker_enabled:
        os.environ.pop("CUDA_VISIBLE_DEVICES", None)
    else:
        os.environ["CUDA_VISIBLE_DEVICES"] = values["NEXPOLY_DFT_GPU_DEVICE"]
    os.environ.pop("PYTHONPATH", None)
    sys.dont_write_bytecode = True

    gpu_result = preflight.validate_physical_gpu(values["NEXPOLY_DFT_GPU_DEVICE"])
    lock, source_result = preflight.validate_source_lock(repo_root, resolved)
    python_result = preflight.validate_python_and_models(
        repo_root,
        resolved,
        lock,
        values["NEXPOLY_DFT_GPU_DEVICE"],
        initialize_cuda=not broker_enabled,
    )
    default_model = next((model for model in lock["models"] if model["alias"] == "aimnet2"), None)
    preflight.require(default_model is not None, "AIMNet lock does not define the aimnet2 alias")
    default_model_path = pathlib.Path(resolved["AIMNET_CACHE_DIR"]) / default_model["file"]
    preflight.require(default_model_path.is_file(), "locked aimnet2 checkpoint disappeared after preflight")
    return {
        "git": git_result,
        "source": source_result,
        "gpu": gpu_result,
        "runtime": python_result,
        "default_model_path": str(default_model_path),
        "broker_enabled": broker_enabled,
        "formal_gpu_authority": formal_gpu_authority is not None,
        "worker_uds": resolved["MONOMER_DFT_WORKER_UDS"],
    }


def _uds_json(
    uds: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else b""
    )
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: monomer-dft-worker\r\n"
        "Connection: close\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii") + body
    chunks: list[bytes] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30.0)
        connection.connect(uds)
        connection.sendall(request)
        while chunk := connection.recv(1024 * 1024):
            chunks.append(chunk)
    header, separator, response_body = b"".join(chunks).partition(b"\r\n\r\n")
    preflight.require(bool(separator), "Worker smoke returned malformed HTTP")
    status = int(header.split(b" ", 2)[1])
    decoded = json.loads(response_body)
    preflight.require(isinstance(decoded, dict), "Worker smoke response is not an object")
    return status, decoded


def run_broker_worker_smoke(preflight_result: dict[str, Any]) -> dict[str, Any]:
    uds = str(preflight_result["worker_uds"])
    status, health = _uds_json(uds, "GET", "/health")
    preflight.require(status == 200 and health.get("runtime_ready") is True, "registered residency executor is not ready")
    models = health.get("runtime", {}).get("models", {})
    preflight.require(isinstance(models, dict) and len(models) == 6, "residency executor did not preload all six models")

    identity = uuid.uuid4().hex
    payload = {
        "schema_version": 2,
        "enqueue_sequence": time.time_ns(),
        "job_id": f"broker-smoke-{identity}",
        "attempt_token": identity,
        "input": {"smiles": "CCO", "net_charge": 0, "multiplicity": 1, "psmiles_mode": None},
        "calculation_type": "single_point",
        "model": "aimnet2",
        "conformer": {"seed": 1, "max_iterations": 500},
        "single_point": {"properties": ["energy", "forces", "charges"]},
    }
    status, job = _uds_json(uds, "POST", "/jobs", payload)
    preflight.require(status in {200, 202}, f"Broker Worker smoke submit failed: HTTP {status}")
    deadline = time.monotonic() + 120.0
    while job.get("status") not in {"completed", "failed", "cancelled"}:
        preflight.require(time.monotonic() < deadline, "Broker Worker smoke timed out")
        time.sleep(0.25)
        status, job = _uds_json(uds, "GET", f"/jobs/{payload['job_id']}")
        preflight.require(status == 200, f"Broker Worker smoke poll failed: HTTP {status}")
    preflight.require(job.get("status") == "completed", f"Broker Worker smoke ended as {job.get('status')}")
    result = job.get("result")
    preflight.require(isinstance(result, dict) and result.get("schema_version") == 2, "Broker Worker smoke result contract is invalid")
    provenance = result.get("provenance", {})
    preflight.require(provenance.get("gpu_uuid") in preflight.EXPECTED_GPU_UUIDS.values(), "Broker Worker smoke lacks fenced GPU provenance")
    return {"status": "ok", "mode": "broker_worker_uds", "health": health, "job": job}


def require_finite(torch: Any, value: Any, name: str) -> None:
    preflight.require(bool(torch.isfinite(value).all().item()), f"{name} contains a non-finite value")


def run_calculations(preflight_result: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    import torch
    from aimnet.calculators import AIMNet2ASE, AIMNet2Calculator
    from ase import Atoms
    from rdkit import Chem
    from rdkit.Chem import AllChem

    started = time.perf_counter()
    calculator = AIMNet2Calculator(preflight_result["default_model_path"], device="cuda:0")

    water_input = {
        "coord": torch.tensor(WATER_COORDINATES, dtype=torch.float32),
        "numbers": torch.tensor(WATER_NUMBERS, dtype=torch.long),
        "charge": 0.0,
    }
    water = calculator(water_input, forces=True, hessian=True)
    torch.cuda.synchronize()
    for key in ("energy", "charges", "forces", "hessian"):
        preflight.require(key in water, f"water calculation did not return {key}")
        require_finite(torch, water[key], f"water {key}")
    preflight.require(tuple(water["charges"].shape) == (3,), f"unexpected charge shape: {water['charges'].shape}")
    preflight.require(tuple(water["forces"].shape) == (3, 3), f"unexpected force shape: {water['forces'].shape}")
    preflight.require(tuple(water["hessian"].shape) == (3, 3, 3, 3), f"unexpected Hessian shape: {water['hessian'].shape}")

    charge_sum = float(water["charges"].sum().detach().cpu())
    preflight.require(abs(charge_sum) <= 1.0e-4, f"water partial charges do not sum to zero: {charge_sum}")
    hessian_flat = water["hessian"].reshape(9, 9)
    hessian_symmetry_error = float((hessian_flat - hessian_flat.T).abs().max().detach().cpu())
    preflight.require(hessian_symmetry_error < 1.0e-3, f"water Hessian is asymmetric: {hessian_symmetry_error}")

    atoms = Atoms(numbers=WATER_NUMBERS, positions=WATER_COORDINATES)
    atoms.calc = AIMNet2ASE(calculator, charge=0)
    ase_energy = float(atoms.get_potential_energy())
    ase_forces = atoms.get_forces()
    preflight.require(np.isfinite(ase_energy), "ASE energy is non-finite")
    preflight.require(ase_forces.shape == (3, 3) and np.isfinite(ase_forces).all(), "ASE forces are invalid")
    water_energy = float(water["energy"].reshape(-1)[0].detach().cpu())
    preflight.require(abs(ase_energy - water_energy) < 1.0e-4, "ASE/direct water energies disagree")

    molecule = Chem.MolFromSmiles("CCO")
    preflight.require(molecule is not None, "RDKit could not parse CCO")
    molecule = Chem.AddHs(molecule)
    params = AllChem.ETKDGv3()
    params.randomSeed = 1
    embed_status = AllChem.EmbedMolecule(molecule, params)
    if embed_status != 0:
        embed_status = AllChem.EmbedMolecule(molecule, randomSeed=1, useRandomCoords=True)
    preflight.require(embed_status == 0, "RDKit could not embed CCO")
    if AllChem.MMFFHasAllMoleculeParams(molecule):
        AllChem.MMFFOptimizeMolecule(molecule, mmffVariant="MMFF94", maxIters=500)
        force_field = "MMFF94"
    else:
        AllChem.UFFOptimizeMolecule(molecule, maxIters=500)
        force_field = "UFF"

    conformer = molecule.GetConformer()
    cco_coordinates = [
        [
            conformer.GetAtomPosition(atom.GetIdx()).x,
            conformer.GetAtomPosition(atom.GetIdx()).y,
            conformer.GetAtomPosition(atom.GetIdx()).z,
        ]
        for atom in molecule.GetAtoms()
    ]
    cco_numbers = [atom.GetAtomicNum() for atom in molecule.GetAtoms()]
    cco = calculator(
        {
            "coord": torch.tensor(cco_coordinates, dtype=torch.float32),
            "numbers": torch.tensor(cco_numbers, dtype=torch.long),
            "charge": 0.0,
        },
        forces=False,
    )
    torch.cuda.synchronize()
    require_finite(torch, cco["energy"], "CCO energy")
    cco_energy = float(cco["energy"].reshape(-1)[0].detach().cpu())
    cco_delta = cco_energy - CCO_BASELINE_EV
    preflight.require(
        abs(cco_delta) <= CCO_TOLERANCE_EV,
        f"CCO baseline mismatch: {cco_energy:.12f} eV (delta {cco_delta:+.12f} eV)",
    )

    elapsed = time.perf_counter() - started
    return {
        "status": "ok",
        "preflight": preflight_result,
        "model": "aimnet2",
        "device": "cuda:0",
        "water": {
            "numbers": list(WATER_NUMBERS),
            "coordinates_angstrom": [list(row) for row in WATER_COORDINATES],
            "energy_eV": water_energy,
            "charge_sum_e": charge_sum,
            "max_force_eV_per_A": float(torch.linalg.vector_norm(water["forces"], dim=1).max().detach().cpu()),
            "forces_shape": list(water["forces"].shape),
            "hessian_shape": list(water["hessian"].shape),
            "hessian_symmetry_max_abs_eV_per_A2": hessian_symmetry_error,
        },
        "ase": {
            "energy_eV": ase_energy,
            "max_force_eV_per_A": float(np.linalg.norm(ase_forces, axis=1).max()),
        },
        "cco": {
            "smiles": "CCO",
            "canonical_smiles": Chem.MolToSmiles(Chem.RemoveHs(molecule)),
            "seed": 1,
            "rdkit_max_iters": 500,
            "force_field": force_field,
            "atom_count": len(cco_numbers),
            "energy_eV": cco_energy,
            "baseline_eV": CCO_BASELINE_EV,
            "delta_eV": cco_delta,
            "tolerance_eV": CCO_TOLERANCE_EV,
        },
        "elapsed_seconds": elapsed,
    }


def _ensure_private_directory(path: pathlib.Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        metadata = path.lstat()
    else:
        preflight.require(
            stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode),
            f"smoke runtime path must be a real directory: {path}",
        )
    preflight.require(
        metadata.st_uid == os.geteuid(),
        f"smoke runtime path must be owned by the current uid: {path}",
    )
    path.chmod(0o700)


def write_report(
    runtime_root: pathlib.Path,
    report: dict[str, Any],
    stamp: str,
) -> pathlib.Path:
    preflight.require(
        runtime_root == runtime_root.parent / ".runtime",
        "smoke runtime root must be the current worktree .runtime",
    )
    _ensure_private_directory(runtime_root)
    run_root = runtime_root / "runs"
    _ensure_private_directory(run_root)
    output = run_root / f"smoke-{stamp}-{os.getpid()}.json"
    temporary = run_root / f".{output.name}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, output)
    directory_fd = os.open(run_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return output


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    if repo_root == PRODUCTION_REPO_ROOT:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": "development DFT smoke is forbidden in the production repository",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    runtime_root = repo_root / ".runtime"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        preflight_result = prepare_runtime(repo_root)
        report = (
            run_broker_worker_smoke(preflight_result)
            if preflight_result["broker_enabled"]
            else run_calculations(preflight_result)
        )
        report["created_at"] = dt.datetime.now(dt.UTC).isoformat()
        output = write_report(runtime_root, report, stamp)
        print(json.dumps({"status": "ok", "report": str(output)}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:  # noqa: BLE001 - the report is the smoke-test boundary
        report = {
            "status": "error",
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        try:
            output = write_report(runtime_root, report, stamp)
        except Exception as report_exc:  # noqa: BLE001 - fail without an unsafe write
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": str(exc),
                        "report_error": str(report_exc),
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        print(json.dumps({"status": "error", "report": str(output), "error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

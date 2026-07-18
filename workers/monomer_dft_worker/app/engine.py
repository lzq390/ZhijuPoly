from __future__ import annotations

import csv
import io
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np

from .artifacts import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_npz,
    describe_artifact,
    ensure_private_directory,
    write_xyz,
)
from .chemistry import (
    ChemistryValidationError,
    prepare_molecule,
    validate_model_domain,
)
from .schemas import (
    ArtifactDescriptor,
    GpuExecutionProvenanceV2,
    JobSubmitRequest,
)


ProgressCallback = Callable[[str, int, str | None], None]
CancellationCheck = Callable[[], bool]
AdmissionCallback = Callable[[], float | None]

# Treat a geometry as numerically linear when its smallest principal moment is
# below one part per million of the largest. This is scale-invariant and maps
# to sub-milliradian transverse distortions, while keeping physically bent
# structures in the nonlinear (three-rotation) branch.
LINEAR_INERTIA_RELATIVE_TOLERANCE = 1.0e-6


class ComputationCancelled(RuntimeError):
    pass


class ScientificComputationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


@dataclass(slots=True)
class OptimizationOutcome:
    coordinates_angstrom: np.ndarray
    converged: bool
    steps: int
    trace: list[dict[str, Any]]


class ComputeBackend(Protocol):
    def synchronize(self) -> None: ...

    def evaluate(
        self,
        *,
        model: str,
        atomic_numbers: np.ndarray,
        coordinates_angstrom: np.ndarray,
        net_charge: int,
        multiplicity: int,
        forces: bool,
        hessian: bool,
    ) -> dict[str, Any]: ...

    def optimize(
        self,
        *,
        model: str,
        atomic_numbers: np.ndarray,
        coordinates_angstrom: np.ndarray,
        net_charge: int,
        multiplicity: int,
        fmax_eV_per_A: float,
        max_steps: int,
        progress: Callable[[int, float, float], None],
        cancelled: CancellationCheck,
    ) -> OptimizationOutcome: ...


@dataclass(frozen=True, slots=True)
class EngineExecution:
    result: dict[str, Any]
    timings: dict[str, float]
    artifacts: tuple[tuple[ArtifactDescriptor, Path], ...]


class AimnetComputeBackend:
    """Thin adapter around the preloaded, process-local AIMNet calculators."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def synchronize(self) -> None:
        self.runtime.synchronize()

    def evaluate(
        self,
        *,
        model: str,
        atomic_numbers: np.ndarray,
        coordinates_angstrom: np.ndarray,
        net_charge: int,
        multiplicity: int,
        forces: bool,
        hessian: bool,
    ) -> dict[str, Any]:
        import torch

        calculator = self.runtime.calculator_for(model)
        device = calculator.device
        payload = {
            "coord": torch.as_tensor(
                coordinates_angstrom,
                dtype=calculator.keys_in["coord"],
                device=device,
            ),
            "numbers": torch.as_tensor(atomic_numbers, dtype=torch.long, device=device),
            "charge": torch.as_tensor(
                float(net_charge), dtype=torch.float32, device=device
            ),
            # AIMNet2-NSE consumes ``mult``. Closed-shell models safely receive 1.
            "mult": torch.as_tensor(
                float(multiplicity), dtype=torch.float32, device=device
            ),
        }
        raw = calculator(
            payload,
            forces=forces or hessian,
            hessian=hessian,
            validate_species=True,
        )
        converted: dict[str, Any] = {}
        for name, value in raw.items():
            if torch.is_tensor(value):
                converted[name] = value.detach().cpu().numpy()
            else:
                converted[name] = value
        return converted

    def optimize(
        self,
        *,
        model: str,
        atomic_numbers: np.ndarray,
        coordinates_angstrom: np.ndarray,
        net_charge: int,
        multiplicity: int,
        fmax_eV_per_A: float,
        max_steps: int,
        progress: Callable[[int, float, float], None],
        cancelled: CancellationCheck,
    ) -> OptimizationOutcome:
        from aimnet.calculators import AIMNet2ASE
        from ase import Atoms
        from ase.optimize import BFGS

        atoms = Atoms(numbers=atomic_numbers, positions=coordinates_angstrom)
        atoms.info["charge"] = int(net_charge)
        atoms.info["mult"] = int(multiplicity)
        atoms.calc = AIMNet2ASE(
            self.runtime.calculator_for(model),
            charge=net_charge,
            mult=multiplicity,
        )
        trace: list[dict[str, Any]] = []
        optimizer = BFGS(atoms, logfile=None)

        def observe() -> None:
            if cancelled():
                raise ComputationCancelled("job cancellation was requested")
            energy = float(atoms.get_potential_energy())
            force_array = np.asarray(atoms.get_forces(), dtype=np.float64)
            charges = np.asarray(atoms.get_charges(), dtype=np.float64).reshape(-1)
            fmax = _maximum_force(force_array)
            trace.append(
                {
                    "step": int(optimizer.nsteps),
                    "energy_eV": energy,
                    "fmax_eV_per_A": fmax,
                    "coordinates_angstrom": np.asarray(
                        atoms.get_positions(), dtype=np.float64
                    ).tolist(),
                    "charges_e": charges.tolist(),
                }
            )
            progress(int(optimizer.nsteps), energy, fmax)

        optimizer.attach(observe, interval=1)
        if cancelled():
            raise ComputationCancelled("job cancellation was requested")
        converged = bool(optimizer.run(fmax=fmax_eV_per_A, steps=max_steps))
        if not trace or trace[-1]["step"] != int(optimizer.nsteps):
            observe()
        return OptimizationOutcome(
            coordinates_angstrom=np.asarray(atoms.get_positions(), dtype=np.float64),
            converged=converged,
            steps=int(optimizer.nsteps),
            trace=trace,
        )


class ScientificEngine:
    def __init__(self, backend: ComputeBackend) -> None:
        self.backend = backend

    def execute(
        self,
        request: JobSubmitRequest,
        output_directory: Path,
        *,
        admitted: AdmissionCallback | None = None,
        progress: ProgressCallback,
        cancelled: CancellationCheck,
        provenance: dict[str, Any] | None = None,
        queue_wait_ms: float = 0.0,
        execution_timings: dict[str, float] | None = None,
    ) -> EngineExecution:
        started = time.perf_counter()
        execution_timings = execution_timings or {}
        timings: dict[str, float] = {
            "queue_wait_ms": max(0.0, float(queue_wait_ms)),
            "gpu_wait_ms": max(0.0, float(execution_timings.get("gpu_wait_ms", 0.0))),
            "model_load_ms": max(
                0.0, float(execution_timings.get("model_load_ms", 0.0))
            ),
            "structure_prepare_ms": 0.0,
            "model_compute_ms": 0.0,
            "optimization_ms": 0.0,
            "hessian_ms": 0.0,
            "frequency_ms": 0.0,
            "artifact_ms": 0.0,
            "total_ms": 0.0,
        }
        artifacts: list[tuple[ArtifactDescriptor, Path]] = []
        ensure_private_directory(output_directory)

        if admitted is not None:
            admitted()
        self._check_cancelled(cancelled)
        progress("conformer", 5, "Generating a deterministic RDKit conformer")
        stage_started = time.perf_counter()
        molecule = prepare_molecule(
            request.input,
            seed=request.conformer.seed,
            max_iters=request.conformer.max_iterations,
        )
        net_charge = molecule.net_charge
        validate_model_domain(
            request.model,
            molecule.atomic_numbers,
            charge=net_charge,
            multiplicity=request.input.multiplicity,
        )
        timings["structure_prepare_ms"] = (time.perf_counter() - stage_started) * 1000.0
        atom_count = len(molecule.atomic_numbers)
        heavy_atom_count = int(np.count_nonzero(molecule.atomic_numbers > 1))
        if heavy_atom_count > 100 or atom_count > 300:
            raise ChemistryValidationError(
                "molecule_too_large",
                "the worker accepts at most 100 heavy atoms and 300 total atoms",
                details={
                    "heavy_atom_count": heavy_atom_count,
                    "atom_count": atom_count,
                },
            )
        if "hessian" in request.properties and atom_count > 100:
            raise ChemistryValidationError(
                "hessian_molecule_too_large",
                "Hessian and frequency jobs accept at most 100 total atoms",
                details={"atom_count": atom_count},
            )

        initial_xyz = output_directory / "initial_structure.xyz"
        write_xyz(
            initial_xyz,
            symbols=molecule.symbols,
            coordinates=molecule.coordinates_angstrom,
            comment=f"RDKit {molecule.rdkit_force_field}; {molecule.canonical_smiles}",
        )
        artifacts.append(
            (
                describe_artifact(
                    artifact_id="initial_structure",
                    path=initial_xyz,
                    media_type="chemical/x-xyz",
                ),
                initial_xyz,
            )
        )

        coordinates = molecule.coordinates_angstrom.copy()
        optimization_result: dict[str, Any] | None = None
        convergence_threshold = 0.01
        if request.calculation_type == "optimization":
            assert request.optimization is not None
            convergence_threshold = request.optimization.fmax_eV_per_A
            progress("optimization", 15, "Running AIMNet2 BFGS geometry optimization")
            self._synchronize_backend()
            stage_started = time.perf_counter()

            def optimization_progress(step: int, energy: float, fmax: float) -> None:
                fraction = min(1.0, step / request.optimization.max_steps)
                progress(
                    "optimization",
                    15 + int(55 * fraction),
                    f"BFGS step {step}: energy={energy:.8f} eV, fmax={fmax:.6f} eV/A",
                )

            outcome = self.backend.optimize(
                model=request.model,
                atomic_numbers=molecule.atomic_numbers,
                coordinates_angstrom=coordinates,
                net_charge=net_charge,
                multiplicity=request.input.multiplicity,
                fmax_eV_per_A=request.optimization.fmax_eV_per_A,
                max_steps=request.optimization.max_steps,
                progress=optimization_progress,
                cancelled=cancelled,
            )
            self._synchronize_backend()
            coordinates = np.asarray(outcome.coordinates_angstrom, dtype=np.float64)
            if coordinates.shape != molecule.coordinates_angstrom.shape:
                raise ScientificComputationError(
                    "invalid_result_shape",
                    "geometry optimization returned an invalid coordinate shape",
                )
            _require_finite("optimized coordinates", coordinates)
            timings["optimization_ms"] = (time.perf_counter() - stage_started) * 1000.0
            optimization_result = {
                "converged": outcome.converged,
                "steps": outcome.steps,
                "fmax_threshold_eV_per_A": request.optimization.fmax_eV_per_A,
                "max_steps": request.optimization.max_steps,
                "trace": outcome.trace,
            }
            trace_path = output_directory / "optimization_trajectory.json"
            atomic_write_json(
                trace_path,
                {
                    "schema_version": 1,
                    "units": {
                        "energy": "eV",
                        "fmax": "eV/angstrom",
                        "coordinates": "angstrom",
                        "charges": "e",
                    },
                    "frames": outcome.trace,
                },
            )
            artifacts.append(
                (
                    describe_artifact(
                        artifact_id="optimization_trajectory",
                        path=trace_path,
                        media_type="application/json",
                    ),
                    trace_path,
                )
            )

        self._check_cancelled(cancelled)
        requested_properties = set(request.properties)
        need_hessian = "hessian" in requested_properties
        progress(
            "hessian" if need_hessian else "single_point",
            75 if request.calculation_type == "optimization" else 25,
            "Evaluating AIMNet2 properties",
        )
        self._synchronize_backend()
        stage_started = time.perf_counter()
        evaluated = self.backend.evaluate(
            model=request.model,
            atomic_numbers=molecule.atomic_numbers,
            coordinates_angstrom=coordinates,
            net_charge=net_charge,
            multiplicity=request.input.multiplicity,
            forces="forces" in requested_properties,
            hessian=False,
        )
        self._synchronize_backend()
        timings["model_compute_ms"] = (time.perf_counter() - stage_started) * 1000.0
        self._check_cancelled(cancelled)

        if need_hessian:
            progress("hessian", 82, "Calculating the AIMNet2 Cartesian Hessian")
            self._synchronize_backend()
            stage_started = time.perf_counter()
            hessian_evaluation = self.backend.evaluate(
                model=request.model,
                atomic_numbers=molecule.atomic_numbers,
                coordinates_angstrom=coordinates,
                net_charge=net_charge,
                multiplicity=request.input.multiplicity,
                forces=True,
                hessian=True,
            )
            self._synchronize_backend()
            timings["hessian_ms"] = (time.perf_counter() - stage_started) * 1000.0
            if "hessian" not in hessian_evaluation:
                raise ScientificComputationError(
                    "missing_result", "AIMNet2 did not return the requested Hessian"
                )
            evaluated["hessian"] = hessian_evaluation["hessian"]
            self._check_cancelled(cancelled)

        scientific = _normalize_evaluation(
            evaluated,
            atom_count=len(molecule.atomic_numbers),
            net_charge=net_charge,
            required_properties=set(request.properties),
        )
        forces = scientific.get("forces_eV_per_A")
        fmax = (
            _maximum_force(np.asarray(forces, dtype=np.float64))
            if forces is not None
            else None
        )

        vibration_result: dict[str, Any] | None = None
        if need_hessian:
            hessian = np.asarray(scientific.pop("hessian_eV_per_A2"), dtype=np.float64)
            hessian_path = output_directory / "hessian_eV_per_A2.npz"
            atomic_write_npz(
                hessian_path,
                hessian_eV_per_A2=hessian,
                atomic_numbers=molecule.atomic_numbers,
                atomic_masses_u=molecule.atomic_masses_u,
                isotope_mass_numbers=molecule.isotope_mass_numbers,
                coordinates_angstrom=coordinates,
            )
            artifacts.append(
                (
                    describe_artifact(
                        artifact_id="hessian",
                        path=hessian_path,
                        media_type="application/x-npz",
                    ),
                    hessian_path,
                )
            )
            hessian_summary = _hessian_summary(hessian)
            scientific["hessian"] = {
                **hessian_summary,
                "artifact_id": "hessian",
                "units": "eV/angstrom^2",
            }
            if "frequencies" in requested_properties:
                progress(
                    "frequency",
                    90,
                    "Projecting rigid modes and calculating frequencies",
                )
                stage_started = time.perf_counter()
                vibration_result = calculate_projected_frequencies(
                    hessian,
                    molecule.atomic_masses_u,
                    coordinates,
                )
                timings["frequency_ms"] = (time.perf_counter() - stage_started) * 1000.0
                frequencies_path = output_directory / "frequencies_cm-1.csv"
                atomic_write_bytes(
                    frequencies_path,
                    _frequency_csv(vibration_result).encode("utf-8"),
                )
                artifacts.append(
                    (
                        describe_artifact(
                            artifact_id="frequencies",
                            path=frequencies_path,
                            media_type="text/csv",
                        ),
                        frequencies_path,
                    )
                )
                vibration_result["artifact_id"] = "frequencies"

        final_xyz = output_directory / "final_structure.xyz"
        write_xyz(
            final_xyz,
            symbols=molecule.symbols,
            coordinates=coordinates,
            comment=f"AIMNet2 {request.model}; charge={net_charge}; mult={request.input.multiplicity}",
        )
        artifacts.append(
            (
                describe_artifact(
                    artifact_id="final_structure",
                    path=final_xyz,
                    media_type="chemical/x-xyz",
                ),
                final_xyz,
            )
        )

        is_stationary = fmax is not None and fmax <= convergence_threshold
        scientific_status = _scientific_status(
            calculation_type=request.calculation_type,
            optimization=optimization_result,
            vibration=vibration_result,
            is_stationary=is_stationary,
            fmax=fmax,
        )
        warnings: list[dict[str, str]] = [
            {
                "code": "single_conformer",
                "message": "Only one deterministic local conformer was evaluated.",
            }
        ]
        if not molecule.rdkit_optimization_performed:
            warnings.append(
                {
                    "code": "rdkit_force_field_unavailable",
                    "message": (
                        "RDKit has no MMFF94 or UFF parameters for this molecule; "
                        "the ETKDG geometry was used without force-field optimization."
                    ),
                }
            )
        elif molecule.rdkit_optimization_status != 0:
            warnings.append(
                {
                    "code": "rdkit_not_converged",
                    "message": "The RDKit force-field conformer did not converge.",
                }
            )
        if request.input.net_charge is not None:
            differs = net_charge != molecule.formal_charge
            warnings.append(
                {
                    "code": "net_charge_override",
                    "message": (
                        "Explicit net_charge overrides SMILES charge inference and differs "
                        "from the encoded formal charge."
                        if differs
                        else "Explicit net_charge overrides SMILES charge inference and matches "
                        "the encoded formal charge."
                    ),
                }
            )

        properties: dict[str, Any] = {
            "energy": {"value_eV": scientific["energy_eV"]},
        }
        if "charges_e" in scientific:
            properties["charges"] = {
                "values_e": scientific["charges_e"],
                "sum_e": scientific["charge_sum_e"],
                "conservation_error_e": scientific["charge_conservation_error_e"],
                "conserved": scientific["charge_conserved"],
            }
        if "spin_charges_e" in scientific:
            properties["spin_charges"] = {"values_e": scientific["spin_charges_e"]}
        if "forces_eV_per_A" in scientific:
            properties["forces"] = {
                "values_eV_per_A": scientific["forces_eV_per_A"],
                "fmax_eV_per_A": scientific["fmax_eV_per_A"],
            }
        if "hessian" in scientific:
            properties["hessian"] = scientific["hessian"]
        if vibration_result is not None:
            properties["frequencies"] = {
                "artifact_id": vibration_result["artifact_id"],
                "values_cm_1": vibration_result["frequencies_cm-1"],
                "mode_count": vibration_result["mode_count"],
                "removed_rigid_modes": vibration_result["removed_rigid_modes"],
                "expected_rigid_modes": vibration_result["expected_rigid_modes"],
                "linear_molecule": vibration_result["linear_molecule"],
                "imaginary_threshold_cm_1": vibration_result[
                    "imaginary_threshold_cm-1"
                ],
                "imaginary_mode_count": vibration_result["imaginary_mode_count"],
                "imaginary_values_cm_1": vibration_result["imaginary_frequencies_cm-1"],
                "near_zero_mode_count": vibration_result["near_zero_mode_count"],
            }

        if optimization_result is not None:
            optimization_result = {
                **{
                    key: value
                    for key, value in optimization_result.items()
                    if key != "trace"
                },
                "trajectory_artifact_id": "optimization_trajectory",
                "trace": [
                    {
                        "step": frame["step"],
                        "energy_eV": frame["energy_eV"],
                        "fmax_eV_per_A": frame["fmax_eV_per_A"],
                    }
                    for frame in optimization_result["trace"]
                ],
            }

        result_provenance = dict(provenance or {})
        result_provenance.update(
            {
                "conformer_seed": request.conformer.seed,
                "rdkit_force_field": molecule.rdkit_force_field,
                "rdkit_optimization_performed": molecule.rdkit_optimization_performed,
                "rdkit_optimization_status": molecule.rdkit_optimization_status,
                "rdkit_version": _rdkit_version(),
                "mass_source": "rdkit_periodic_table_explicit_isotopes",
            }
        )
        # Result V2 always carries a complete, fenced GPU execution identity.
        # V1 readers remain supported by Backend but new Workers never emit a
        # V2 result without these fields.
        result_provenance = GpuExecutionProvenanceV2.model_validate(
            result_provenance
        ).model_dump(mode="json")
        result: dict[str, Any] = {
            "schema_version": 2,
            "calculation_type": request.calculation_type,
            "engine": "aimnet2",
            "model": request.model,
            "input": {
                "input_type": molecule.input_type,
                "canonical_smiles": molecule.canonical_smiles,
                "net_charge": net_charge,
                "input_formal_charge": molecule.formal_charge,
                "multiplicity": request.input.multiplicity,
                "electron_count": molecule.electron_count,
            },
            "atoms": {
                "count": atom_count,
                "atomic_numbers": molecule.atomic_numbers.tolist(),
                "atomic_masses_u": molecule.atomic_masses_u.tolist(),
                "isotope_mass_numbers": molecule.isotope_mass_numbers.tolist(),
                "symbols": list(molecule.symbols),
            },
            "geometry": {
                "initial_coordinates_angstrom": molecule.coordinates_angstrom.tolist(),
                "final_coordinates_angstrom": coordinates.tolist(),
                "units": "angstrom",
            },
            "rdkit": {
                "seed": request.conformer.seed,
                "force_field": molecule.rdkit_force_field,
                "optimization_performed": molecule.rdkit_optimization_performed,
                "optimization_status": molecule.rdkit_optimization_status,
                "optimization_state": (
                    "not_performed"
                    if not molecule.rdkit_optimization_performed
                    else (
                        "converged"
                        if molecule.rdkit_optimization_status == 0
                        else "not_converged"
                    )
                ),
            },
            "properties": properties,
            "optimization": optimization_result,
            "scientific_status": scientific_status,
            "warnings": warnings,
            "provenance": result_provenance,
        }
        progress("artifacts", 96, "Writing checksummed result artifacts")
        result_path = output_directory / "scientific_result.json"
        elapsed_before_result_ms = (time.perf_counter() - started) * 1000.0
        measured_science_ms = sum(
            timings[key]
            for key in (
                "structure_prepare_ms",
                "model_compute_ms",
                "optimization_ms",
                "hessian_ms",
                "frequency_ms",
            )
        )
        # Artifact work is intentionally accumulated as the elapsed remainder:
        # this includes XYZ/JSON/CSV/NPZ serialization performed throughout the
        # run, rather than timing only the final result JSON write.
        timings["artifact_ms"] = max(
            0.0, elapsed_before_result_ms - measured_science_ms
        )
        timings["total_ms"] = (
            timings["queue_wait_ms"]
            + timings["gpu_wait_ms"]
            + timings["model_load_ms"]
            + elapsed_before_result_ms
        )
        result["timings"] = dict(timings)
        result_write_started = time.perf_counter()
        atomic_write_json(result_path, result)
        timings["artifact_ms"] += (time.perf_counter() - result_write_started) * 1000.0
        timings["total_ms"] = (
            timings["queue_wait_ms"]
            + timings["gpu_wait_ms"]
            + timings["model_load_ms"]
            + (time.perf_counter() - started) * 1000.0
        )
        result["timings"] = dict(timings)
        # Rewrite once so the checksummed result contains final timing values.
        atomic_write_json(result_path, result)
        artifacts.append(
            (
                describe_artifact(
                    artifact_id="scientific_result",
                    path=result_path,
                    media_type="application/json",
                ),
                result_path,
            )
        )
        return EngineExecution(
            result=result,
            timings=timings,
            artifacts=tuple(artifacts),
        )

    @staticmethod
    def _check_cancelled(cancelled: CancellationCheck) -> None:
        if cancelled():
            raise ComputationCancelled("job cancellation was requested")

    def _synchronize_backend(self) -> None:
        synchronize = getattr(self.backend, "synchronize", None)
        if synchronize is not None:
            synchronize()


def _normalize_evaluation(
    raw: dict[str, Any],
    *,
    atom_count: int,
    net_charge: int,
    required_properties: set[str],
) -> dict[str, Any]:
    if "energy" not in raw:
        raise ScientificComputationError(
            "missing_result", "AIMNet2 did not return energy"
        )
    energy_values = np.asarray(raw["energy"], dtype=np.float64).reshape(-1)
    if energy_values.size != 1:
        raise ScientificComputationError(
            "invalid_result_shape",
            "AIMNet2 returned a non-scalar molecular energy",
        )
    _require_finite("energy", energy_values)
    result: dict[str, Any] = {"energy_eV": float(energy_values[0])}

    if "charges" not in raw and "charges" in required_properties:
        raise ScientificComputationError(
            "missing_result", "AIMNet2 did not return the requested atomic charges"
        )
    if "charges" in raw and "charges" in required_properties:
        charges = np.asarray(raw["charges"], dtype=np.float64).reshape(-1)
        if charges.size != atom_count:
            raise ScientificComputationError(
                "invalid_result_shape",
                "AIMNet2 atomic charge count does not match the molecule",
            )
        _require_finite("charges", charges)
        charge_sum = float(charges.sum())
        result.update(
            {
                "charges_e": charges.tolist(),
                "charge_sum_e": charge_sum,
                "charge_conservation_error_e": charge_sum - net_charge,
                "charge_conserved": abs(charge_sum - net_charge) <= 1.0e-3,
            }
        )
    if "spin_charges" in raw and "charges" in required_properties:
        spin_charges = np.asarray(raw["spin_charges"], dtype=np.float64).reshape(-1)
        if spin_charges.size == atom_count:
            _require_finite("spin charges", spin_charges)
            result["spin_charges_e"] = spin_charges.tolist()
    if "forces" not in raw and "forces" in required_properties:
        raise ScientificComputationError(
            "missing_result", "AIMNet2 did not return the requested atomic forces"
        )
    if "forces" in raw and "forces" in required_properties:
        force_values = np.asarray(raw["forces"], dtype=np.float64)
        if force_values.size != atom_count * 3:
            raise ScientificComputationError(
                "invalid_result_shape",
                "AIMNet2 force shape does not match the molecule",
            )
        forces = force_values.reshape(atom_count, 3)
        _require_finite("forces", forces)
        result["forces_eV_per_A"] = forces.tolist()
        result["fmax_eV_per_A"] = _maximum_force(forces)
    if "hessian" not in raw and "hessian" in required_properties:
        raise ScientificComputationError(
            "missing_result", "AIMNet2 did not return the requested Hessian"
        )
    if "hessian" in raw and "hessian" in required_properties:
        hessian_values = np.asarray(raw["hessian"], dtype=np.float64)
        dimension = atom_count * 3
        if hessian_values.size != dimension * dimension:
            raise ScientificComputationError(
                "invalid_result_shape",
                "AIMNet2 Hessian shape does not match the molecule",
            )
        hessian = hessian_values.reshape(dimension, dimension)
        _require_finite("Hessian", hessian)
        result["hessian_eV_per_A2"] = hessian
    return result


def _require_finite(label: str, value: np.ndarray) -> None:
    if not np.isfinite(value).all():
        raise ScientificComputationError(
            "non_finite_result",
            f"AIMNet2 returned non-finite {label}",
        )


def _maximum_force(forces: np.ndarray) -> float:
    if forces.size == 0:
        return 0.0
    return float(np.linalg.norm(forces, axis=1).max())


def calculate_projected_frequencies(
    hessian_eV_per_A2: np.ndarray,
    atomic_masses_u: np.ndarray,
    coordinates_angstrom: np.ndarray,
    *,
    imaginary_threshold_cm_1: float = -10.0,
    linear_inertia_relative_tolerance: float = LINEAR_INERTIA_RELATIVE_TOLERANCE,
) -> dict[str, Any]:
    masses = np.asarray(atomic_masses_u, dtype=np.float64).reshape(-1)
    coordinates = np.asarray(coordinates_angstrom, dtype=np.float64).reshape(-1, 3)
    atom_count = len(masses)
    if coordinates.shape[0] != atom_count:
        raise ScientificComputationError(
            "invalid_atomic_mass",
            "atomic masses do not match the molecular geometry",
        )
    if not np.isfinite(masses).all() or np.any(masses <= 0.0):
        raise ScientificComputationError(
            "invalid_atomic_mass", "atomic masses must be positive and finite"
        )
    dimension = atom_count * 3
    hessian = np.asarray(hessian_eV_per_A2, dtype=np.float64).reshape(
        dimension, dimension
    )
    hessian = (hessian + hessian.T) / 2.0
    repeated_masses = np.repeat(masses, 3)
    inv_sqrt_mass = 1.0 / np.sqrt(repeated_masses)
    mass_weighted = hessian * np.outer(inv_sqrt_mass, inv_sqrt_mass)

    center_of_mass = np.average(coordinates, axis=0, weights=masses)
    centered = coordinates - center_of_mass
    if (
        not math.isfinite(linear_inertia_relative_tolerance)
        or linear_inertia_relative_tolerance <= 0.0
        or linear_inertia_relative_tolerance >= 1.0
    ):
        raise ValueError("linear inertia relative tolerance must be between 0 and 1")

    rigid_vectors: list[np.ndarray] = []
    for axis_index in range(3):
        vector = np.zeros((atom_count, 3), dtype=np.float64)
        vector[:, axis_index] = np.sqrt(masses)
        rigid_vectors.append(vector.reshape(-1))
    rotational_rank = 0
    inertia_eigenvalues = np.zeros(3, dtype=np.float64)
    rotation_axes = np.empty((3, 0), dtype=np.float64)
    if atom_count > 1:
        inertia = np.zeros((3, 3), dtype=np.float64)
        for mass, position in zip(masses, centered, strict=True):
            radius_squared = float(np.dot(position, position))
            inertia += mass * (
                radius_squared * np.eye(3, dtype=np.float64)
                - np.outer(position, position)
            )
        inertia = (inertia + inertia.T) / 2.0
        inertia_eigenvalues, inertia_axes = np.linalg.eigh(inertia)
        inertia_eigenvalues = np.maximum(inertia_eigenvalues, 0.0)
        largest_moment = float(inertia_eigenvalues[-1])
        if largest_moment > 0.0:
            inertia_tolerance = (
                largest_moment * linear_inertia_relative_tolerance
            )
            retained = inertia_eigenvalues > inertia_tolerance
            rotation_axes = inertia_axes[:, retained]
            rotational_rank = int(np.count_nonzero(retained))

    for axis in rotation_axes.T:
        rotational = np.cross(np.broadcast_to(axis, centered.shape), centered)
        rotational *= np.sqrt(masses)[:, None]
        rigid_vectors.append(rotational.reshape(-1))

    rigid_matrix = np.column_stack(rigid_vectors)
    rigid_rank = 3 + rotational_rank
    orthogonal_basis, _ = np.linalg.qr(rigid_matrix, mode="complete")
    vibrational_basis = orthogonal_basis[:, rigid_rank:]
    if vibrational_basis.shape[1]:
        reduced_hessian = vibrational_basis.T @ mass_weighted @ vibrational_basis
        eigenvalues = np.linalg.eigvalsh((reduced_hessian + reduced_hessian.T) / 2.0)
    else:
        eigenvalues = np.empty((0,), dtype=np.float64)

    electron_volt = 1.602176634e-19
    atomic_mass_unit = 1.66053906660e-27
    speed_of_light_cm_s = 2.99792458e10
    conversion = math.sqrt(electron_volt / (atomic_mass_unit * 1.0e-20)) / (
        2.0 * math.pi * speed_of_light_cm_s
    )
    frequencies = np.sign(eigenvalues) * np.sqrt(np.abs(eigenvalues)) * conversion
    frequencies.sort()
    imaginary = frequencies[frequencies < imaginary_threshold_cm_1]
    near_zero = frequencies[np.abs(frequencies) <= abs(imaginary_threshold_cm_1)]
    is_linear = atom_count > 1 and rotational_rank == 2
    return {
        "frequencies_cm-1": frequencies.tolist(),
        "mode_count": int(frequencies.size),
        "removed_rigid_modes": rigid_rank,
        "expected_rigid_modes": 3 if atom_count == 1 else (5 if is_linear else 6),
        "linear_molecule": is_linear,
        "imaginary_threshold_cm-1": imaginary_threshold_cm_1,
        "imaginary_mode_count": int(imaginary.size),
        "imaginary_frequencies_cm-1": imaginary.tolist(),
        "near_zero_mode_count": int(near_zero.size),
    }


def _rdkit_version() -> str | None:
    try:
        import rdkit

        return str(rdkit.__version__)
    except (ImportError, AttributeError):
        return None


def _hessian_summary(hessian: np.ndarray) -> dict[str, Any]:
    difference = hessian - hessian.T
    max_abs = float(np.max(np.abs(difference))) if hessian.size else 0.0
    scale = max(float(np.max(np.abs(hessian))) if hessian.size else 0.0, 1.0e-12)
    return {
        "shape": list(hessian.shape),
        "symmetry_max_abs_eV_per_A2": max_abs,
        "symmetry_relative_error": max_abs / scale,
        "symmetric_within_tolerance": max_abs / scale <= 1.0e-5,
    }


def _frequency_csv(vibration: dict[str, Any]) -> str:
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["vibrational_mode", "frequency_cm-1", "classification"])
    threshold = float(vibration["imaginary_threshold_cm-1"])
    for index, frequency in enumerate(vibration["frequencies_cm-1"], start=1):
        if frequency < threshold:
            classification = "imaginary"
        elif abs(frequency) <= abs(threshold):
            classification = "near_zero"
        else:
            classification = "real"
        writer.writerow([index, f"{frequency:.10f}", classification])
    return stream.getvalue()


def _scientific_status(
    *,
    calculation_type: str,
    optimization: dict[str, Any] | None,
    vibration: dict[str, Any] | None,
    is_stationary: bool,
    fmax: float | None,
) -> dict[str, Any]:
    if calculation_type == "optimization":
        geometry_status = (
            "converged"
            if optimization and optimization["converged"]
            else "max_steps_reached"
        )
    else:
        geometry_status = "not_optimized"
    if vibration is None:
        stationary_point = "not_evaluated"
    elif not is_stationary:
        stationary_point = "not_stationary"
    else:
        imaginary_count = int(vibration["imaginary_mode_count"])
        if imaginary_count == 0:
            stationary_point = "minimum"
        elif imaginary_count == 1:
            stationary_point = "first_order_saddle"
        else:
            stationary_point = "higher_order_saddle"
    if calculation_type != "optimization":
        minimum_assessment = "unassessed"
    elif not optimization or not optimization["converged"]:
        minimum_assessment = "not_converged"
    elif vibration is None:
        minimum_assessment = "unassessed"
    elif int(vibration["imaginary_mode_count"]) == 0:
        minimum_assessment = "confirmed_minimum"
    else:
        minimum_assessment = "nonminimum_or_saddle"
    return {
        "calculation_completed": True,
        "geometry_status": geometry_status,
        "is_stationary": is_stationary,
        "stationary_point": stationary_point,
        "minimum_assessment": minimum_assessment,
        "fmax_eV_per_A": fmax,
    }

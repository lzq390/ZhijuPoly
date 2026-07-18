from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .monomer_dft_models import (
    MonomerDftConformer,
    MonomerDftInput,
    MonomerDftModelName,
    MonomerDftOptimizationOptions,
    MonomerDftRunRequest,
    MonomerDftSinglePointOptions,
    MAX_ARTIFACT_BYTES,
    PORTABLE_ARTIFACT_FILENAME_PATTERN,
    validate_portable_artifact_filename,
)
from .monomer_dft_protocol import (
    calculation_request_sha256,
    prepare_monomer_dft_request,
)


WORKER_STAGES = (
    "queued",
    "validating",
    "conformer",
    "single_point",
    "optimization",
    "hessian",
    "frequency",
    "artifacts",
)
WORKER_TIMING_KEYS = (
    "queue_wait_ms",
    "structure_prepare_ms",
    "gpu_wait_ms",
    "model_load_ms",
    "model_compute_ms",
    "optimization_ms",
    "hessian_ms",
    "frequency_ms",
    "artifact_ms",
    "total_ms",
)
LEGACY_SCIENTIFIC_TIMING_KEYS = (
    "queue_wait_ms",
    "structure_prepare_ms",
    "model_compute_ms",
    "optimization_ms",
    "hessian_ms",
    "frequency_ms",
    "artifact_ms",
    "total_ms",
)
WorkerStage = Literal[
    "queued",
    "validating",
    "conformer",
    "single_point",
    "optimization",
    "hessian",
    "frequency",
    "artifacts",
]
WorkerJobStatus = Literal[
    "pending",
    "queued",
    "running",
    "failed",
    "cancel_requested",
    "completed",
    "cancelled",
]
WorkerArtifactState = Literal["none", "available", "deleting", "deleted"]

_RUN_REQUEST_ADAPTER = TypeAdapter(MonomerDftRunRequest)
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_V2_MASS_SOURCE = "rdkit_periodic_table_explicit_isotopes"
_ATOMIC_SYMBOLS = {
    1: "H",
    5: "B",
    6: "C",
    7: "N",
    8: "O",
    9: "F",
    14: "Si",
    15: "P",
    16: "S",
    17: "Cl",
    33: "As",
    34: "Se",
    35: "Br",
    46: "Pd",
    53: "I",
}


class InternalWorkerModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
        validate_default=True,
    )


def _validated_timings(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) not in (
        set(WORKER_TIMING_KEYS),
        set(LEGACY_SCIENTIFIC_TIMING_KEYS),
    ):
        raise ValueError("worker timings must contain a supported protocol key set")
    result: dict[str, float] = {}
    for key in value:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("worker timing values must be numeric")
        number = float(item)
        if not math.isfinite(number) or number < 0.0:
            raise ValueError("worker timing values must be finite and non-negative")
        result[key] = number
    return result


class InternalWorkerRequest(InternalWorkerModel):
    schema_version: Literal[2]
    job_id: str = Field(pattern=_UUID_PATTERN)
    attempt_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    enqueue_sequence: int = Field(ge=1)
    input: MonomerDftInput
    calculation_type: Literal["single_point", "optimization"]
    model: MonomerDftModelName = "aimnet2"
    conformer: MonomerDftConformer = Field(default_factory=MonomerDftConformer)
    single_point: MonomerDftSinglePointOptions | None = None
    optimization: MonomerDftOptimizationOptions | None = None

    @model_validator(mode="after")
    def validate_scientific_branch_and_hash(self) -> "InternalWorkerRequest":
        payload: dict[str, Any] = {
            "input": self.input.model_dump(mode="json"),
            "calculation_type": self.calculation_type,
            "model": self.model,
            "conformer": self.conformer.model_dump(mode="json"),
        }
        if self.calculation_type == "single_point":
            if self.single_point is None or self.optimization is not None:
                raise ValueError("worker request has an invalid single_point branch")
            payload["single_point"] = self.single_point.model_dump(mode="json")
        else:
            if self.optimization is None or self.single_point is not None:
                raise ValueError("worker request has an invalid optimization branch")
            payload["optimization"] = self.optimization.model_dump(mode="json")
        normalized = _RUN_REQUEST_ADAPTER.validate_python(payload).model_dump(mode="json")
        if normalized != payload:
            raise ValueError("worker request is not in canonical normalized form")
        if calculation_request_sha256(payload) != self.request_sha256:
            raise ValueError("worker request hash does not match its scientific payload")
        return self

    def scientific_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "input": self.input.model_dump(mode="json"),
            "calculation_type": self.calculation_type,
            "model": self.model,
            "conformer": self.conformer.model_dump(mode="json"),
        }
        branch = "single_point" if self.calculation_type == "single_point" else "optimization"
        config = self.single_point if branch == "single_point" else self.optimization
        assert config is not None
        payload[branch] = config.model_dump(mode="json")
        return payload

    def requested_properties(self) -> set[str]:
        if self.calculation_type == "single_point":
            assert self.single_point is not None
            properties = set(self.single_point.properties)
        else:
            assert self.optimization is not None
            properties = {"energy", "charges", "forces", *self.optimization.post_optimization_properties}
        if "frequencies" in properties:
            properties.add("hessian")
        properties.add("energy")
        return properties


class InternalWorkerError(InternalWorkerModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class InternalWorkerArtifact(InternalWorkerModel):
    artifact_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(
        min_length=1,
        max_length=255,
        pattern=PORTABLE_ARTIFACT_FILENAME_PATTERN,
    )
    media_type: str = Field(min_length=1, max_length=127)
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("name", mode="before")
    @classmethod
    def safe_name(cls, value: Any) -> str:
        return validate_portable_artifact_filename(value)

    @field_validator("media_type", mode="before")
    @classmethod
    def safe_media_type(cls, value: Any) -> str:
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("worker artifact media type is unsafe")
        return value


class InternalWorkerArtifactDeletionResponse(InternalWorkerModel):
    job_id: str = Field(pattern=_UUID_PATTERN)
    deleted: Literal[True]
    artifact_state: Literal["none", "deleted"]
    deleted_artifacts: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_absent_count(self) -> "InternalWorkerArtifactDeletionResponse":
        if self.artifact_state == "none" and self.deleted_artifacts != 0:
            raise ValueError("worker none artifact state cannot report deleted artifacts")
        return self


class InternalResultInput(InternalWorkerModel):
    input_type: Literal["smiles", "psmiles_close", "psmiles_cap"]
    canonical_smiles: str = Field(min_length=1, max_length=2_048)
    net_charge: int = Field(ge=-5, le=5)
    # The public charge limit applies to the effective net charge sent to
    # AIMNet, not to the formal charge encoded by the input SMILES.  Explicit
    # net_charge overrides intentionally preserve the latter for provenance.
    input_formal_charge: int
    multiplicity: int = Field(ge=1, le=7)
    electron_count: int = Field(ge=1)


class InternalResultAtoms(InternalWorkerModel):
    count: int = Field(ge=1, le=300)
    atomic_numbers: list[int] = Field(min_length=1, max_length=300)
    symbols: list[str] = Field(min_length=1, max_length=300)
    isotope_mass_numbers: list[int] | None = None
    atomic_masses_u: list[float] | None = None

    @model_validator(mode="after")
    def validate_atoms(self) -> "InternalResultAtoms":
        if len(self.atomic_numbers) != self.count or len(self.symbols) != self.count:
            raise ValueError("worker atom arrays do not match atoms.count")
        for number, symbol in zip(self.atomic_numbers, self.symbols, strict=True):
            if _ATOMIC_SYMBOLS.get(number) != symbol:
                raise ValueError("worker atom number and symbol arrays are inconsistent")
        if self.isotope_mass_numbers is not None:
            if len(self.isotope_mass_numbers) != self.count or any(
                item < 0 for item in self.isotope_mass_numbers
            ):
                raise ValueError("worker isotope mass numbers do not match atoms.count")
        if self.atomic_masses_u is not None:
            if len(self.atomic_masses_u) != self.count or any(
                not math.isfinite(item) or item <= 0.0 for item in self.atomic_masses_u
            ):
                raise ValueError("worker atomic masses do not match atoms.count")
        return self


def _validate_coordinate_array(value: Any, *, label: str) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty array")
    result: list[list[float]] = []
    for vector in value:
        if not isinstance(vector, list) or len(vector) != 3:
            raise ValueError(f"{label} must contain three-dimensional vectors")
        converted: list[float] = []
        for item in vector:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"{label} coordinates must be numeric")
            number = float(item)
            if not math.isfinite(number):
                raise ValueError(f"{label} coordinates must be finite")
            converted.append(number)
        result.append(converted)
    return result


class InternalResultGeometry(InternalWorkerModel):
    initial_coordinates_angstrom: list[list[float]]
    final_coordinates_angstrom: list[list[float]]
    units: Literal["angstrom"]

    @field_validator("initial_coordinates_angstrom", "final_coordinates_angstrom", mode="before")
    @classmethod
    def validate_coordinates(cls, value: Any, info) -> list[list[float]]:
        return _validate_coordinate_array(value, label=info.field_name)


class InternalResultRdkit(InternalWorkerModel):
    seed: int = Field(ge=0, le=2_147_483_647)
    force_field: Literal["MMFF94", "UFF", "ETKDG-only"]
    optimization_performed: bool | None = None
    optimization_status: int
    optimization_state: Literal["not_performed", "converged", "not_converged"] | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "InternalResultRdkit":
        if self.optimization_performed is None and self.optimization_state is None:
            return self
        if self.optimization_performed is None or self.optimization_state is None:
            raise ValueError("worker RDKit optimization fields are only partially present")
        expected = (
            "not_performed"
            if not self.optimization_performed
            else ("converged" if self.optimization_status == 0 else "not_converged")
        )
        if self.optimization_state != expected:
            raise ValueError("worker RDKit optimization state is inconsistent")
        return self


class InternalEnergy(InternalWorkerModel):
    value_eV: float


class InternalCharges(InternalWorkerModel):
    values_e: list[float] = Field(min_length=1, max_length=300)
    sum_e: float
    conservation_error_e: float
    conserved: bool

    @model_validator(mode="after")
    def validate_sum(self) -> "InternalCharges":
        if not math.isclose(sum(self.values_e), self.sum_e, rel_tol=1e-9, abs_tol=1e-8):
            raise ValueError("worker atomic charge sum is inconsistent")
        if self.conserved != (abs(self.conservation_error_e) <= 1.0e-3):
            raise ValueError("worker charge conservation flag is inconsistent")
        return self


class InternalSpinCharges(InternalWorkerModel):
    values_e: list[float] = Field(min_length=1, max_length=300)


class InternalForces(InternalWorkerModel):
    values_eV_per_A: list[list[float]]
    fmax_eV_per_A: float = Field(ge=0.0)

    @field_validator("values_eV_per_A", mode="before")
    @classmethod
    def validate_force_vectors(cls, value: Any) -> list[list[float]]:
        return _validate_coordinate_array(value, label="forces")

    @model_validator(mode="after")
    def validate_fmax(self) -> "InternalForces":
        calculated = max(math.sqrt(sum(component * component for component in row)) for row in self.values_eV_per_A)
        if not math.isclose(calculated, self.fmax_eV_per_A, rel_tol=1e-7, abs_tol=1e-8):
            raise ValueError("worker maximum force is inconsistent")
        return self


class InternalHessian(InternalWorkerModel):
    shape: list[int] = Field(min_length=2, max_length=2)
    symmetry_max_abs_eV_per_A2: float = Field(ge=0.0)
    symmetry_relative_error: float = Field(ge=0.0)
    symmetric_within_tolerance: bool
    artifact_id: Literal["hessian"]
    units: Literal["eV/angstrom^2"]


class InternalFrequencies(InternalWorkerModel):
    artifact_id: Literal["frequencies"]
    values_cm_1: list[float]
    mode_count: int = Field(ge=0)
    removed_rigid_modes: int = Field(ge=0, le=6)
    expected_rigid_modes: int = Field(ge=3, le=6)
    linear_molecule: bool
    imaginary_threshold_cm_1: float
    imaginary_mode_count: int = Field(ge=0)
    imaginary_values_cm_1: list[float]
    near_zero_mode_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "InternalFrequencies":
        if len(self.values_cm_1) != self.mode_count:
            raise ValueError("worker frequency mode count is inconsistent")
        if len(self.imaginary_values_cm_1) != self.imaginary_mode_count:
            raise ValueError("worker imaginary frequency count is inconsistent")
        if self.near_zero_mode_count > self.mode_count:
            raise ValueError("worker near-zero frequency count is inconsistent")
        return self


class InternalProperties(InternalWorkerModel):
    energy: InternalEnergy
    charges: InternalCharges | None = None
    spin_charges: InternalSpinCharges | None = None
    forces: InternalForces | None = None
    hessian: InternalHessian | None = None
    frequencies: InternalFrequencies | None = None


class InternalOptimizationTracePoint(InternalWorkerModel):
    step: int = Field(ge=0, le=50)
    energy_eV: float
    fmax_eV_per_A: float = Field(ge=0.0)


class InternalOptimization(InternalWorkerModel):
    converged: bool
    steps: int = Field(ge=0, le=50)
    fmax_threshold_eV_per_A: float = Field(ge=0.001, le=1.0)
    max_steps: int = Field(ge=10, le=50)
    trajectory_artifact_id: Literal["optimization_trajectory"]
    trace: list[InternalOptimizationTracePoint] = Field(max_length=51)


class InternalScientificStatus(InternalWorkerModel):
    calculation_completed: Literal[True]
    geometry_status: Literal["converged", "max_steps_reached", "not_optimized"]
    is_stationary: bool
    stationary_point: Literal[
        "minimum",
        "first_order_saddle",
        "higher_order_saddle",
        "not_evaluated",
        "not_stationary",
    ]
    minimum_assessment: Literal[
        "confirmed_minimum",
        "nonminimum_or_saddle",
        "not_converged",
        "unassessed",
    ]
    fmax_eV_per_A: float | None


class InternalResultWarning(InternalWorkerModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: str = Field(min_length=1, max_length=1_000)


class InternalProvenance(InternalWorkerModel):
    worker_version: str
    worker_instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    model_alias: MonomerDftModelName
    model_id: MonomerDftModelName
    model_registry_key: str | None
    model_family: str | None
    model_reference: str | None
    model_sha256: str | None = Field(pattern=_SHA256_PATTERN)
    aimnet_version: str | None
    aimnet_commit: str | None
    aimnet_wheel_sha256: str | None = Field(pattern=_SHA256_PATTERN)
    warp_version: str | None
    torch_version: str | None
    cuda_runtime: str | None
    cuda_version: str | None
    gpu_name: str | None
    visible_gpu_count: int = Field(ge=1)
    logical_device: Literal["cuda:0"]
    physical_gpu: str
    gpu_logical_device: Literal["cuda:0"]
    gpu_physical_device: str
    conformer_seed: int = Field(ge=0, le=2_147_483_647)
    rdkit_force_field: Literal["MMFF94", "UFF", "ETKDG-only"]
    rdkit_optimization_performed: bool | None = None
    rdkit_optimization_status: int | None = None
    rdkit_version: str | None = None
    mass_source: str | None = None
    execution_path: Literal["primary", "overflow"] | None = None
    gpu_uuid: str | None = Field(default=None, min_length=1)
    gpu_budget_mib: int | None = Field(default=None, gt=0)
    broker_instance_id: str | None = Field(default=None, min_length=1)
    lease_id: str | None = Field(default=None, min_length=1)
    fencing_token: int | None = Field(default=None, gt=0)
    gpu_active_thread_percentage: int | None = Field(default=None, ge=1, le=100)
    gpu_lease_id: str | None = Field(default=None, min_length=1)
    gpu_fencing_token: int | None = Field(default=None, gt=0)
    gpu_broker_instance_id: str | None = Field(default=None, min_length=1)
    gpu_preferred: bool | None = None


class InternalScientificResult(InternalWorkerModel):
    schema_version: Literal[1, 2]
    calculation_type: Literal["single_point", "optimization"]
    engine: Literal["aimnet2"]
    model: MonomerDftModelName
    input: InternalResultInput
    atoms: InternalResultAtoms
    geometry: InternalResultGeometry
    rdkit: InternalResultRdkit
    properties: InternalProperties
    optimization: InternalOptimization | None
    scientific_status: InternalScientificStatus
    warnings: list[InternalResultWarning] = Field(min_length=1, max_length=100)
    timings: dict[str, float]
    provenance: InternalProvenance

    @field_validator("timings", mode="before")
    @classmethod
    def validate_timings(cls, value: Any) -> dict[str, float]:
        return _validated_timings(value)

    @model_validator(mode="after")
    def validate_result_shapes(self) -> "InternalScientificResult":
        count = self.atoms.count
        if self.schema_version == 2 and set(self.timings) != set(WORKER_TIMING_KEYS):
            raise ValueError("worker result v2 timings must contain exactly the ten protocol keys")
        if self.schema_version == 1:
            if self.atoms.isotope_mass_numbers is not None or self.atoms.atomic_masses_u is not None:
                raise ValueError("worker result v1 contains v2 atom-mass fields")
        elif (
            self.atoms.isotope_mass_numbers is None
            or self.atoms.atomic_masses_u is None
            or not self.provenance.rdkit_version
            or not self.provenance.mass_source
            or self.rdkit.optimization_performed is None
            or self.rdkit.optimization_state is None
            or self.provenance.rdkit_optimization_performed is None
            or self.provenance.rdkit_optimization_status is None
            or self.provenance.execution_path is None
            or self.provenance.gpu_uuid is None
            or self.provenance.gpu_budget_mib is None
            or self.provenance.broker_instance_id is None
            or self.provenance.lease_id is None
            or self.provenance.fencing_token is None
        ):
            raise ValueError("worker result v2 is missing required runtime provenance")
        if self.schema_version == 2 and (
            self.provenance.rdkit_optimization_performed
            != self.rdkit.optimization_performed
            or self.provenance.rdkit_optimization_status
            != self.rdkit.optimization_status
        ):
            raise ValueError("worker result v2 RDKit provenance is inconsistent")
        if self.schema_version == 2 and (
            self.provenance.gpu_lease_id not in {None, self.provenance.lease_id}
            or self.provenance.gpu_fencing_token not in {None, self.provenance.fencing_token}
            or self.provenance.gpu_broker_instance_id
            not in {None, self.provenance.broker_instance_id}
        ):
            raise ValueError("worker result v2 GPU provenance aliases are inconsistent")
        if self.schema_version == 2 and (
            self.provenance.physical_gpu != self.provenance.gpu_physical_device
        ):
            raise ValueError("worker result v2 physical GPU provenance is inconsistent")
        if len(self.geometry.initial_coordinates_angstrom) != count or len(self.geometry.final_coordinates_angstrom) != count:
            raise ValueError("worker geometry arrays do not match atom count")
        if self.input.electron_count != sum(self.atoms.atomic_numbers) - self.input.net_charge:
            raise ValueError("worker electron count is inconsistent")
        if self.properties.charges is not None:
            if len(self.properties.charges.values_e) != count:
                raise ValueError("worker atomic charges do not match atom count")
            expected_error = self.properties.charges.sum_e - self.input.net_charge
            if not math.isclose(
                expected_error,
                self.properties.charges.conservation_error_e,
                rel_tol=1e-9,
                abs_tol=1e-8,
            ):
                raise ValueError("worker charge conservation error is inconsistent")
        if self.properties.spin_charges is not None and len(self.properties.spin_charges.values_e) != count:
            raise ValueError("worker spin charges do not match atom count")
        if self.properties.forces is not None and len(self.properties.forces.values_eV_per_A) != count:
            raise ValueError("worker forces do not match atom count")
        if self.properties.hessian is not None and self.properties.hessian.shape != [count * 3, count * 3]:
            raise ValueError("worker Hessian summary shape does not match atom count")
        if self.calculation_type == "single_point" and self.optimization is not None:
            raise ValueError("single-point worker result contains optimization data")
        if self.calculation_type == "optimization" and self.optimization is None:
            raise ValueError("optimization worker result is missing optimization data")
        if self.schema_version == 2:
            self._validate_v2_scientific_status()
        return self

    def _validate_v2_scientific_status(self) -> None:
        """Bind the V2 scientific interpretation to the numeric result.

        V1 remains deliberately permissive for historical reads.  New V2
        results may not self-assert convergence or a minimum independently of
        their force/frequency summaries.
        """

        frequencies = self.properties.frequencies
        imaginary_count: int | None = None
        if frequencies is not None:
            threshold = frequencies.imaginary_threshold_cm_1
            if not math.isclose(threshold, -10.0, rel_tol=0.0, abs_tol=1.0e-12):
                raise ValueError("worker result v2 uses an unsupported imaginary-mode threshold")
            imaginary_values = [value for value in frequencies.values_cm_1 if value < threshold]
            if len(imaginary_values) != frequencies.imaginary_mode_count or any(
                not math.isclose(actual, expected, rel_tol=1.0e-12, abs_tol=1.0e-12)
                for actual, expected in zip(
                    frequencies.imaginary_values_cm_1,
                    imaginary_values,
                    strict=True,
                )
            ):
                raise ValueError("worker result v2 imaginary frequencies are inconsistent")
            expected_near_zero = sum(
                abs(value) <= abs(threshold) for value in frequencies.values_cm_1
            )
            if frequencies.near_zero_mode_count != expected_near_zero:
                raise ValueError("worker result v2 near-zero frequency count is inconsistent")
            imaginary_count = len(imaginary_values)

        force_fmax = (
            self.properties.forces.fmax_eV_per_A
            if self.properties.forces is not None
            else None
        )
        status_fmax = self.scientific_status.fmax_eV_per_A
        if (force_fmax is None) != (status_fmax is None) or (
            force_fmax is not None
            and status_fmax is not None
            and not math.isclose(force_fmax, status_fmax, rel_tol=1.0e-9, abs_tol=1.0e-10)
        ):
            raise ValueError("worker result v2 scientific fmax does not match forces")

        convergence_threshold = 0.01
        if self.calculation_type == "optimization":
            assert self.optimization is not None
            convergence_threshold = self.optimization.fmax_threshold_eV_per_A
        expected_stationary = force_fmax is not None and force_fmax <= convergence_threshold
        if self.scientific_status.is_stationary != expected_stationary:
            raise ValueError("worker result v2 stationary flag is inconsistent with fmax")

        if self.calculation_type == "optimization":
            assert self.optimization is not None
            expected_geometry_status = (
                "converged" if self.optimization.converged else "max_steps_reached"
            )
            if self.optimization.converged != expected_stationary:
                raise ValueError("worker result v2 optimization convergence is inconsistent with fmax")
        else:
            expected_geometry_status = "not_optimized"
        if self.scientific_status.geometry_status != expected_geometry_status:
            raise ValueError("worker result v2 geometry status is inconsistent")

        if imaginary_count is None:
            expected_stationary_point = "not_evaluated"
        elif not expected_stationary:
            expected_stationary_point = "not_stationary"
        elif imaginary_count == 0:
            expected_stationary_point = "minimum"
        elif imaginary_count == 1:
            expected_stationary_point = "first_order_saddle"
        else:
            expected_stationary_point = "higher_order_saddle"
        if self.scientific_status.stationary_point != expected_stationary_point:
            raise ValueError("worker result v2 stationary-point assessment is inconsistent")

        if self.calculation_type == "single_point":
            expected_minimum_assessment = "unassessed"
        else:
            assert self.optimization is not None
            if not self.optimization.converged:
                expected_minimum_assessment = "not_converged"
            elif imaginary_count is None:
                expected_minimum_assessment = "unassessed"
            elif imaginary_count == 0:
                expected_minimum_assessment = "confirmed_minimum"
            else:
                expected_minimum_assessment = "nonminimum_or_saddle"
        if self.scientific_status.minimum_assessment != expected_minimum_assessment:
            raise ValueError("worker result v2 minimum assessment is inconsistent")


class InternalWorkerSnapshot(InternalWorkerModel):
    schema_version: Literal[2]
    job_id: str = Field(pattern=_UUID_PATTERN)
    attempt_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    enqueue_sequence: int = Field(ge=1)
    worker_instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: WorkerJobStatus
    artifact_state: WorkerArtifactState
    queue_position: int | None = Field(default=None, ge=1)
    stage: WorkerStage
    progress_percent: int = Field(ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    request: InternalWorkerRequest
    result: InternalScientificResult | None = None
    error: InternalWorkerError | None = None
    timings: dict[str, float]
    artifacts: list[InternalWorkerArtifact] = Field(max_length=100)

    @field_validator("created_at", "updated_at", "started_at", "finished_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> Any:
        if value is None or isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise ValueError("worker timestamp must be ISO-8601 text")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("worker timestamp must include a timezone")
        return parsed

    @field_validator("timings", mode="before")
    @classmethod
    def validate_timings(cls, value: Any) -> dict[str, float]:
        return _validated_timings(value)

    @model_validator(mode="after")
    def validate_identity_state_and_result(self) -> "InternalWorkerSnapshot":
        if set(self.timings) != set(WORKER_TIMING_KEYS):
            raise ValueError("worker snapshot v2 timings must contain exactly the ten protocol keys")
        if (
            self.request.job_id != self.job_id
            or self.request.attempt_token != self.attempt_token
            or self.request.request_sha256 != self.request_sha256
            or self.request.enqueue_sequence != self.enqueue_sequence
        ):
            raise ValueError("worker snapshot identity does not match its request")
        if self.updated_at < self.created_at:
            raise ValueError("worker snapshot updated_at precedes created_at")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("worker snapshot started_at precedes created_at")
        if self.finished_at is not None and self.finished_at < self.created_at:
            raise ValueError("worker snapshot finished_at precedes created_at")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("worker snapshot finished_at precedes started_at")

        terminal = self.status in {"completed", "failed", "cancelled"}
        if terminal != (self.finished_at is not None):
            raise ValueError("worker terminal status and finished_at are inconsistent")
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("worker artifact manifest contains duplicate ids")
        artifact_names = [artifact.name.casefold() for artifact in self.artifacts]
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("worker artifact manifest contains duplicate names")
        if self.status != "completed" and self.artifacts:
            raise ValueError("only completed worker snapshots may expose artifacts")
        if self.status != "completed" and self.artifact_state != "none":
            raise ValueError("only completed worker snapshots may have artifact state")
        if self.artifact_state == "available":
            if self.status != "completed" or not self.artifacts:
                raise ValueError("available worker artifacts require a completed manifest")
        elif self.artifacts:
            raise ValueError("non-available worker artifact states must hide the manifest")
        if self.status == "queued":
            if self.queue_position is None or self.started_at is not None:
                raise ValueError("worker queued snapshot has invalid queue/start state")
        elif self.queue_position is not None:
            raise ValueError("only queued worker snapshots may have queue_position")
        if self.status in {"running", "cancel_requested", "completed"} and self.started_at is None:
            raise ValueError("worker execution status is missing started_at")

        if self.status == "completed":
            if self.result is None or self.error is not None or self.progress_percent != 100:
                raise ValueError("completed worker snapshot must contain only a complete result")
            if self.stage != "artifacts":
                raise ValueError("completed worker snapshot must finish in artifacts stage")
            if self.result.calculation_type != self.request.calculation_type or self.result.model != self.request.model:
                raise ValueError("worker result does not match the requested calculation/model")
            if self.result.input.multiplicity != self.request.input.multiplicity:
                raise ValueError("worker result multiplicity does not match the request")
            prepared = None
            if self.result.schema_version == 2:
                prepared = prepare_monomer_dft_request(
                    _RUN_REQUEST_ADAPTER.validate_python(self.request.scientific_payload())
                )
                expected_charge = prepared.effective_charge
            else:
                expected_charge = (
                    self.result.input.input_formal_charge
                    if self.request.input.net_charge is None
                    else self.request.input.net_charge
                )
            if self.result.input.net_charge != expected_charge:
                raise ValueError("worker result charge does not match the request")
            if self.result.rdkit.seed != self.request.conformer.seed:
                raise ValueError("worker result conformer seed does not match the request")
            if self.result.provenance.worker_instance_id != self.worker_instance_id:
                raise ValueError("worker result provenance instance does not match snapshot")
            if (
                self.result.provenance.model_alias != self.request.model
                or self.result.provenance.model_id != self.request.model
                or self.result.provenance.conformer_seed != self.request.conformer.seed
            ):
                raise ValueError("worker result provenance does not match the request")
            if self.result.timings != self.timings:
                raise ValueError("worker snapshot and result timings differ")
            if self.result.schema_version == 2:
                assert prepared is not None
                result_input = self.result.input
                if (
                    result_input.canonical_smiles != prepared.canonical_smiles
                    or result_input.input_type != prepared.input_type
                    or result_input.input_formal_charge != prepared.formal_charge
                    or result_input.net_charge != prepared.effective_charge
                    or result_input.electron_count != prepared.electron_count
                ):
                    raise ValueError(
                        "worker v2 result input identity does not match the canonical request"
                    )
                result_atoms = self.result.atoms
                if result_atoms.atomic_numbers != list(prepared.atomic_numbers):
                    raise ValueError(
                        "worker v2 atom numbers do not match the canonical request"
                    )
                if result_atoms.isotope_mass_numbers != list(
                    prepared.isotope_mass_numbers
                ):
                    raise ValueError(
                        "worker v2 isotope labels do not match the canonical request"
                    )
                expected_masses = list(prepared.atomic_masses_u)
                actual_masses = result_atoms.atomic_masses_u
                if actual_masses is None or len(actual_masses) != len(expected_masses) or any(
                    not math.isclose(actual, expected, rel_tol=1.0e-12, abs_tol=1.0e-12)
                    for actual, expected in zip(
                        actual_masses, expected_masses, strict=True
                    )
                ):
                    raise ValueError(
                        "worker v2 atomic masses do not match the RDKit mass table"
                    )
                if self.result.provenance.mass_source != _V2_MASS_SOURCE:
                    raise ValueError("worker v2 mass source is not the locked contract value")
            if self.artifact_state == "available":
                required_artifacts = {
                    "initial_structure",
                    "final_structure",
                    "scientific_result",
                }
                if self.result.optimization is not None:
                    required_artifacts.add(self.result.optimization.trajectory_artifact_id)
                if self.result.properties.hessian is not None:
                    required_artifacts.add(self.result.properties.hessian.artifact_id)
                if self.result.properties.frequencies is not None:
                    required_artifacts.add(self.result.properties.frequencies.artifact_id)
                if not required_artifacts.issubset(artifact_ids):
                    raise ValueError(
                        "worker artifact manifest is missing a result-referenced artifact"
                    )
            required = self.request.requested_properties()
            for name in ("charges", "forces", "hessian", "frequencies"):
                if name in required and getattr(self.result.properties, name) is None:
                    raise ValueError(f"worker result is missing requested property: {name}")
            if self.request.calculation_type == "optimization":
                assert self.request.optimization is not None and self.result.optimization is not None
                if (
                    self.result.optimization.max_steps != self.request.optimization.max_steps
                    or self.result.optimization.fmax_threshold_eV_per_A
                    != self.request.optimization.fmax_eV_per_A
                ):
                    raise ValueError("worker optimization result does not match request limits")
            if self.request.input.net_charge is not None and not any(
                warning.code == "net_charge_override" for warning in self.result.warnings
            ):
                raise ValueError("worker result omitted explicit net_charge warning")
        elif self.status == "failed":
            if self.result is not None or self.error is None:
                raise ValueError("failed worker snapshot must contain only a structured error")
        elif self.status == "cancelled":
            if self.result is not None or self.error is not None:
                raise ValueError("cancelled worker snapshot cannot contain result or error")
        elif self.result is not None or self.error is not None or self.finished_at is not None:
            raise ValueError("active worker snapshot cannot contain terminal payloads")
        return self


class InternalWorkerJobList(InternalWorkerModel):
    jobs: list[InternalWorkerSnapshot]
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "InternalWorkerJobList":
        if self.total != len(self.jobs):
            raise ValueError("worker job-list total is inconsistent")
        return self

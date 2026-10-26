"""Versioned wire contracts. Importing this module never imports a scientific engine."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

PROTOCOL_VERSION = 1
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
JobStatus = Literal["pending", "queued", "running", "completed", "failed", "cancel_requested", "cancelled"]
QualityStatus = Literal["not_evaluated", "passed", "warning", "not_converged", "failed"]
StageStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"pending", "queued", "running", "cancel_requested"})

# Literal values are a closed execution boundary, not arbitrary solver strings.
# Keep this list aligned with the frozen, source-backed user configuration.
Forcefield = Literal["GAFF", "GAFF2", "GAFF2_mod", "Dreiding", "Dreiding_UT"]
NativeMethod = Literal["HF", "wb97m-d3bj"]
NativeBasis = Literal["6-31G(d)", "6-31G(d,p)", "6-311G(d,p)", "6-311+G(2d,p)"]
NativeCharge = Literal["RESP", "ESP", "Mulliken", "Lowdin", "gasteiger", "zero"]
NativeProperty = Literal[
    "density", "Rg", "self-diffusion", "Cp", "Cv", "compressibility",
    "isentropic_compressibility", "bulk_modulus", "isentropic_bulk_modulus",
    "volume_expansion", "linear_expansion", "r2", "static_dielectric_const",
    "nematic_order_parameter", "qm_total_energy", "qm_homo", "qm_lumo",
    "qm_dipole", "qm_polarizability", "refractive_index", "thermal_conductivity", "thermal_diffusivity",
    "tg", "sp_total", "sp_ced",
]


def safe_repeat_smiles(value: str) -> str:
    if not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("smiles must be ASCII without whitespace or control characters")
    if value.count("*") != 2 or "." in value or "," in value:
        raise ValueError("each component requires one repeating unit with two connection points")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def finite_json(self):
        # Any-valued scientific/diagnostic mappings must also reject non-JSON
        # and non-finite payloads, including deeply nested NaN results.
        def check(value):
            if isinstance(value,float) and not math.isfinite(value):
                raise ValueError("non-finite scientific data is forbidden")
            if isinstance(value,dict):
                for item in value.values():
                    check(item)
            elif isinstance(value,(list,tuple)):
                for item in value:
                    check(item)
        check(self.model_dump(mode="python"))
        json.dumps(self.model_dump(mode="json"), allow_nan=False)
        return self


class NativeConfiguration(Contract):
    """Explicit native choices; absence on a legacy request stays absent."""

    opt_method: NativeMethod = "wb97m-d3bj"
    opt_basis: NativeBasis = "6-31G(d,p)"
    charge: NativeCharge = "RESP"
    charge_method: NativeMethod = "HF"
    charge_basis: NativeBasis = "6-31G(d)"
    sp_method: NativeMethod = "wb97m-d3bj"
    sp_basis: NativeBasis = "6-311G(d,p)"
    polar_method: NativeMethod = "wb97m-d3bj"
    polar_basis: NativeBasis = "6-311+G(2d,p)"
    properties: list[NativeProperty] = Field(min_length=1, max_length=25)

    @field_validator("properties")
    @classmethod
    def unique_property_targets(cls, value: list[NativeProperty]) -> list[NativeProperty]:
        if len(value) != len(set(value)):
            raise ValueError("native property targets must be unique")
        return sorted(value)


class ChainLength(Contract):
    mode: Literal["atoms", "dp", "molecular_weight"]
    value: float = Field(gt=0, le=1_000_000)

    @model_validator(mode="before")
    @classmethod
    def numeric_target(cls, value):
        if isinstance(value, dict) and (isinstance(value.get("value"), bool)
            or not isinstance(value.get("value"), (int, float))):
            raise ValueError("chain length value must be a number")
        return value

    @model_validator(mode="after")
    def target_range(self):
        if self.mode in {"atoms", "dp"} and not self.value.is_integer():
            raise ValueError("atoms and dp targets must be integers")
        minimum = 1 if self.mode == "dp" else 100
        maximum = 1_000_000 if self.mode == "molecular_weight" else 5000
        if not minimum <= self.value <= maximum:
            raise ValueError(f"{self.mode} target must be between {minimum} and {maximum}")
        return self


class ModelComponent(Contract):
    id: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
    smiles: str = Field(min_length=3, max_length=2048)
    n_chain: int | None = Field(default=None, ge=1, le=20, strict=True)
    chain_length: ChainLength | None = None

    @field_validator("smiles")
    @classmethod
    def valid_repeat_text(cls, value):
        return safe_repeat_smiles(value)

    @model_serializer(mode="wrap")
    def omit_absent_fields(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class ModelBlock(Contract):
    component_id: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")
    dp: int = Field(ge=1, le=5000, strict=True)


class ModelConfiguration(Contract):
    version: Literal[1] = 1
    mode: Literal["homopolymer", "alternating", "random", "block", "blend"]
    components: list[ModelComponent] = Field(min_length=1, max_length=4)
    chain_length: ChainLength | None = None
    ratios: list[float] | None = Field(default=None, min_length=2, max_length=4)
    blocks: list[ModelBlock] | None = Field(default=None, min_length=2, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def exact_version(cls, value):
        if isinstance(value, dict) and "version" in value and type(value["version"]) is not int:
            raise ValueError("model configuration version must be an integer")
        return value

    @field_validator("ratios", mode="before")
    @classmethod
    def numeric_ratios(cls, value):
        if value is not None and (not isinstance(value, list) or any(isinstance(item, bool)
            or not isinstance(item, (int, float)) for item in value)):
            raise ValueError("ratios must be numeric composition fractions")
        return value

    @model_validator(mode="after")
    def model_shape(self):
        identities = [component.id for component in self.components]
        if len(set(identities)) != len(identities):
            raise ValueError("model component ids must be unique")
        if self.mode == "homopolymer" and len(identities) != 1 or self.mode != "homopolymer" and len(identities) < 2:
            raise ValueError("homopolymer requires one component; other modes require two to four")
        if self.mode == "blend":
            if self.chain_length is not None or self.ratios is not None or self.blocks is not None:
                raise ValueError("blend accepts per-component lengths and counts only")
            if any(component.n_chain is None or component.chain_length is None for component in self.components):
                raise ValueError("blend requires a chain count and length for every component")
            if sum(component.n_chain for component in self.components) > 20:
                raise ValueError("blend supports at most twenty chains")
        else:
            if any(component.n_chain is not None or component.chain_length is not None for component in self.components):
                raise ValueError("per-component chain counts and lengths are only valid for blend")
            if self.mode == "block":
                if self.blocks is None or self.chain_length is not None or self.ratios is not None:
                    raise ValueError("block requires explicit blocks and accepts no global length or ratios")
                if {block.component_id for block in self.blocks} != set(identities):
                    raise ValueError("blocks must reference all and only declared component ids")
            else:
                if self.chain_length is None or self.blocks is not None:
                    raise ValueError("this mode requires a global chain length and accepts no blocks")
                if self.mode == "random":
                    if self.ratios is None or len(self.ratios) != len(identities) or any(value <= 0 or value >= 1 for value in self.ratios):
                        raise ValueError("random requires one positive composition fraction per component")
                    if not math.isclose(sum(self.ratios), 1.0, rel_tol=0, abs_tol=1e-8):
                        raise ValueError("random composition fractions must sum to one")
                elif self.ratios is not None:
                    raise ValueError("composition fractions are only valid for random mode")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_fields(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class ScientificRequest(Contract):
    smiles: str = Field(min_length=3, max_length=2048)
    temp: float = Field(default=300.0, ge=100, le=1000, description="Target temperature in kelvin")
    pressure: float = Field(default=1.0, gt=0, le=1000, description="Target pressure in atmospheres")
    n_atom: int = Field(default=1000, ge=100, le=5000, strict=True, description="Approximate atoms per polymer chain; actual count is checked after construction")
    n_chain: int = Field(default=10, ge=1, le=20, strict=True, description="Number of chains in the equilibrium cell")
    forcefield: Forcefield = "GAFF2_mod"
    terminal_head: Literal["CH3", "H", "OH"] = "CH3"
    terminal_tail: Literal["CH3", "H", "OH"] = "CH3"
    tacticity: Literal["atactic", "isotactic", "syndiotactic"] = "atactic"
    calculation: Literal["basic", "basic_tc"] = "basic"
    eq_ns: float = Field(default=5.0, ge=0.01, le=20, description="EQ3 production stage duration in nanoseconds")
    additional_ns: float = Field(default=5.0, ge=0.01, le=20, description="Each additional equilibration duration in nanoseconds")
    max_additional: int = Field(default=2, ge=0, le=2, strict=True, description="Maximum physical equilibration extensions, separate from technical retries")
    sample_interval_ps: float = Field(default=1.0, ge=0.1, le=10, description="EQ3/additional-production thermo and trajectory interval in ps; EQ1 reports its variable-timestep schedule and RNEMD retains fixed 0.2 ps output")
    tc_axis: Literal["x", "y", "z"] = "x"
    tc_ns: float = Field(default=1.0, ge=0.01, le=10, description="RNEMD production duration in nanoseconds")
    tc_rep: int = Field(default=3, ge=1, le=5, strict=True, description="Cell replication factor along tc_axis")
    seed: int | None = Field(default=None, ge=1, le=2147483647, strict=True, description="Null generates a persisted effective seed exactly once per idempotent request")
    native_configuration: NativeConfiguration | None = None
    model_configuration: ModelConfiguration | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_wire_shape(self, handler):
        result = handler(self)
        for key in ("native_configuration", "model_configuration"):
            if getattr(self, key) is None:
                result.pop(key, None)
        return result

    @field_validator("smiles")
    @classmethod
    def safe_smiles_text(cls, value: str) -> str:
        return safe_repeat_smiles(value)

    @model_validator(mode="after")
    def size_budget(self):
        if self.native_configuration is not None:
            if {"sp_total", "sp_ced"} & set(self.native_configuration.properties) and self.n_chain < 2:
                raise ValueError("solubility parameter / cohesive energy density requires at least two polymer chains")
            thermal = bool({"thermal_conductivity", "thermal_diffusivity"} & set(self.native_configuration.properties))
            if thermal != (self.calculation == "basic_tc"):
                raise ValueError("native thermal_conductivity/thermal_diffusivity requires basic_tc; basic_tc requires a thermal target")
        model = self.model_configuration
        if model is not None:
            if self.native_configuration is not None and all(key.startswith("qm_") for key in self.native_configuration.properties):
                raise ValueError("model configurations require a polymer property target; molecular-only subsets are legacy-only")
            if self.smiles != model.components[0].smiles:
                raise ValueError("smiles must equal the first model component smiles")
            if model.mode == "blend" and sum(component.n_chain for component in model.components) != self.n_chain:
                raise ValueError("n_chain must equal the sum of blend component chain counts")
        estimated = self.n_atom * self.n_chain if model is None else (
            sum(component.n_chain * component.chain_length.value for component in model.components)
            if model.mode == "blend" and all(component.chain_length.mode == "atoms" for component in model.components)
            else model.chain_length.value * self.n_chain if model.chain_length is not None and model.chain_length.mode == "atoms" else None)
        if estimated is not None and estimated > 20000:
            raise ValueError("requested cell exceeds 20000 atoms")
        if self.calculation == "basic_tc" and estimated is not None and estimated * self.tc_rep > 60000:
            raise ValueError("replicated RNEMD cell exceeds 60000 atoms")
        return self


def request_sha256(request: ScientificRequest) -> str:
    encoded = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class SubmitRequest(Contract):
    protocol_version: Literal[1] = 1
    job_id: str
    owner_user_id: str
    attempt_token: Sha256
    request_sha256: Sha256
    enqueue_sequence: int = Field(ge=1, strict=True)
    scientific_request: ScientificRequest

    @field_validator("job_id", "owner_user_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @model_validator(mode="after")
    def matching_request(self):
        if self.scientific_request.seed is None:
            raise ValueError("worker request requires an effective seed")
        if request_sha256(self.scientific_request) != self.request_sha256:
            raise ValueError("request hash does not match scientific request")
        return self


class StageRun(Contract):
    stage_run_id: SafeId
    stage: SafeId
    attempt: int = Field(default=1, ge=1, strict=True)
    status: StageStatus = "pending"
    quality_status: QualityStatus = "not_evaluated"
    results: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Artifact(Contract):
    artifact_id: SafeId
    stage_run_id: SafeId
    kind: SafeId
    filename: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    media_type: str = Field(default="application/octet-stream", max_length=120)
    size_bytes: int = Field(ge=0, le=20 * 1024**3, strict=True)
    sha256: Sha256
    storage_ref: str | None = Field(default=None, max_length=1024)

    @field_validator("storage_ref")
    @classmethod
    def safe_storage_ref(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(part in {".",".."} or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*",part) is None for part in value.split("/"))):
            raise ValueError("artifact storage reference must be a safe relative path")
        return value

    @field_validator("filename")
    @classmethod
    def portable_filename(cls, value: str) -> str:
        if value.endswith(".") or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("artifact filename is not portable")
        return value


class WorkerSnapshot(Contract):
    protocol_version: Literal[1] = 1
    job_id: str
    attempt_token: Sha256
    request_sha256: Sha256
    enqueue_sequence: int = Field(ge=1, strict=True)
    worker_instance_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    revision: int = Field(default=1, ge=1, strict=True)
    status: JobStatus
    quality_status: QualityStatus = "not_evaluated"
    stage: SafeId = "queued"
    stage_runs: list[StageRun] = Field(default_factory=list, max_length=50)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=500)
    results: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cleanup_confirmed: bool = False

    @model_validator(mode="after")
    def complete_manifest(self):
        stages = {s.stage_run_id for s in self.stage_runs}
        if len(stages) != len(self.stage_runs):
            raise ValueError("duplicate stage_run_id")
        if len({a.artifact_id for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("duplicate artifact_id")
        if any(a.stage_run_id not in stages for a in self.artifacts):
            raise ValueError("artifact references unknown stage")
        if any(a.storage_ref is not None and not a.storage_ref.startswith(f"{self.job_id}/{self.attempt_token}/") for a in self.artifacts):
            raise ValueError("artifact storage reference belongs to another execution attempt")
        if self.status in TERMINAL_STATUSES and not self.cleanup_confirmed:
            raise ValueError("terminal status requires confirmed resource cleanup")
        return self


class StartAuthorizationRequest(SubmitRequest):
    worker_instance_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    replaces_worker_instance_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    prior_execution_cleanup_confirmed: bool = False


class StartAuthorizationResponse(Contract):
    protocol_version: Literal[1] = 1
    authorized: Literal[True] = True
    job_id: str
    attempt_token: Sha256
    request_sha256: Sha256
    enqueue_sequence: int
    worker_instance_id: str
    start_authorized_at: datetime


class DistanceRequest(Contract):
    atom_id_1: int = Field(ge=1, strict=True)
    atom_id_2: int = Field(ge=1, strict=True)
    start_frame: int = Field(default=0, ge=0, strict=True)
    stop_frame: int | None = Field(default=None, ge=1, strict=True)
    stride: int = Field(default=1, ge=1, le=10000, strict=True)
    use_pbc: bool = True

    @model_validator(mode="after")
    def valid_range(self):
        if self.atom_id_1 == self.atom_id_2:
            raise ValueError("two different atom IDs are required")
        if self.stop_frame is not None and self.stop_frame <= self.start_frame:
            raise ValueError("stop_frame must follow start_frame")
        return self

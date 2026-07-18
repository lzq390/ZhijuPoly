from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JobStatus = Literal[
    "pending",
    "queued",
    "running",
    "failed",
    "cancel_requested",
    "completed",
    "cancelled",
]
CalculationType = Literal["single_point", "optimization"]
JobStage = Literal[
    "queued",
    "validating",
    "conformer",
    "single_point",
    "optimization",
    "hessian",
    "frequency",
    "artifacts",
]
ModelAlias = Literal[
    "aimnet2",
    "aimnet2-b973c",
    "aimnet2-2025",
    "aimnet2-nse",
    "aimnet2-pd",
    "aimnet2-rxn",
]
PropertyName = Literal["energy", "charges", "forces", "hessian", "frequencies"]
ArtifactState = Literal["none", "available", "deleting", "deleted"]
EnqueueSequenceSource = Literal["backend", "legacy_terminal_local", "legacy_mapping"]

PROPERTY_ORDER: tuple[PropertyName, ...] = (
    "energy",
    "charges",
    "forces",
    "hessian",
    "frequencies",
)
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"pending", "queued", "running", "cancel_requested"})
LEGACY_TIMING_KEYS = (
    "queue_wait_ms",
    "structure_prepare_ms",
    "model_compute_ms",
    "optimization_ms",
    "hessian_ms",
    "frequency_ms",
    "artifact_ms",
    "total_ms",
)
TIMING_KEYS = (
    "queue_wait_ms",
    "gpu_wait_ms",
    "model_load_ms",
    "structure_prepare_ms",
    "model_compute_ms",
    "optimization_ms",
    "hessian_ms",
    "frequency_ms",
    "artifact_ms",
    "total_ms",
)
MAX_ARTIFACT_SIZE_BYTES = 64 * 1024 * 1024
MAX_ENQUEUE_SEQUENCE = (1 << 63) - 1

_PORTABLE_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def validate_artifact_name(value: str) -> str:
    """Require a portable, single-component filename suitable for ZIP members."""
    if not _PORTABLE_ARTIFACT_NAME.fullmatch(value):
        raise ValueError(
            "artifact name must be a portable basename without slashes, "
            "backslashes, whitespace, controls, or shell punctuation"
        )
    if value.endswith("."):
        raise ValueError("artifact name must not end with a dot")
    device_name = value.split(".", 1)[0].upper()
    if device_name in _WINDOWS_RESERVED_NAMES:
        raise ValueError("artifact name must not use a reserved device name")
    return value


def default_job_timings() -> dict[str, float]:
    return {key: 0.0 for key in TIMING_KEYS}


def default_legacy_job_timings() -> dict[str, float]:
    return {key: 0.0 for key in LEGACY_TIMING_KEYS}


def normalize_v2_timings(value: Any) -> Any:
    """Read the original eight-key Journal/HTTP V2 timing contract safely.

    Journal V2 was deployed before GPU admission and model-load timings were
    added.  Those additive fields must not make already-durable V2 envelopes
    unreadable.  New writes still serialize the complete ten-key contract.
    """
    if not isinstance(value, dict) or set(value) != set(LEGACY_TIMING_KEYS):
        return value
    normalized = default_job_timings()
    normalized.update(value)
    return normalized


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class GpuExecutionProvenanceV2(BaseModel):
    """Required GPU execution identity for every scientific result V2."""

    model_config = ConfigDict(
        extra="allow",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    execution_path: Literal["primary", "overflow"]
    gpu_uuid: str = Field(min_length=1)
    gpu_budget_mib: int = Field(gt=0)
    broker_instance_id: str = Field(min_length=1)
    lease_id: str = Field(min_length=1)
    parent_lease_id: str | None = Field(default=None, min_length=1)
    fencing_token: int = Field(gt=0)


class MolecularInput(StrictModel):
    smiles: str = Field(min_length=1, max_length=2048)
    net_charge: int | None = Field(default=None, ge=-5, le=5)
    multiplicity: int = Field(default=1, ge=1, le=7)
    psmiles_mode: Literal["close", "cap"] | None = None

    @field_validator("smiles")
    @classmethod
    def validate_smiles_text(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("SMILES/PSMILES must not contain whitespace")
        return value


class ConformerConfig(StrictModel):
    seed: int = Field(default=1, ge=0, le=2_147_483_647)
    max_iterations: int = Field(default=500, ge=1, le=5000)


def _normalize_properties(value: Any) -> tuple[PropertyName, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("properties must be an array")
    requested = list(value)
    if not requested:
        raise ValueError("properties must not be empty")
    unknown = sorted({str(item) for item in requested} - set(PROPERTY_ORDER))
    if unknown:
        raise ValueError(f"unsupported properties: {', '.join(unknown)}")
    if len(set(requested)) != len(requested):
        raise ValueError("properties must not contain duplicates")
    # Property order is semantic-free; canonicalize it for stable idempotency.
    return tuple(item for item in PROPERTY_ORDER if item in requested)


class SinglePointConfig(StrictModel):
    properties: tuple[PropertyName, ...] = (
        "energy",
        "charges",
        "forces",
    )

    @field_validator("properties", mode="before")
    @classmethod
    def normalize_properties(cls, value: Any) -> tuple[PropertyName, ...]:
        return _normalize_properties(value)


class OptimizationConfig(StrictModel):
    fmax_eV_per_A: float = Field(default=0.01, ge=0.001, le=1.0)
    max_steps: int = Field(default=50, ge=10, le=50)
    post_optimization_properties: tuple[PropertyName, ...] = ()

    @field_validator("post_optimization_properties", mode="before")
    @classmethod
    def normalize_properties(cls, value: Any) -> tuple[PropertyName, ...]:
        if isinstance(value, str) or not isinstance(
            value, (list, tuple, set, frozenset)
        ):
            raise ValueError("post_optimization_properties must be an array")
        requested = list(value)
        unknown = sorted({str(item) for item in requested} - {"hessian", "frequencies"})
        if unknown:
            raise ValueError(
                "optimization always returns energy, charges and forces; "
                "post_optimization_properties only accepts hessian/frequencies"
            )
        if len(set(requested)) != len(requested):
            raise ValueError("post_optimization_properties must not contain duplicates")
        return tuple(item for item in PROPERTY_ORDER if item in requested)


class JobSubmitRequest(StrictModel):
    schema_version: Literal[2] = 2
    enqueue_sequence: int = Field(ge=1, le=MAX_ENQUEUE_SEQUENCE)
    job_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    attempt_token: str = Field(pattern=r"^[0-9a-f]{32,64}$")
    request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input: MolecularInput
    calculation_type: CalculationType
    model: ModelAlias = "aimnet2"
    conformer: ConformerConfig = Field(default_factory=ConformerConfig)
    single_point: SinglePointConfig | None = None
    optimization: OptimizationConfig | None = None

    @model_validator(mode="after")
    def validate_task_shape_and_hash(self) -> "JobSubmitRequest":
        if self.calculation_type == "single_point":
            if self.single_point is None:
                raise ValueError(
                    "single_point config is required for a single_point job"
                )
            if self.optimization is not None:
                raise ValueError(
                    "optimization config is not allowed for a single_point job"
                )
        else:
            if self.optimization is None:
                raise ValueError(
                    "optimization config is required for an optimization job"
                )
            if self.single_point is not None:
                raise ValueError(
                    "single_point config is not allowed for an optimization job"
                )

        calculated = compute_request_sha256(self)
        if self.request_sha256 is not None and self.request_sha256 != calculated:
            raise ValueError(
                "request_sha256 does not match the canonical scientific request payload"
            )
        self.request_sha256 = calculated
        return self

    @property
    def properties(self) -> tuple[PropertyName, ...]:
        if self.calculation_type == "single_point":
            assert self.single_point is not None
            requested = list(self.single_point.properties)
            if "frequencies" in requested and "hessian" not in requested:
                requested.append("hessian")
            return tuple(requested)
        assert self.optimization is not None
        requested: list[PropertyName] = ["energy", "charges", "forces"]
        requested.extend(self.optimization.post_optimization_properties)
        if "frequencies" in requested and "hessian" not in requested:
            requested.append("hessian")
        return tuple(requested)


def compute_request_sha256(request: Any) -> str:
    dumped = request.model_dump(mode="json")
    payload = {
        "input": dumped["input"],
        "calculation_type": dumped["calculation_type"],
        "model": dumped["model"],
        "conformer": dumped["conformer"],
    }
    branch = (
        "single_point" if request.calculation_type == "single_point" else "optimization"
    )
    payload[branch] = dumped[branch]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StructuredError(StrictModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ArtifactDescriptor(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=127)
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_SIZE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def validate_safe_name(cls, value: str) -> str:
        return validate_artifact_name(value)


def _validate_artifact_manifest(
    artifacts: list[ArtifactDescriptor],
) -> list[ArtifactDescriptor]:
    artifact_ids = [item.artifact_id for item in artifacts]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("artifact manifest must not contain duplicate artifact ids")
    folded_names = [item.name.casefold() for item in artifacts]
    if len(folded_names) != len(set(folded_names)):
        raise ValueError(
            "artifact manifest must not contain case-insensitive duplicate names"
        )
    return artifacts


class LegacyJobSubmitRequestV1(StrictModel):
    """Read-only compatibility model for pre-V2 durable journals."""

    job_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    attempt_token: str = Field(pattern=r"^[0-9a-f]{32,64}$")
    request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input: MolecularInput
    calculation_type: CalculationType
    model: ModelAlias = "aimnet2"
    conformer: ConformerConfig = Field(default_factory=ConformerConfig)
    single_point: SinglePointConfig | None = None
    optimization: OptimizationConfig | None = None

    @model_validator(mode="after")
    def validate_task_shape_and_hash(self) -> "LegacyJobSubmitRequestV1":
        if self.calculation_type == "single_point":
            if self.single_point is None or self.optimization is not None:
                raise ValueError("legacy single_point journal has an invalid branch")
        elif self.optimization is None or self.single_point is not None:
            raise ValueError("legacy optimization journal has an invalid branch")
        calculated = compute_request_sha256(self)
        if self.request_sha256 is not None and self.request_sha256 != calculated:
            raise ValueError(
                "legacy request_sha256 does not match its scientific payload"
            )
        self.request_sha256 = calculated
        return self

    @property
    def properties(self) -> tuple[PropertyName, ...]:
        if self.calculation_type == "single_point":
            assert self.single_point is not None
            requested = list(self.single_point.properties)
        else:
            assert self.optimization is not None
            requested = ["energy", "charges", "forces"]
            requested.extend(self.optimization.post_optimization_properties)
        if "frequencies" in requested and "hessian" not in requested:
            requested.append("hessian")
        return tuple(requested)


ScientificRequest: TypeAlias = JobSubmitRequest | LegacyJobSubmitRequestV1


class JobSnapshot(StrictModel):
    schema_version: Literal[2] = 2
    enqueue_sequence: int = Field(ge=1, le=MAX_ENQUEUE_SEQUENCE)
    job_id: str
    attempt_token: str
    request_sha256: str
    worker_instance_id: str
    status: JobStatus
    queue_position: int | None = Field(default=None, ge=1)
    stage: JobStage
    progress_percent: int = Field(ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    request: JobSubmitRequest
    result: dict[str, Any] | None = None
    error: StructuredError | None = None
    timings: dict[str, float] = Field(default_factory=default_job_timings)
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)

    @field_validator("timings", mode="before")
    @classmethod
    def validate_timing_contract(cls, value: Any) -> Any:
        value = normalize_v2_timings(value)
        if not isinstance(value, dict):
            return value
        if set(value) != set(TIMING_KEYS):
            raise ValueError(
                "timings must contain exactly the ten worker timing keys"
            )
        if any(item < 0.0 for item in value.values()):
            raise ValueError("timings must be non-negative milliseconds")
        return value

    @field_validator("result", mode="before")
    @classmethod
    def normalize_legacy_v2_result_timings(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "timings" not in value:
            return value
        normalized_timings = normalize_v2_timings(value.get("timings"))
        if normalized_timings is value.get("timings"):
            return value
        normalized = dict(value)
        normalized["timings"] = normalized_timings
        return normalized

    @field_validator("artifacts")
    @classmethod
    def validate_artifacts(
        cls, value: list[ArtifactDescriptor]
    ) -> list[ArtifactDescriptor]:
        return _validate_artifact_manifest(value)

    @model_validator(mode="after")
    def validate_request_envelope(self) -> "JobSnapshot":
        if self.request.enqueue_sequence != self.enqueue_sequence:
            raise ValueError("snapshot enqueue_sequence does not match its request")
        if (
            self.request.job_id != self.job_id
            or self.request.attempt_token != self.attempt_token
            or self.request.request_sha256 != self.request_sha256
        ):
            raise ValueError("snapshot identity does not match its request")
        return self


class PublicJobSnapshot(JobSnapshot):
    """HTTP V2 projection; artifact lifecycle remains journal-envelope state."""

    artifact_state: ArtifactState


class LegacyJobSnapshotV1(StrictModel):
    schema_version: Literal[1] = 1
    job_id: str
    attempt_token: str
    request_sha256: str
    worker_instance_id: str
    status: JobStatus
    queue_position: int | None = Field(default=None, ge=1)
    stage: JobStage
    progress_percent: int = Field(ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    request: LegacyJobSubmitRequestV1
    result: dict[str, Any] | None = None
    error: StructuredError | None = None
    timings: dict[str, float] = Field(default_factory=default_legacy_job_timings)
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)

    @field_validator("timings")
    @classmethod
    def validate_timing_contract(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != set(LEGACY_TIMING_KEYS):
            raise ValueError(
                "timings must contain exactly the eight legacy worker timing keys"
            )
        if any(item < 0.0 for item in value.values()):
            raise ValueError("timings must be non-negative milliseconds")
        return value

    @field_validator("artifacts")
    @classmethod
    def validate_artifacts(
        cls, value: list[ArtifactDescriptor]
    ) -> list[ArtifactDescriptor]:
        return _validate_artifact_manifest(value)

    @model_validator(mode="after")
    def validate_request_envelope(self) -> "LegacyJobSnapshotV1":
        if (
            self.request.job_id != self.job_id
            or self.request.attempt_token != self.attempt_token
            or self.request.request_sha256 != self.request_sha256
        ):
            raise ValueError("legacy snapshot identity does not match its request")
        return self


class JobJournalV2(StrictModel):
    journal_schema_version: Literal[2] = 2
    snapshot: JobSnapshot
    enqueue_sequence: int = Field(ge=1, le=MAX_ENQUEUE_SEQUENCE)
    enqueue_sequence_source: EnqueueSequenceSource
    artifact_state: ArtifactState
    artifact_manifest: list[ArtifactDescriptor] = Field(default_factory=list)
    artifact_delete_requested_at: datetime | None = None
    artifacts_deleted_at: datetime | None = None

    @field_validator("artifact_manifest")
    @classmethod
    def validate_artifacts(
        cls, value: list[ArtifactDescriptor]
    ) -> list[ArtifactDescriptor]:
        return _validate_artifact_manifest(value)

    @model_validator(mode="after")
    def validate_state(self) -> "JobJournalV2":
        if self.snapshot.queue_position is not None:
            raise ValueError(
                "durable journals must not persist a derived queue_position"
            )
        if self.snapshot.enqueue_sequence != self.enqueue_sequence:
            raise ValueError("journal enqueue_sequence does not match its V2 snapshot")
        if (
            self.snapshot.status not in TERMINAL_STATUSES
            and self.artifact_state != "none"
        ):
            raise ValueError("active journals cannot publish or delete artifacts")
        if self.artifact_state == "available":
            if self.snapshot.status != "completed" or not self.artifact_manifest:
                raise ValueError(
                    "available artifacts require a completed job and manifest"
                )
            if self.snapshot.artifacts != self.artifact_manifest:
                raise ValueError("available journal manifest differs from its snapshot")
        if (
            self.artifact_state in {"deleting", "deleted"}
            and self.snapshot.status != "completed"
        ):
            raise ValueError("artifact deletion state requires a completed job")
        if self.artifact_state == "none" and self.artifact_manifest:
            raise ValueError("artifact_state none cannot retain a published manifest")
        if self.artifact_state == "none" and self.snapshot.artifacts:
            raise ValueError("artifact_state none cannot expose snapshot artifacts")
        if (
            self.artifact_state == "deleting"
            and self.artifact_delete_requested_at is None
        ):
            raise ValueError("deleting artifacts require a request timestamp")
        if self.artifact_state == "deleting" and self.snapshot.artifacts:
            raise ValueError("deleting journal snapshots must not expose artifacts")
        if self.artifact_state == "deleted":
            if self.artifacts_deleted_at is None:
                raise ValueError("deleted artifacts require a completion timestamp")
            if self.snapshot.artifacts:
                raise ValueError("deleted journal snapshots must not expose artifacts")
        return self


class JobListResponse(StrictModel):
    jobs: list[PublicJobSnapshot]
    total: int = Field(ge=0)


class DrainResponse(StrictModel):
    status: Literal["draining", "ready"]
    accepting_jobs: bool
    active_jobs: int = Field(ge=0)
    queued_jobs: int = Field(ge=0)
    worker_instance_id: str


class HealthResponse(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["ok", "degraded"]
    runtime_ready: bool
    accepting_jobs: bool
    draining: bool
    recovering: bool
    active_jobs: int = Field(ge=0)
    queued_jobs: int = Field(ge=0)
    max_concurrent_jobs: Literal[1] = 1
    max_queued_jobs: Literal[8] = 8
    worker_instance_id: str
    worker_version: str
    runtime: dict[str, Any]


class CapabilitiesResponse(StrictModel):
    schema_version: Literal[1] = 1
    models: list[dict[str, Any]]
    calculation_types: list[CalculationType]
    properties: list[PropertyName]
    input_limits: dict[str, Any]
    queue: dict[str, int]


class ArtifactDeletionResponse(StrictModel):
    job_id: str
    deleted: bool
    artifact_state: Literal["none", "deleted"]
    deleted_artifacts: int = Field(ge=0)
    message: str

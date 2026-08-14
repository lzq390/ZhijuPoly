from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


MonomerDftModelName: TypeAlias = Literal[
    "aimnet2",
    "aimnet2-2025",
    "aimnet2-b973c",
    "aimnet2-nse",
    "aimnet2-pd",
    "aimnet2-rxn",
]
MonomerDftProperty: TypeAlias = Literal[
    "energy",
    "charges",
    "forces",
    "hessian",
    "frequencies",
]
MonomerDftPostOptimizationProperty: TypeAlias = Literal["hessian", "frequencies"]
MonomerDftCalculationType: TypeAlias = Literal["single_point", "optimization"]
MonomerDftArtifactsState: TypeAlias = Literal[
    "none",
    "available",
    "delete_requested",
    "deleted",
]
MonomerDftJobStatus: TypeAlias = Literal[
    "pending",
    "queued",
    "running",
    "completed",
    "failed",
    "cancel_requested",
    "cancelled",
]
MonomerDftJobStage: TypeAlias = Literal[
    "queued",
    "validating",
    "conformer",
    "single_point",
    "optimization",
    "hessian",
    "frequency",
    "artifacts",
]

PROPERTY_ORDER = ("energy", "charges", "forces", "hessian", "frequencies")
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
PORTABLE_ARTIFACT_FILENAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$"
_PORTABLE_ARTIFACT_FILENAME = re.compile(PORTABLE_ARTIFACT_FILENAME_PATTERN)
_WINDOWS_RESERVED_ARTIFACT_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def validate_portable_artifact_filename(value: Any) -> str:
    if not isinstance(value, str) or _PORTABLE_ARTIFACT_FILENAME.fullmatch(value) is None:
        raise ValueError("artifact name must be a portable basename")
    if value.endswith("."):
        raise ValueError("artifact name must not end with a dot")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_ARTIFACT_NAMES:
        raise ValueError("artifact name must not use a reserved device name")
    return value


class StrictApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class MonomerDftInput(StrictApiModel):
    smiles: Annotated[StrictStr, Field(min_length=1, max_length=2_048)]
    net_charge: Annotated[StrictInt, Field(ge=-5, le=5)] | None = None
    multiplicity: Annotated[StrictInt, Field(ge=1, le=7)] = 1
    psmiles_mode: Literal["close", "cap"] | None = None

    @field_validator("smiles")
    @classmethod
    def validate_smiles_text(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("input.smiles must not contain whitespace")
        return value


class MonomerDftConformer(StrictApiModel):
    seed: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)] = 1
    max_iterations: Annotated[StrictInt, Field(ge=1, le=5_000)] = 500


class MonomerDftSinglePointOptions(StrictApiModel):
    properties: Annotated[list[MonomerDftProperty], Field(min_length=1, max_length=5)] = Field(
        default_factory=lambda: ["energy", "charges", "forces"]
    )

    @field_validator("properties")
    @classmethod
    def validate_properties(cls, value: list[MonomerDftProperty]) -> list[MonomerDftProperty]:
        if len(value) != len(set(value)):
            raise ValueError("single_point.properties must not contain duplicates")
        return [item for item in PROPERTY_ORDER if item in value]


class MonomerDftOptimizationOptions(StrictApiModel):
    fmax_eV_per_A: Annotated[StrictFloat, Field(ge=0.001, le=1.0)] = 0.01
    max_steps: Annotated[StrictInt, Field(ge=10, le=50)] = 50
    post_optimization_properties: Annotated[
        list[MonomerDftPostOptimizationProperty], Field(max_length=2)
    ] = Field(default_factory=list)

    @field_validator("post_optimization_properties")
    @classmethod
    def validate_properties(
        cls, value: list[MonomerDftPostOptimizationProperty]
    ) -> list[MonomerDftPostOptimizationProperty]:
        if len(value) != len(set(value)):
            raise ValueError("optimization.post_optimization_properties must not contain duplicates")
        return [item for item in ("hessian", "frequencies") if item in value]


class _MonomerDftRequestBase(StrictApiModel):
    input: MonomerDftInput
    model: MonomerDftModelName = "aimnet2"
    conformer: MonomerDftConformer = Field(default_factory=MonomerDftConformer)


class MonomerDftSinglePointRequest(_MonomerDftRequestBase):
    calculation_type: Literal["single_point"]
    single_point: MonomerDftSinglePointOptions


class MonomerDftOptimizationRequest(_MonomerDftRequestBase):
    calculation_type: Literal["optimization"]
    optimization: MonomerDftOptimizationOptions


MonomerDftRunRequest: TypeAlias = Annotated[
    MonomerDftSinglePointRequest | MonomerDftOptimizationRequest,
    Field(discriminator="calculation_type"),
]


class MonomerDftArtifact(StrictApiModel):
    artifact_id: Annotated[
        StrictStr,
        Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
    ]
    name: Annotated[
        StrictStr,
        Field(min_length=1, max_length=255, pattern=PORTABLE_ARTIFACT_FILENAME_PATTERN),
    ]
    media_type: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    size_bytes: Annotated[StrictInt, Field(ge=0, le=MAX_ARTIFACT_BYTES)]
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    available: StrictBool = True

    @field_validator("name", mode="before")
    @classmethod
    def validate_safe_name(cls, value: Any) -> str:
        return validate_portable_artifact_filename(value)


class MonomerDftJobError(StrictApiModel):
    code: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    message: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]
    retryable: StrictBool = False
    details: dict[str, Any] = Field(default_factory=dict)


class MonomerDftJobResponse(StrictApiModel):
    job_id: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
    ]
    calculation_type: MonomerDftCalculationType
    status: MonomerDftJobStatus
    request: MonomerDftSinglePointRequest | MonomerDftOptimizationRequest = Field(
        discriminator="calculation_type"
    )
    request_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    attempt: Annotated[StrictInt, Field(ge=1)]
    queue_position: Annotated[StrictInt, Field(ge=1)] | None = None
    stage: MonomerDftJobStage
    progress_percent: Annotated[StrictFloat, Field(ge=0.0, le=100.0)]
    scientific_status: StrictStr | None = None
    warnings: list[StrictStr] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    timings: dict[str, float] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    error: MonomerDftJobError | None = None
    artifacts: list[MonomerDftArtifact] = Field(default_factory=list)
    artifacts_state: MonomerDftArtifactsState = "none"
    artifacts_deleted: StrictBool = False
    cancel_requested: StrictBool = False
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    idempotent_replay: StrictBool = False

    @model_validator(mode="after")
    def request_matches_job(self) -> "MonomerDftJobResponse":
        if self.request.calculation_type != self.calculation_type:
            raise ValueError("request calculation_type does not match the job")
        if self.artifacts_deleted != (self.artifacts_state == "deleted"):
            raise ValueError("artifacts_deleted does not match artifacts_state")
        return self


class MonomerDftJobListResponse(StrictApiModel):
    items: list[MonomerDftJobResponse]
    page: Annotated[StrictInt, Field(ge=1)]
    page_size: Annotated[StrictInt, Field(ge=1, le=100)]
    total: Annotated[StrictInt, Field(ge=0)]


class MonomerDftArtifactDeleteResponse(StrictApiModel):
    job_id: Annotated[
        StrictStr,
        Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
    ]
    deleted: StrictBool
    artifacts_state: MonomerDftArtifactsState
    deleted_artifacts: Annotated[StrictInt, Field(ge=0)]
    message: StrictStr


class MonomerDftStatusResponse(StrictApiModel):
    enabled: StrictBool
    available: StrictBool
    schema_ready: StrictBool
    worker_status: StrictStr
    runtime_ready: StrictBool | None = None
    gpu_guard_mode: Literal["enforce", "observe"] | None = None
    gpu_guard_status: Literal[
        "ready", "quarantined", "missing", "stale", "invalid"
    ] | None = None
    gpu_contention_observed: StrictBool = False
    draining: StrictBool | None = None
    active_jobs: Annotated[StrictInt, Field(ge=0)]
    max_active_jobs: Annotated[StrictInt, Field(ge=1)]
    job_retention_enabled: StrictBool = False
    job_retention_days: Annotated[StrictInt, Field(ge=1, le=3650)] = 30
    job_retention_status: Literal[
        "disabled", "standby", "ready", "degraded"
    ] = "disabled"
    job_retention_last_sweep_at: datetime | None = None
    message: StrictStr


class MonomerDftModelCapability(StrictApiModel):
    id: MonomerDftModelName
    label: StrictStr
    description: StrictStr
    available: StrictBool
    is_default: StrictBool = False
    deprecated: StrictBool = False
    deprecation_message: StrictStr | None = None
    supported_calculation_types: list[MonomerDftCalculationType]
    supported_properties: list[MonomerDftProperty]
    supported_elements: list[StrictStr]
    supports_spin: StrictBool
    charge_min: Annotated[StrictInt, Field(ge=-5, le=5)] = -5
    charge_max: Annotated[StrictInt, Field(ge=-5, le=5)] = 5


class MonomerDftCapabilityDefaults(StrictApiModel):
    conformer: MonomerDftConformer = Field(default_factory=MonomerDftConformer)
    single_point: MonomerDftSinglePointOptions = Field(default_factory=MonomerDftSinglePointOptions)
    optimization: MonomerDftOptimizationOptions = Field(default_factory=MonomerDftOptimizationOptions)


class MonomerDftCapabilityLimits(StrictApiModel):
    max_atoms: Annotated[StrictInt, Field(ge=1)]
    max_heavy_atoms: Annotated[StrictInt, Field(ge=1)]
    max_hessian_atoms: Annotated[StrictInt, Field(ge=1)]
    min_optimization_steps: Annotated[StrictInt, Field(ge=1)] = 10
    max_optimization_steps: Annotated[StrictInt, Field(ge=1)] = 50
    max_concurrent_jobs: Annotated[StrictInt, Field(ge=1)]
    max_queued_jobs: Annotated[StrictInt, Field(ge=0)]
    max_active_jobs: Annotated[StrictInt, Field(ge=1)]


class MonomerDftCapabilitiesResponse(StrictApiModel):
    enabled: StrictBool
    available: StrictBool
    schema_ready: StrictBool
    calculation_types: list[MonomerDftCalculationType]
    properties: list[MonomerDftProperty]
    default_model: MonomerDftModelName
    models: list[MonomerDftModelCapability]
    defaults: MonomerDftCapabilityDefaults
    limits: MonomerDftCapabilityLimits
    worker: dict[str, Any] = Field(default_factory=dict)
    message: StrictStr | None = None

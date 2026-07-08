from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ProtocolName = Literal["DensityDemo", "Density", "Transport", "HVap", "Dielectric", "Compressibility"]
RunMode = Literal["demo", "formal"]


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    smiles: str = Field(min_length=1, max_length=2048)
    canonical_smiles: str | None = Field(default=None, max_length=2048)
    steps: int | None = Field(default=None, ge=1)
    protocol: ProtocolName = "DensityDemo"
    run_mode: RunMode = "demo"
    config_json: dict[str, Any] | None = None

    @field_validator("job_id", "smiles", "canonical_smiles")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_shape(self) -> "JobRequest":
        if self.run_mode == "demo" and self.protocol == "DensityDemo":
            return self
        if self.run_mode != "formal":
            raise ValueError("formal ByteFF2 protocols must use run_mode='formal'")
        if self.protocol == "DensityDemo":
            raise ValueError("DensityDemo must use run_mode='demo'")
        if self.config_json is None:
            raise ValueError("config_json is required for formal ByteFF2 jobs")
        return self


class JobAccepted(BaseModel):
    job_id: str
    status: str
    mode: str
    steps: int
    worker_id: str
    worker_job_id: str
    worker_version: str


class HealthResponse(BaseModel):
    status: str
    mode: str
    db_configured: bool
    byteff2_root: str
    byteff2_root_exists: bool
    runtime_ready: bool
    runtime_error: str | None = None
    job_root: str
    active_jobs: int
    default_steps: int
    max_steps: int
    report_interval: int
    worker_id: str
    worker_version: str
    protocols: dict[str, Any] = Field(default_factory=dict)


class ArtifactDeletionResponse(BaseModel):
    job_id: str
    deleted: bool
    artifact_root: str
    message: str

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    smiles: str = Field(min_length=1, max_length=2048)
    canonical_smiles: str | None = Field(default=None, max_length=2048)
    steps: int | None = Field(default=None, ge=1)

    @field_validator("job_id", "smiles", "canonical_smiles")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped


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
